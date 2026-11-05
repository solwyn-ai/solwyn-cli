"""Cursor-based alert history, delivery receipts, and account spend alerts."""

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli._vendor.wire import (
    AccountAlertPreviewRequest,
    AccountAlertSnoozeRequest,
    AccountAlertUpdateRequest,
)
from solwyn_cli.auth import (
    AuthError,
    LoginRequiredError,
    LoginCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli import _mcp_operation as operation
from solwyn_cli.commands._alert_models import (
    AlertEventWindow as AlertEventWindow,
    AlertDeliveryLegEntry as AlertDeliveryLegEntry,
    AlertDeliveryStatus as AlertDeliveryStatus,
    AlertEventEntry,
    AlertEventListResponse,
)

from solwyn_cli.commands._cost_filters import window_params
from solwyn_cli.commands._shared import (
    exit_with_error,
    required_credential,
    with_project,
)
from solwyn_cli.commands.budget import _AlertRouting
from solwyn_cli.commands.budget_rules import parse_until
from solwyn_cli.commands.costs import report_window, stream_export
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, OutputFormat, TableData, _terminal_safe
from solwyn_cli.runtime import command_context, output_engine


class AlertQueryError(ValueError):
    """Invalid local history query."""


class AccountAlertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    spend_threshold_usd: float
    enabled: bool
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    routing: _AlertRouting
    created_at: datetime
    updated_at: datetime


class AccountAlertSnoozeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snoozed_until: datetime | None = None


def _help(
    endpoint: str,
    auth: str = "key or login; account requires login",
    schema: str = "solwyn.alerts.v1",
    *,
    not_found: bool = True,
) -> str:
    return help_contract(
        endpoint=endpoint,
        auth=auth,
        exits="0 success; 1 generic/API; 2 usage; 3 auth"
        + ("; 5 not found" if not_found else ""),
        schema=schema,
    )


_HISTORY_PATH = (
    "/api/v1/projects/{project_id}/alert-events or /api/v1/account/alert-events"
)
_ACCOUNT_PATH = "/api/v1/account/alerts"
app = typer.Typer(
    help="Inspect alert history and delivery receipts.",
    epilog=_help("GET " + _HISTORY_PATH),
)
account_app = typer.Typer(
    help="Manage account spend alerts.",
    epilog=_help(
        _ACCOUNT_PATH, "login; mutations require owner", "solwyn.alerts.account.v1"
    ),
)
app.add_typer(account_app, name="account")


def alert_params(
    *,
    time_range: str = "30d",
    from_: str | None = None,
    to: str | None = None,
    rule_type: str | None = None,
    scope: str | None = None,
) -> dict[str, str]:
    try:
        params = window_params(time_range=time_range, from_=from_, to=to)
    except ValueError as error:
        raise AlertQueryError(str(error)) from None
    if "range" in params and time_range not in {"7d", "30d", "90d"}:
        raise AlertQueryError("--range must be 7d, 30d, or 90d.")
    for name, value in {"rule_type": rule_type, "scope": scope}.items():
        if value is not None:
            if len(value) > 32 or "\0" in value:
                raise AlertQueryError(
                    "Alert filters allow at most 32 characters and no NUL."
                )
            params[name] = value
    return params


def history_path(project_id: str | None, account: bool) -> str:
    if account:
        return "/api/v1/account/alert-events"
    if not project_id:
        raise ValueError("project_required")
    return f"/api/v1/projects/{quote(project_id, safe='')}/alert-events"


def request_alert_events(
    *,
    client: SolwynClient,
    project_id: str | None = None,
    account: bool = False,
    cursor: str | None = None,
    all_pages: bool = False,
    failed_only: bool = False,
    event_id: str | None = None,
    **filters: Any,
) -> AlertEventListResponse:
    """Walk real list pages; failure and event-ID matching are local filters."""
    params = alert_params(**filters)
    path = history_path(project_id, account)
    if account and not isinstance(client.current_credential, LoginCredential):
        raise LoginRequiredError(
            "Account alert history requires a Solwyn login. Run solwyn login."
        )
    seen: set[str] = set()
    events: list[AlertEventEntry] = []
    first_window = None
    while True:
        operation.page(cursor)
        query = dict(params)
        if cursor is not None:
            if cursor in seen:
                raise ValueError("Alert history repeated a cursor.")
            seen.add(cursor)
            query["cursor"] = cursor
        page = AlertEventListResponse.model_validate(
            client.request("GET", path, params=query).json()
        )
        if not account and any(e.project_id != project_id for e in page.events):
            raise ValueError("Alert event project did not match the request.")
        if first_window is None:
            first_window = page.window
        else:
            first_window.retention_clamped |= page.window.retention_clamped
        selected = [
            e
            for e in page.events
            if (event_id is None or e.id == event_id)
            and (
                not failed_only
                or (
                    e.delivery is not None
                    and e.delivery.status in {"partial", "failed"}
                )
            )
        ]
        events.extend(selected)
        cursor = page.next_cursor
        operation.retain(
            AlertEventListResponse(
                events=events, next_cursor=cursor, window=first_window
            ),
            cursor=cursor,
        )
        if (
            not cursor
            or (event_id is not None and selected)
            or (not all_pages and event_id is None)
        ):
            return AlertEventListResponse(
                events=events, next_cursor=cursor, window=first_window
            )


@contextmanager
def _client(
    context: typer.Context, *, account: bool = False, project: bool = False
) -> Iterator[SolwynClient]:
    credential = required_credential(context, resolve=resolve_credential)
    if account and not isinstance(credential, LoginCredential):
        exit_with_error(
            context,
            OutputError(
                code="login_required",
                message="Account alerts require user login.",
                hint="Run solwyn login. Mutations require the account owner.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    if project and not account:
        with_project(context, credential)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            yield client
    except ApiError as error:
        if error.code == "unknown_notification_channel" and isinstance(
            error.detail, dict
        ):
            error.hint = "Unknown notification channel IDs: " + ", ".join(
                str(i) for i in error.detail.get("channel_ids", [])
            )
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
    except (ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_alerts_response",
                message="The alerts response was invalid.",
            ),
        )


def _table(events: list[AlertEventEntry], *, detail: bool = False) -> TableData:
    if detail:
        event = events[0]
        return TableData(
            columns=("channel", "name", "status", "attempts", "delivered at", "error"),
            rows=tuple(
                (
                    leg.channel_type,
                    leg.channel_name or "—",
                    leg.status,
                    str(leg.attempt_count),
                    leg.delivered_at.isoformat() if leg.delivered_at else "—",
                    leg.error or "—",
                )
                for leg in (event.delivery.legs if event.delivery else [])
            ),
            fold_cells=True,
        )
    return TableData(
        columns=("id", "emitted at", "rule type", "event", "delivery"),
        rows=tuple(
            (
                e.id,
                e.emitted_at.isoformat(),
                e.rule_type or "—",
                e.event_type,
                e.delivery.status if e.delivery else "—",
            )
            for e in events
        ),
        fold_cells=True,
    )


def _history(
    context: typer.Context,
    *,
    account: bool = False,
    event_id: str | None = None,
    **options: Any,
) -> None:
    runtime = command_context(context)
    try:
        alert_params(
            **{
                k: v
                for k, v in options.items()
                if k not in {"cursor", "all_pages", "failed_only"}
            }
        )
        history_path(runtime.project, account)
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(
                code="invalid_alert_query",
                message=str(error),
                hint="Choose --project for project history; provide paired timezone-aware --from/--to or --range 7d|30d|90d.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    with _client(context, account=account, project=True) as client:
        data = request_alert_events(
            client=client,
            project_id=command_context(context).project,
            account=account,
            event_id=event_id,
            **options,
        )
    report_window(data.window)
    if event_id is not None and not data.events:
        exit_with_error(
            context,
            OutputError(
                code="alert_event_not_found",
                message="No matching alert event in the requested window.",
                hint="Choose a wider --range or check --account.",
            ),
            semantic=ErrorSemantic.NOT_FOUND,
        )
    if (
        event_id is None
        and data.next_cursor
        and runtime.output_format is OutputFormat.TABLE
    ):
        typer.echo(
            _terminal_safe(
                "More events are available. Next cursor (pass with --cursor): "
                + json.dumps(data.next_cursor)
            ),
            err=True,
        )
    if event_id is not None:
        event = data.events[0]
        typer.echo(
            _terminal_safe(
                f"Event {event.id}; delivery: {event.delivery.status if event.delivery else 'unavailable'}."
            ),
            err=True,
        )
    output_engine(context).write_success(
        schema="solwyn.alerts.v1",
        data=(data.events[0] if event_id is not None else data).model_dump(mode="json"),
        table=_table(data.events, detail=event_id is not None),
    )


@app.command("list", epilog=_help("GET " + _HISTORY_PATH))
def list_events(
    context: typer.Context,
    account: Annotated[bool, typer.Option("--account")] = False,
    time_range: Annotated[str, typer.Option("--range")] = "30d",
    from_: Annotated[str | None, typer.Option("--from")] = None,
    to: Annotated[str | None, typer.Option("--to")] = None,
    rule_type: Annotated[str | None, typer.Option("--rule-type")] = None,
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            help="Rule scope in metadata (for example tag), not project/account.",
        ),
    ] = None,
    failed_only: Annotated[
        bool,
        typer.Option(
            "--failed-only", help="Locally include partial and failed delivery rollups."
        ),
    ] = False,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    all_pages: Annotated[
        bool,
        typer.Option(
            "--all", help="Walk all cursor pages; Core pages contain at most 25 events."
        ),
    ] = False,
) -> None:
    """List history. Delivery is Core's worst-leg rollup; JSON includes every leg."""
    _history(
        context,
        account=account,
        time_range=time_range,
        from_=from_,
        to=to,
        rule_type=rule_type,
        scope=scope,
        failed_only=failed_only,
        cursor=cursor,
        all_pages=all_pages,
    )


@app.command(
    "show",
    epilog=_help("GET " + _HISTORY_PATH + " (cursor walk; local event ID match)"),
)
def show_event(
    context: typer.Context,
    event_id: str,
    account: Annotated[bool, typer.Option("--account")] = False,
    time_range: Annotated[str, typer.Option("--range")] = "30d",
    from_: Annotated[str | None, typer.Option("--from")] = None,
    to: Annotated[str | None, typer.Option("--to")] = None,
    rule_type: Annotated[str | None, typer.Option("--rule-type")] = None,
    scope: Annotated[str | None, typer.Option("--scope")] = None,
) -> None:
    """Find an event across list pages bounded by the requested window and show delivery legs."""
    _history(
        context,
        event_id=event_id,
        account=account,
        time_range=time_range,
        from_=from_,
        to=to,
        rule_type=rule_type,
        scope=scope,
    )


@app.command(
    "export",
    epilog=_help(
        "GET " + _HISTORY_PATH.replace("alert-events", "alert-events/export"),
        schema="stream (CSV or NDJSON)",
    ),
)
def export_events(
    context: typer.Context,
    account: Annotated[bool, typer.Option("--account")] = False,
    export_format: Annotated[
        str, typer.Option("--format", help="csv or json (NDJSON).")
    ] = "csv",
    time_range: Annotated[str, typer.Option("--range")] = "30d",
    from_: Annotated[str | None, typer.Option("--from")] = None,
    to: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Stream CSV or NDJSON (Production+). Delivery receipts are excluded. Window headers go to stderr."""
    try:
        params = alert_params(time_range=time_range, from_=from_, to=to)
        path = history_path(command_context(context).project, account)
        if export_format not in {"csv", "json"}:
            raise ValueError("--format must be csv or json (NDJSON).")
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    params["format"] = export_format
    typer.echo("Alert exports exclude delivery receipts.", err=True)
    with _client(context, account=account, project=True) as client:
        path = history_path(command_context(context).project, account)
        stream_export(client=client, path=path + "/export", params=params)


def _account_help(method: str, suffix: str = "", *, owner: bool = False) -> str:
    return _help(
        method + " " + _ACCOUNT_PATH + suffix,
        "login owner" if owner else "login; any account member",
        "solwyn.alerts.account.v1",
    )


def _account(
    context: typer.Context, operation: str, body: BaseModel | None = None
) -> None:
    method = {"show": "GET", "set": "PUT", "delete": "DELETE"}.get(operation, "POST")
    suffix = "/" + operation if operation in {"snooze", "unsnooze", "preview"} else ""
    with _client(context, account=True) as client:
        response = client.request(
            method,
            _ACCOUNT_PATH + suffix,
            **(
                {"json": body.model_dump(mode="json", exclude_unset=True)}
                if body is not None
                else {}
            ),
        )
        if operation == "delete":
            if response.status_code != 204:
                raise ValueError("Expected no-content deletion response.")
            payload = {"deleted": True}
        else:
            model = (
                AccountAlertResponse
                if operation in {"show", "set"}
                else _AlertRouting
                if operation == "preview"
                else AccountAlertSnoozeResponse
            )
            payload = model.model_validate(response.json()).model_dump(mode="json")
    output_engine(context).write_success(
        schema="solwyn.alerts.account.v1", data=payload, table=_account_table(payload)
    )


def _account_table(payload: dict[str, Any]) -> TableData:
    return TableData(
        columns=("field", "value"),
        rows=tuple(
            (
                key,
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (list, dict))
                else "—"
                if value is None
                else str(value),
            )
            for key, value in payload.items()
        ),
        fold_cells=True,
    )


@account_app.command("show", epilog=_account_help("GET"))
def account_show(
    context: typer.Context,
    with_last_fired: Annotated[bool, typer.Option("--with-last-fired")] = False,
) -> None:
    """Show the configured account spend alert (404 if absent)."""
    if not with_last_fired:
        _account(context, "show")
        return
    from solwyn_cli.commands._alert_reads import request_account_alert

    with _client(context, account=True) as client:
        data = request_account_alert(client=client, with_last_fired=True)
    output_engine(context).write_success(
        schema="solwyn.alerts.account.v1",
        data=data,
        table=_account_table(data.model_dump(mode="json")),
    )


@account_app.command("set", epilog=_account_help("PUT", owner=True))
def account_set(
    context: typer.Context,
    spend: Annotated[float, typer.Option("--spend", min=0)],
    channel: Annotated[
        list[str] | None,
        typer.Option(
            "--channel",
            help="Repeat to replace channels; omit to preserve existing channels.",
        ),
    ] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
) -> None:
    """Create or update the account spend alert (Production+; owner only)."""
    if not math.isfinite(spend) or spend <= 0:
        raise typer.BadParameter("--spend must be a finite positive USD amount.")
    values: dict[str, Any] = {"spend_threshold_usd": spend}
    if channel is not None:
        values["channel_ids"] = channel
    if enabled is not None:
        values["enabled"] = enabled
    _account(context, "set", AccountAlertUpdateRequest(**values))


@account_app.command("delete", epilog=_account_help("DELETE", owner=True))
def account_delete(context: typer.Context) -> None:
    """Delete the account spend alert; succeeds when already absent."""
    _account(context, "delete")


@account_app.command("snooze", epilog=_account_help("POST", "/snooze", owner=True))
def account_snooze(
    context: typer.Context, until: Annotated[str, typer.Option("--until")]
) -> None:
    """Snooze to an absolute timezone-aware timestamp (or +2h)."""
    try:
        body = AccountAlertSnoozeRequest(snoozed_until=parse_until(until))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    _account(context, "snooze", body)


@account_app.command("unsnooze", epilog=_account_help("POST", "/unsnooze", owner=True))
def account_unsnooze(context: typer.Context) -> None:
    """Clear the account spend alert snooze."""
    _account(context, "unsnooze")


@account_app.command("preview", epilog=_account_help("POST", "/preview"))
def account_preview(
    context: typer.Context,
    channel: Annotated[list[str] | None, typer.Option("--channel")] = None,
) -> None:
    """Preview unsaved account routing; ungated and available to any member."""
    _account(context, "preview", AccountAlertPreviewRequest(channel_ids=channel or []))


@app.command(
    "activity",
    epilog=_help(
        "GET /api/v1/account/alerts/activity",
        "login; any account member",
        "solwyn.alerts.activity.v1",
        not_found=False,
    ),
)
def activity(
    context: typer.Context,
    time_range: Annotated[str, typer.Option("--range")] = "30d",
    event_type: Annotated[str | None, typer.Option("--event-type")] = None,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    all_pages: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """Read account-wide activity across every project and account scope."""
    from solwyn_cli.commands._alert_reads import (
        activity_params,
        request_account_alert_page,
    )

    try:
        activity_params(time_range, event_type)
    except AlertQueryError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_alert_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    with _client(context, account=True) as client:
        data = request_account_alert_page(
            client=client,
            time_range=time_range,
            event_type=event_type,
            cursor=cursor,
            all_pages=all_pages,
        )
    report_window(data.window)
    _write_account_events(context, data, "solwyn.alerts.activity.v1")


def _write_account_events(context: typer.Context, data: BaseModel, schema: str) -> None:
    if (
        data.next_cursor
        and command_context(context).output_format is OutputFormat.TABLE
    ):
        typer.echo(
            _terminal_safe(
                "More events are available. Next cursor (pass with --cursor): "
                + json.dumps(data.next_cursor)
            ),
            err=True,
        )
    output_engine(context).write_success(
        schema=schema, data=data, table=_table(data.events)
    )


@app.command(
    "pending",
    epilog=_help(
        "GET /api/v1/account/alerts/open-deliveries",
        "login; any account member",
        "solwyn.alerts.pending.v1",
        not_found=False,
    ),
)
def pending(
    context: typer.Context,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    all_pages: Annotated[bool, typer.Option("--all")] = False,
    fail_if_open: Annotated[
        bool,
        typer.Option(
            "--fail-if-open",
            help="Exit 1 after printing data when open deliveries exist.",
        ),
    ] = False,
) -> None:
    """Read pending/retrying deliveries, regardless of event retention."""
    from solwyn_cli.commands._alert_reads import request_account_alert_page

    with _client(context, account=True) as client:
        data = request_account_alert_page(
            client=client, pending=True, cursor=cursor, all_pages=all_pages
        )
    _write_account_events(context, data, "solwyn.alerts.pending.v1")
    if fail_if_open and data.events:
        exit_with_error(
            context,
            OutputError(
                code="open_alert_deliveries",
                message="Open alert deliveries remain.",
                hint="Inspect delivery attempts and next_attempt_at; retry this read after delivery processing.",
            ),
        )
