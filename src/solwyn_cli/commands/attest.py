"""Stream spend-control attestation exports."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict

from solwyn_cli.auth import (
    AuthError,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._cost_filters import window_params
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands.costs import stream_export
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError
from solwyn_cli.runtime import command_context


class AttestationFormat(StrEnum):
    """Formats Core can emit for an attestation export."""

    JSON = "json"
    CSV = "csv"


class AttestationPresetRange(StrEnum):
    """Preset attestation windows accepted by Core."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    YEAR = "365d"


AttestationRange = Literal["7d", "30d", "90d", "365d", "custom"]


class _AttestationModel(BaseModel):
    """Strict base for every attestation response object."""

    model_config = ConfigDict(extra="forbid")


class AttestationScope(_AttestationModel):
    type: Literal["account", "project"]
    account_id: str
    project_id: str | None


class AttestationWindow(_AttestationModel):
    requested_from: datetime
    requested_to: datetime
    effective_from: datetime
    effective_to: datetime
    retention_clamped: bool


class AttestationSpendSummary(_AttestationModel):
    real_spend_usd: float
    real_call_count: int
    denied_spend_usd: float
    denial_count: int


class AttestationBudget(_AttestationModel):
    project_id: str
    project_name: str
    archived: bool
    archived_at: datetime | None
    budget_limit_usd: float
    budget_period: str
    budget_mode: str
    sub_period: str | None
    sub_limit_usd: float | None
    rules: list[dict[str, Any]]


class AttestationAlertEvent(_AttestationModel):
    id: str
    project_id: str | None
    account_id: str
    scope: str
    rule_key: str
    emitted_at: datetime
    event_type: str
    rule_type: str | None
    value_at_event: float | None
    threshold_at_event: float | None
    period_key: str | None
    linked_event_ids: list[str]
    metadata_json: dict[str, Any]


class AttestationAlerts(_AttestationModel):
    begins_at: datetime | None
    delivery_evidence_available: bool
    delivered_count: int | None
    coverage_note: str
    events: list[AttestationAlertEvent]


class AttestationAccountSpendAlert(_AttestationModel):
    evidence_available: bool
    configured: bool | None
    spend_threshold_usd: float | None
    enabled: bool | None
    snoozed_until: datetime | None
    channel_ids: list[str]
    coverage_note: str


class AttestationAuditEvent(_AttestationModel):
    id: str
    account_id: str
    actor_user_id: str | None
    action: str
    target_type: str
    target_id: str
    detail: dict[str, Any]
    created_at: datetime


class AttestationSubscriptionSnapshot(_AttestationModel):
    tier: str
    billing_interval: str
    status: str
    pending_update_exists: bool
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    pause_collection_exists: bool
    grace_period_until: datetime | None
    raw_status_at: datetime
    created_at: datetime
    updated_at: datetime


class AttestationSubscription(_AttestationModel):
    current_tier: str | None
    snapshot: AttestationSubscriptionSnapshot | None
    changes: list[AttestationAuditEvent]
    coverage_note: str


class AttestationAudit(_AttestationModel):
    begins_at: datetime | None
    coverage_note: str
    events: list[AttestationAuditEvent]


class SpendControlAttestation(_AttestationModel):
    schema_version: Literal["1"]
    generated_at: datetime
    scope: AttestationScope
    range: AttestationRange
    window: AttestationWindow
    spend_summary: AttestationSpendSummary
    budgets: list[AttestationBudget]
    alerts: AttestationAlerts
    account_spend_alert: AttestationAccountSpendAlert
    subscription: AttestationSubscription
    audit: AttestationAudit


ATTEST_HELP = help_contract(
    endpoint=(
        "GET /api/v1/projects/{project}/spend-control-attestation or "
        "GET /api/v1/account/spend-control-attestation"
    ),
    auth="project: key or JWT; account: JWT",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="stream (SpendControlAttestation JSON or section,field,value CSV)",
)


def attest(
    context: typer.Context,
    account: Annotated[
        bool,
        typer.Option("--account", help="Export account-wide evidence (requires JWT)."),
    ] = False,
    time_range: Annotated[
        AttestationPresetRange,
        typer.Option(
            "--range",
            help="Preset window; custom --from/--to replace it.",
        ),
    ] = AttestationPresetRange.THIRTY_DAYS,
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
    export_format: Annotated[
        AttestationFormat,
        typer.Option("--format", help="Export format: json or csv."),
    ] = AttestationFormat.JSON,
) -> None:
    """Stream a project or account spend-control attestation to stdout."""
    runtime = command_context(context)
    if not account and runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required for a project attestation.",
                hint="Choose a project with --project, SOLWYN_PROJECT_ID, or config default_project; use --account for account-wide evidence.",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    try:
        params = window_params(
            time_range=time_range.value,
            from_=from_,
            to=to,
        )
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_attestation_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    params["format"] = export_format.value

    credential = required_credential(context, resolve=resolve_credential)
    if account and isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry attest --account."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry attest --account."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required for account attestations.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )

    path = (
        "/api/v1/account/spend-control-attestation"
        if account
        else (
            f"/api/v1/projects/{quote(runtime.project or '', safe='')}"
            "/spend-control-attestation"
        )
    )
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            stream_export(client=client, path=path, params=params)
    except ApiError as error:
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
