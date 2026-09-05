"""Project cost analytics, paginated events, totals, and streamed exports."""

import json
import sys
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import quote

import typer
from pydantic import ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands._cost_filters import cost_params, validate_group_by
from solwyn_cli.commands._cost_models import (
    CostQueryResponse,
    CostEventsResponse,
    CostTotalResponse,
    CostWindow,
)
from solwyn_cli.commands._entitlement import get_capabilities
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, write_byte_stream
from solwyn_cli.runtime import command_context, output_engine


class CostRange(StrEnum):
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    YEAR = "365d"


CostsData = CostQueryResponse

COSTS_HELP = help_contract(
    endpoint="GET /api/v1/projects/{project}/costs",
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.costs.v1",
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
            "Truncated",
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
    **filters: Any,
) -> None:
    runtime = command_context(context)
    if runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint="Choose a project with --project, SOLWYN_PROJECT_ID, or config default_project.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        validate_group_by(group_by)
        params = cost_params(**filters)
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_cost_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    entitlement_hints: list[str] = []
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            capabilities = get_capabilities(client=client)
            if capabilities is not None:
                analytics = capabilities.analytics
                # Entitlements distinguish allowed values from paid values, not
                # invalid dimensions. Core must decide 400 versus 402.
                if group_by is not None and not (
                    group_by in analytics.allowed_group_by
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
            path = f"/api/v1/projects/{quote(runtime.project, safe='')}/costs"
            if operation == "export":
                params["format"] = export_format
                if group_by is not None:
                    params["group_by"] = group_by
                stream_export(client=client, path=path + "/export", params=params)
                return
            if operation == "events":
                params["limit"] = limit
                if cursor is not None:
                    params["cursor"] = cursor
                data = CostEventsResponse.model_validate(
                    client.request("GET", path + "/events", params=params).json()
                )
                if any(item.project_id != runtime.project for item in data.items):
                    raise ValueError("Costs event project did not match the request.")
                table = _events_table(data)
            elif operation == "total":
                data = fetch_cost_total(
                    client=client, project_id=runtime.project, **filters
                )
                table = _total_table(data)
            else:
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
    if isinstance(data, CostsData) and data.truncated:
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
        "solwyn.costs.v1"
        if operation == "aggregate"
        else f"solwyn.costs.{operation}.v1"
    )
    payload = data.model_dump(mode="json")
    if payload.get("savings", False) is None:
        payload.pop("savings")
    output_engine(context).write_success(schema=schema, data=payload, table=table)


@app.callback()
def costs(
    context: typer.Context,
    group_by: Annotated[
        str | None,
        typer.Option(
            "--group-by",
            help="Dimension from billing capabilities, or tag:<key> (Team+). Omit for raw export.",
        ),
    ] = "model",
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
    _run(
        context,
        "aggregate",
        group_by=group_by,
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
