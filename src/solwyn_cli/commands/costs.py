"""Project cost analytics, paginated events, totals, and streamed exports."""

import json
import sys
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import quote

import typer
import httpx
from pydantic import ValidationError

from solwyn_cli.auth import (
    AuthError,
    JwtRequiredError,
    JwtCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands._cost_filters import (
    UnsupportedAccountCostFilterError,
    cost_params,
    validate_group_by,
)
from solwyn_cli.commands._cost_models import (
    AccountCostQueryResponse,
    CostGroup,
    CostQueryResponse,
    CostEventsResponse,
    CostTotalResponse,
    CostWindow,
)
from solwyn_cli.commands._entitlement import get_capabilities
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, OutputFormat, TableData, write_byte_stream
from solwyn_cli.runtime import command_context, output_engine


class CostRange(StrEnum):
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    YEAR = "365d"


CostsData = CostQueryResponse
AccountCostsData = AccountCostQueryResponse

COSTS_HELP = help_contract(
    endpoint=(
        "GET /api/v1/projects/{project}/costs or, with --account, "
        "GET /api/v1/account/costs"
    ),
    auth="project key or JWT; --account requires JWT",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.costs.v1 or solwyn.account_costs.v1",
)
app = typer.Typer(invoke_without_command=True, no_args_is_help=False, epilog=COSTS_HELP)


def fetch_costs(
    *,
    client: SolwynClient,
    project_id: str,
    time_range: str = "7d",
    group_by: str = "model",
    **filters: Any,
) -> CostsData:
    """Read one validated aggregate cost query using shared filter semantics."""
    validate_group_by(group_by)
    params = cost_params(time_range=time_range, **filters)
    params["group_by"] = group_by
    response = client.request(
        "GET", f"/api/v1/projects/{quote(project_id, safe='')}/costs", params=params
    )
    data = CostsData.model_validate(response.json())
    if data.project_id != project_id:
        raise ValueError("Costs response project did not match the request.")
    return data


def fetch_cost_total(
    *, client: SolwynClient, project_id: str, time_range: str = "7d", **filters: Any
) -> CostTotalResponse:
    """Read an uncapped project total with the same window and filters."""
    response = client.request(
        "GET",
        f"/api/v1/projects/{quote(project_id, safe='')}/costs/total",
        params=cost_params(time_range=time_range, **filters),
    )
    data = CostTotalResponse.model_validate(response.json())
    if data.project_id != project_id:
        raise ValueError("Costs response project did not match the request.")
    return data


def fetch_account_costs(
    *,
    client: SolwynClient,
    time_range: str = "7d",
    group_by: str = "project",
    **filters: Any,
) -> AccountCostsData:
    """Read an account-wide cost rollup."""
    data, _ = _request_account_costs(
        client=client,
        time_range=time_range,
        group_by=group_by,
        **filters,
    )
    return data


def _request_account_costs(
    *,
    client: SolwynClient,
    time_range: str = "7d",
    group_by: str = "project",
    **filters: Any,
) -> tuple[AccountCostsData, httpx.Headers]:
    """Read account costs and retain Core's effective-window headers."""
    if not isinstance(client.current_credential, JwtCredential):
        raise JwtRequiredError("Account costs require JWT.")
    if any(
        filters.get(name) is not None
        for name in (
            "status",
            "service_tier",
            "provider_region",
            "agent_run",
            "failover_reason",
            "model_fallback",
            "provider_fallback",
            "tag",
        )
    ):
        raise UnsupportedAccountCostFilterError(
            "Account costs support only model, provider, modality, group_by and window filters."
        )
    params = cost_params(time_range=time_range, **filters)
    params["group_by"] = group_by
    response = client.request("GET", "/api/v1/account/costs", params=params)
    return AccountCostsData.model_validate(response.json()), response.headers


def report_window_headers(headers: httpx.Headers) -> None:
    """Print account-cost requested/effective window metadata when supplied."""
    for suffix in (
        "Requested-From",
        "Requested-To",
        "Effective-From",
        "Effective-To",
        "Retention-Clamped",
    ):
        name = f"X-Solwyn-{suffix}"
        if name in headers:
            typer.echo(f"{name}: {headers[name]}", err=True)


def report_window(window: CostWindow) -> None:
    """Warn only when retention changes the requested analytics window."""
    if window.retention_clamped:
        typer.echo(
            f"Retention clamped window: {window.effective_from} to {window.effective_to} (requested {window.requested_from} to {window.requested_to}).",
            err=True,
        )


def stream_export(*, client: SolwynClient, path: str, params: dict[str, Any]) -> None:
    """Copy raw response bytes and surface Core window headers on stderr."""
    with client.stream("GET", path, params=params) as response:
        for suffix in (
            "Requested-From",
            "Requested-To",
            "Effective-From",
            "Effective-To",
            "Retention-Clamped",
            "Export-Snapshot",
        ):
            name = f"X-Solwyn-{suffix}"
            if name in response.headers:
                typer.echo(f"{name}: {response.headers[name]}", err=True)
        write_byte_stream(chunks=response.iter_bytes(), output=sys.stdout.buffer)


def _table(data: CostsData) -> TableData:
    named = any(group.agent_run_name is not None for group in data.groups)
    columns = (
        "Group",
        "Provider",
        "Cost",
        "Input",
        "Output",
        "Calls",
        "Unpriced",
        "Local",
    )
    if named:
        columns += ("Run name",)
    rows = tuple(
        (
            group.group_key if group.group_key is not None else "—",
            group.provider or "-",
            f"{group.total_cost:.6f}",
            str(group.total_input_tokens),
            str(group.total_output_tokens),
            str(group.call_count),
            str(group.unpriced_call_count),
            str(group.local_call_count),
        )
        + ((group.agent_run_name or "—",) if named else ())
        for group in data.groups
    )
    return TableData(
        columns=columns,
        rows=rows,
        right_align=("Cost", "Input", "Output", "Calls", "Unpriced", "Local"),
    )


def _sparkline(points: list[Any] | None) -> str:
    if not points:
        return "—"
    levels = "▁▂▃▄▅▆▇█"
    maximum = max(point.cost for point in points)
    if maximum <= 0:
        return levels[0] * len(points)
    return "".join(
        levels[min(len(levels) - 1, round(point.cost / maximum * (len(levels) - 1)))]
        for point in points
    )


def _account_table(data: AccountCostsData) -> TableData:
    if data.analytics_withheld:
        return TableData(
            columns=("Project", "Current period"),
            rows=tuple(
                (
                    group.project_name or group.group_key or "—",
                    "—"
                    if group.current_period_cost is None
                    else f"{group.current_period_cost:.6f}",
                )
                for group in data.groups
            ),
            right_align=("Current period",),
        )
    if data.group_by == "project":
        return TableData(
            columns=("Project", "Window cost", "Current period", "Calls", "Daily"),
            rows=tuple(
                (
                    group.project_name or group.group_key or "—",
                    f"{group.total_cost:.6f}",
                    "—"
                    if group.current_period_cost is None
                    else f"{group.current_period_cost:.6f}",
                    str(group.call_count),
                    _sparkline(group.daily),
                )
                for group in data.groups
            ),
            right_align=("Window cost", "Current period", "Calls"),
        )
    return _table(
        CostsData(
            project_id="account",
            range=data.range,
            group_by=data.group_by,
            groups=[
                CostGroup.model_validate(
                    group.model_dump(
                        exclude={"project_name", "current_period_cost", "daily"}
                    )
                )
                for group in data.groups
            ],
            truncated=data.truncated,
            savings=data.savings,
            window=data.window,
        )
    )


def _events_table(data: CostEventsResponse) -> TableData:
    return TableData(
        columns=(
            "Time",
            "Model",
            "Provider",
            "Cost",
            "Denied cost",
            "Deny source",
            "Deny reason",
            "SDK instance",
            "Tags",
        ),
        rows=tuple(
            (
                e.created_at,
                e.model,
                e.provider,
                "—" if e.is_unpriced else f"{e.cost:.6f}",
                f"{e.denied_cost:.6f}",
                e.deny_source or "—",
                e.deny_reason or "—",
                e.sdk_instance_id,
                json.dumps(e.tags, ensure_ascii=False, sort_keys=True)
                if e.tags is not None
                else "—",
            )
            for e in data.items
        ),
    )


def _total_table(data: CostTotalResponse) -> TableData:
    return TableData(
        columns=("Cost", "Input", "Output", "Calls", "Unpriced", "Local"),
        rows=(
            (
                f"{data.total_cost:.6f}",
                str(data.total_input_tokens),
                str(data.total_output_tokens),
                str(data.call_count),
                str(data.unpriced_call_count),
                str(data.local_call_count),
            ),
        ),
    )


def _run(
    context: typer.Context,
    operation: str,
    *,
    group_by: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
    export_format: str = "csv",
    account: bool = False,
    **filters: Any,
) -> None:
    runtime = command_context(context)
    if runtime.project is None and not account:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint="Choose a project with --project, SOLWYN_PROJECT_ID, or config default_project.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if account:
        unsupported = next(
            (
                name
                for name in (
                    "status",
                    "service_tier",
                    "provider_region",
                    "agent_run",
                    "failover_reason",
                    "model_fallback",
                    "provider_fallback",
                    "tag",
                )
                if filters.get(name) is not None
            ),
            None,
        )
        if unsupported is not None:
            exit_with_error(
                context,
                OutputError(
                    code="invalid_cost_query",
                    message=(
                        "Account costs support only model, provider, modality, "
                        "group-by, and window filters."
                    ),
                ),
                semantic=ErrorSemantic.USAGE,
            )
    try:
        if not account:
            validate_group_by(group_by)
        params = cost_params(**filters)
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_cost_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    if account and not isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Account costs require user login.",
                hint="Unset SOLWYN_API_KEY and run solwyn login, then retry.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    entitlement_hints: list[str] = []
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            capabilities = get_capabilities(client=client)
            if capabilities is not None:
                analytics = capabilities.analytics
                # Entitlements distinguish allowed values from paid values, not
                # invalid dimensions. Core must decide 400 versus 402.
                if group_by is not None and not (
                    (account and group_by == "project")
                    or group_by in analytics.allowed_group_by
                    or (group_by.startswith("tag:") and analytics.tags)
                ):
                    entitlement_hints.append(
                        "Current plan groupings: "
                        + ", ".join(analytics.allowed_group_by)
                        + ("; tag:<key>" if analytics.tags else "")
                        + ".",
                    )
                if (
                    "range" in params
                    and params["range"] not in analytics.allowed_ranges
                ):
                    entitlement_hints.append(
                        "Current plan ranges: "
                        + ", ".join(analytics.allowed_ranges)
                        + ".",
                    )
            if account:
                data, account_headers = _request_account_costs(
                    client=client,
                    group_by=group_by or "project",
                    **filters,
                )
                table = _account_table(data)
            else:
                path = f"/api/v1/projects/{quote(runtime.project, safe='')}/costs"
            if not account and operation == "export":
                params["format"] = export_format
                if group_by is not None:
                    params["group_by"] = group_by
                stream_export(client=client, path=path + "/export", params=params)
                return
            if not account and operation == "events":
                from solwyn_cli.commands._cost_reads import fetch_cost_events

                data = fetch_cost_events(
                    client=client,
                    project_id=runtime.project,
                    limit=limit,
                    cursor=cursor,
                    **filters,
                )
                table = _events_table(data)
            elif not account and operation == "total":
                data = fetch_cost_total(
                    client=client, project_id=runtime.project, **filters
                )
                table = _total_table(data)
            elif not account:
                data = fetch_costs(
                    client=client,
                    project_id=runtime.project,
                    group_by=group_by,
                    **filters,
                )
                table = _table(data)
    except ApiError as error:
        if entitlement_hints and error.status_code in {400, 402, 422}:
            error.hint = " ".join(filter(None, [error.hint, *entitlement_hints]))
        exit_with_error(
            context,
            error,
            semantic=ErrorSemantic.USAGE if error.status_code in {400, 422} else None,
        )
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_costs_response", message="The costs response was invalid."
            ),
        )
    report_window(data.window)
    if account:
        report_window_headers(account_headers)
    if (
        isinstance(data, CostEventsResponse)
        and data.has_more
        and runtime.output_format is OutputFormat.TABLE
    ):
        if data.next_cursor is not None:
            typer.echo(
                "More events are available. Next cursor (pass with --cursor): "
                + json.dumps(data.next_cursor),
                err=True,
            )
        else:
            typer.echo(
                "More events are available, but Core returned no continuation cursor.",
                err=True,
            )
    if isinstance(data, (CostsData, AccountCostsData)) and data.truncated:
        typer.echo("showing top 100 groups", err=True)
    if getattr(data, "savings", None) is not None:
        savings = data.savings
        upper = (
            "—"
            if savings.saved_cost_upper is None
            else f"${savings.saved_cost_upper:.6f}"
        )
        typer.echo(
            f"Savings: ${savings.saved_cost:.6f}; upper {upper}; blocked calls {savings.blocked_call_count}.",
            err=True,
        )
    schema = (
        "solwyn.account_costs.v1"
        if account
        else (
            "solwyn.costs.v1"
            if operation == "aggregate"
            else f"solwyn.costs.{operation}.v1"
        )
    )
    payload = data.model_dump(mode="json")
    if payload.get("savings", False) is None:
        payload.pop("savings")
    if account and data.analytics_withheld:
        payload["groups"] = [
            {
                "group_key": group.group_key,
                "project_name": group.project_name,
                "current_period_cost": group.current_period_cost,
            }
            for group in data.groups
        ]
    output_engine(context).write_success(schema=schema, data=payload, table=table)


@app.callback()
def costs(
    context: typer.Context,
    group_by: Annotated[
        str,
        typer.Option(
            "--group-by",
            help="Dimension from billing capabilities, or tag:<key> (Team+). Omit for raw export.",
        ),
    ] = "model",
    account: Annotated[
        bool,
        typer.Option(
            "--account",
            help="Show the JWT-authenticated account rollup across projects.",
        ),
    ] = False,
    time_range: Annotated[
        str,
        typer.Option(
            "--range",
            help="Preset window; allowed ranges come from billing capabilities.",
        ),
    ] = "7d",
    from_: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Timezone-aware ISO start; requires --to and replaces --range.",
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Timezone-aware ISO exclusive end; requires --from."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Filter by model.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Filter by provider.")
    ] = None,
    modality: Annotated[
        str | None, typer.Option("--modality", help="Filter by modality.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    service_tier: Annotated[
        str | None, typer.Option("--service-tier", help="Filter by service tier.")
    ] = None,
    provider_region: Annotated[
        str | None, typer.Option("--provider-region", help="Filter by provider region.")
    ] = None,
    agent_run: Annotated[
        str | None, typer.Option("--agent-run", help="Filter by agent run.")
    ] = None,
    failover_reason: Annotated[
        str | None, typer.Option("--failover-reason", help="Filter by failover reason.")
    ] = None,
    model_fallback: Annotated[
        bool | None,
        typer.Option(
            "--model-fallback/--no-model-fallback", help="Filter model fallback calls."
        ),
    ] = None,
    provider_fallback: Annotated[
        bool | None,
        typer.Option(
            "--provider-fallback/--no-provider-fallback",
            help="Filter provider fallback calls.",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Repeatable key=value filter (Team+); duplicate keys rejected.",
        ),
    ] = None,
) -> None:
    """Show aggregate project costs; use subcommands for events, totals, and export."""
    if context.invoked_subcommand is not None:
        if any(
            getattr(context.get_parameter_source(name), "name", None) == "COMMANDLINE"
            for name in context.params
        ):
            raise typer.BadParameter(
                "Place cost filters after the subcommand (for example: costs total --model MODEL)."
            )
        return
    effective_group_by = group_by
    if (
        account
        and getattr(context.get_parameter_source("group_by"), "name", None)
        != "COMMANDLINE"
    ):
        effective_group_by = "project"
    _run(
        context,
        "aggregate",
        group_by=effective_group_by,
        account=account,
        time_range=time_range,
        from_=from_,
        to=to,
        model=model,
        provider=provider,
        modality=modality,
        status=status,
        service_tier=service_tier,
        provider_region=provider_region,
        agent_run=agent_run,
        failover_reason=failover_reason,
        model_fallback=model_fallback,
        provider_fallback=provider_fallback,
        tag=tag,
    )


@app.command(
    "events",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/costs/events",
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.costs.events.v1",
    ),
)
def events(
    context: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    time_range: Annotated[
        str,
        typer.Option(
            "--range",
            help="Preset window; allowed ranges come from billing capabilities.",
        ),
    ] = "7d",
    from_: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Timezone-aware ISO start; requires --to and replaces --range.",
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Timezone-aware ISO exclusive end; requires --from."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Filter by model.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Filter by provider.")
    ] = None,
    modality: Annotated[
        str | None, typer.Option("--modality", help="Filter by modality.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    service_tier: Annotated[
        str | None, typer.Option("--service-tier", help="Filter by service tier.")
    ] = None,
    provider_region: Annotated[
        str | None, typer.Option("--provider-region", help="Filter by provider region.")
    ] = None,
    agent_run: Annotated[
        str | None, typer.Option("--agent-run", help="Filter by agent run.")
    ] = None,
    failover_reason: Annotated[
        str | None, typer.Option("--failover-reason", help="Filter by failover reason.")
    ] = None,
    model_fallback: Annotated[
        bool | None,
        typer.Option(
            "--model-fallback/--no-model-fallback", help="Filter model fallback calls."
        ),
    ] = None,
    provider_fallback: Annotated[
        bool | None,
        typer.Option(
            "--provider-fallback/--no-provider-fallback",
            help="Filter provider fallback calls.",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Repeatable key=value filter (Team+); duplicate keys rejected.",
        ),
    ] = None,
) -> None:
    """Show paginated events (Production+; limit 1 without cursor is an onboarding probe). Event rows omit call_id, provider fallback and failover reason; use raw export for failover detail."""
    _run(
        context,
        "events",
        limit=limit,
        cursor=cursor,
        time_range=time_range,
        from_=from_,
        to=to,
        model=model,
        provider=provider,
        modality=modality,
        status=status,
        service_tier=service_tier,
        provider_region=provider_region,
        agent_run=agent_run,
        failover_reason=failover_reason,
        model_fallback=model_fallback,
        provider_fallback=provider_fallback,
        tag=tag,
    )


@app.command(
    "total",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/costs/total",
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.costs.total.v1",
    ),
)
def total(
    context: typer.Context,
    time_range: Annotated[
        str,
        typer.Option(
            "--range",
            help="Preset window; allowed ranges come from billing capabilities.",
        ),
    ] = "7d",
    from_: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Timezone-aware ISO start; requires --to and replaces --range.",
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Timezone-aware ISO exclusive end; requires --from."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Filter by model.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Filter by provider.")
    ] = None,
    modality: Annotated[
        str | None, typer.Option("--modality", help="Filter by modality.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    service_tier: Annotated[
        str | None, typer.Option("--service-tier", help="Filter by service tier.")
    ] = None,
    provider_region: Annotated[
        str | None, typer.Option("--provider-region", help="Filter by provider region.")
    ] = None,
    agent_run: Annotated[
        str | None, typer.Option("--agent-run", help="Filter by agent run.")
    ] = None,
    failover_reason: Annotated[
        str | None, typer.Option("--failover-reason", help="Filter by failover reason.")
    ] = None,
    model_fallback: Annotated[
        bool | None,
        typer.Option(
            "--model-fallback/--no-model-fallback", help="Filter model fallback calls."
        ),
    ] = None,
    provider_fallback: Annotated[
        bool | None,
        typer.Option(
            "--provider-fallback/--no-provider-fallback",
            help="Filter provider fallback calls.",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Repeatable key=value filter (Team+); duplicate keys rejected.",
        ),
    ] = None,
) -> None:
    """Show uncapped project cost totals."""
    _run(
        context,
        "total",
        time_range=time_range,
        from_=from_,
        to=to,
        model=model,
        provider=provider,
        modality=modality,
        status=status,
        service_tier=service_tier,
        provider_region=provider_region,
        agent_run=agent_run,
        failover_reason=failover_reason,
        model_fallback=model_fallback,
        provider_fallback=provider_fallback,
        tag=tag,
    )


@app.command(
    "export",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/costs/export",
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="stream (CSV or NDJSON)",
    ),
)
def export(
    context: typer.Context,
    group_by: Annotated[
        str | None,
        typer.Option(
            "--group-by",
            help="Dimension from billing capabilities, or tag:<key> (Team+). Omit for raw export.",
        ),
    ] = None,
    export_format: Annotated[
        str,
        typer.Option("--format", help="csv or json (newline-delimited JSON / NDJSON)."),
    ] = "csv",
    time_range: Annotated[
        str,
        typer.Option(
            "--range",
            help="Preset window; allowed ranges come from billing capabilities.",
        ),
    ] = "30d",
    from_: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Timezone-aware ISO start; requires --to and replaces --range.",
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Timezone-aware ISO exclusive end; requires --from."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Filter by model.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Filter by provider.")
    ] = None,
    modality: Annotated[
        str | None, typer.Option("--modality", help="Filter by modality.")
    ] = None,
    status: Annotated[
        str | None, typer.Option("--status", help="Filter by status.")
    ] = None,
    service_tier: Annotated[
        str | None, typer.Option("--service-tier", help="Filter by service tier.")
    ] = None,
    provider_region: Annotated[
        str | None, typer.Option("--provider-region", help="Filter by provider region.")
    ] = None,
    agent_run: Annotated[
        str | None, typer.Option("--agent-run", help="Filter by agent run.")
    ] = None,
    failover_reason: Annotated[
        str | None, typer.Option("--failover-reason", help="Filter by failover reason.")
    ] = None,
    model_fallback: Annotated[
        bool | None,
        typer.Option(
            "--model-fallback/--no-model-fallback", help="Filter model fallback calls."
        ),
    ] = None,
    provider_fallback: Annotated[
        bool | None,
        typer.Option(
            "--provider-fallback/--no-provider-fallback",
            help="Filter provider fallback calls.",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Repeatable key=value filter (Team+); duplicate keys rejected.",
        ),
    ] = None,
) -> None:
    """Stream CSV or NDJSON bytes to stdout. Raw export requires Team; aggregated export requires Production. Window and truncation headers go to stderr."""
    if export_format not in {"csv", "json"}:
        raise typer.BadParameter("Must be csv or json (NDJSON).", param_hint="--format")
    _run(
        context,
        "export",
        group_by=group_by,
        export_format=export_format,
        time_range=time_range,
        from_=from_,
        to=to,
        model=model,
        provider=provider,
        modality=modality,
        status=status,
        service_tier=service_tier,
        provider_region=provider_region,
        agent_run=agent_run,
        failover_reason=failover_reason,
        model_fallback=model_fallback,
        provider_fallback=provider_fallback,
        tag=tag,
    )
