"""Account audit trail command."""

import json
from enum import StrEnum
from typing import Annotated

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from solwyn_cli import _mcp_operation as operation
from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, OutputFormat, TableData
from solwyn_cli.runtime import command_context, output_engine


class AuditRange(StrEnum):
    DAYS_7 = "7d"
    DAYS_30 = "30d"
    DAYS_90 = "90d"
    DAYS_365 = "365d"


AUDIT_ACTIONS = (
    "account.created",
    "account.updated",
    "team.member_removed",
    "team.member_role_changed",
    "team.invitation_created",
    "team.invitation_revoked",
    "project.created",
    "project.updated",
    "project.archived",
    "budget.defaults_restored",
    "budget.updated",
    "api_key.rotated",
    "api_key.deleted",
    "agent_run.stopped",
    "billing.plan_intent_deleted",
    "billing.plan_changed",
    "billing.cancel_scheduled",
    "billing.cancel_undone",
    "billing.plan_change_discarded",
    "notification_channel.created",
    "notification_channel.updated",
    "notification_channel.deleted",
    "account_phone.verified",
    "account_phone.deleted",
    "consent.sms_alerts_changed",
    "upserted",
)


AUDIT_HELP = help_contract(
    endpoint="GET /api/v1/account/audit-events",
    auth="JWT",
    exits="0 success; 1 generic/API; 2 usage; 3 auth",
    schema="solwyn.audit.v1",
    guidance=(
        "Actions are exact-match strings; use --all to follow every returned cursor.",
        f"Known actions: {', '.join(AUDIT_ACTIONS)}.",
        "Rule add/remove is not audited; only budget.updated is in this trail. Use alerts list --rule-type for per-rule lifecycle events.",
    ),
)


class _AuditQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    range: AuditRange = AuditRange.DAYS_30
    action: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    cursor: str | None = None


class AuditEventEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    account_id: str
    actor_user_id: str
    action: str
    target_type: str
    target_id: str
    detail: dict[str, object] = Field(default_factory=dict)
    created_at: AwareDatetime


class AuditEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    events: list[AuditEventEntry]
    next_cursor: str | None
    has_more: bool

    @model_validator(mode="after")
    def validate_cursor_state(self) -> "AuditEventListResponse":
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("Audit cursor state is inconsistent")
        return self


def _jwt_credential(context: typer.Context) -> JwtCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry audit."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry audit."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to read the account audit trail.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def _request_params(query: _AuditQuery) -> dict[str, str]:
    params = {"range": query.range.value}
    if query.action is not None:
        params["action"] = query.action
    if query.cursor is not None:
        params["cursor"] = query.cursor
    return params


def _date_time(value: AwareDatetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _audit_table(data: AuditEventListResponse) -> TableData:
    rows: list[tuple[str, str]] = []
    for event in data.events:
        rows.extend(
            (
                ("Event", event.id),
                ("Created", _date_time(event.created_at)),
                ("Account", event.account_id),
                ("Actor", event.actor_user_id),
                ("Action", event.action),
                ("Target", f"{event.target_type}:{event.target_id}"),
                (
                    "Detail",
                    json.dumps(
                        event.detail,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def _invalid_audit_response(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="invalid_audit_response",
            message="The audit response was invalid.",
        ),
    )


def audit(
    context: typer.Context,
    range_: Annotated[
        AuditRange,
        typer.Option("--range", help="Audit window."),
    ] = AuditRange.DAYS_30,
    action: Annotated[
        str | None,
        typer.Option("--action", help="Exact audit action to match."),
    ] = None,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Opaque cursor from the preceding page."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", help="Follow cursors until all remaining events are read."
        ),
    ] = False,
) -> None:
    """List account audit events newest first."""
    try:
        _AuditQuery(range=range_, action=action, cursor=cursor)
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_audit_query",
                message="The audit query is invalid.",
                hint="Use a supported range and an action from 1 through 100 characters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _jwt_credential(context)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = fetch_audit_events(
                client=client,
                time_range=range_,
                action=action,
                cursor=cursor,
                all_pages=all_,
            )
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_audit_response(context)

    output_engine(context).write_success(
        schema="solwyn.audit.v1",
        data=data,
        table=_audit_table(data),
    )
    if runtime.output_format is OutputFormat.TABLE and data.has_more:
        typer.echo("More audit events are available.", err=True)
        typer.echo(
            f"Next cursor (JSON escaped): {json.dumps(data.next_cursor)}",
            err=True,
        )
        typer.echo(
            "Run `solwyn audit --all` to fetch all remaining events.",
            err=True,
        )


def fetch_audit_events(
    *,
    client: SolwynClient,
    time_range: AuditRange = AuditRange.DAYS_30,
    action: str | None = None,
    cursor: str | None = None,
    all_pages: bool = False,
) -> AuditEventListResponse:
    query = _AuditQuery(range=time_range, action=action, cursor=cursor)
    if not isinstance(client.current_credential, JwtCredential):
        raise AuthError("Account audit requires JWT.")
    events = []
    seen = set()
    while True:
        operation.page(query.cursor)
        if query.cursor is not None:
            if query.cursor in seen:
                raise ValueError("Audit pagination repeated a cursor.")
            seen.add(query.cursor)
        response = client.request(
            "GET", "/api/v1/account/audit-events", params=_request_params(query)
        )
        if response.status_code != 200:
            raise ValueError("Audit request did not return 200")
        page = AuditEventListResponse.model_validate(response.json())
        events.extend(page.events)
        data = AuditEventListResponse(
            events=events, next_cursor=page.next_cursor, has_more=page.has_more
        )
        operation.retain(data, cursor=page.next_cursor)
        if not all_pages or not page.has_more:
            return data
        query = query.model_copy(update={"cursor": page.next_cursor})
