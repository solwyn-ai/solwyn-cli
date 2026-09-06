"""Project budget inspection and pre-flight guardrail commands."""

import json
import math
import os
import sys
from pathlib import Path
from uuid import uuid4
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
    model_validator,
)

from solwyn_cli._vendor.wire import (
    BudgetCheckRequest,
    BudgetCheckBatchRequest,
    BudgetConfirmRequest,
    BudgetMode,
    BudgetPeriod,
    BudgetUpdateRequest,
    MediaUsage,
    IngestMetadataEvent,
    ProviderName,
    ServiceTier,
    TokenDetails,
)
from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._record import (
    CALL_ID_HINT,
    RecordingResult,
    build_event,
    record_best_effort,
    validate_call_id,
)
from solwyn_cli.commands._tags import parse_tags, validate_tags
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, ExitCode
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(
    help="Inspect budgets and run pre-flight guardrail checks.",
    epilog=help_contract(
        endpoint="composite router for budget subcommands",
        auth="varies by subcommand",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
        guidance=(
            "Use status to poll, check immediately before un-instrumented spend, "
            "and confirm only a non-null reservation.",
        ),
    ),
)

_FAIL_OPEN_NOTICE = "fail-open, unreserved — skip confirm"
_CONFIRM_NOT_FOUND_HINT = (
    "Expired or already consumed. Run a fresh check before spend; confirm once."
)
# Budget check and confirm are the only CLI surfaces the API can reject for key
# scope, so the hint lives with them rather than in the shared error machinery.
# The server message already states the key is read-only; the hint is remedy only.
_READ_ONLY_KEY_HINT = (
    "Set SOLWYN_API_KEY to a write-scoped key, or run solwyn login and "
    "solwyn keys rotate --scope full to replace this project's key with one."
)


def _read_only_key_hinted(error: ApiError) -> ApiError:
    """Make a read-only-key rejection actionable without altering other errors."""
    if error.status_code != 403 or error.code != "read_only_key":
        return error
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        hint=_READ_ONLY_KEY_HINT,
        detail=error.detail,
    )


class _RoutingChannel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    name: str


class _RoutingMuted(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    reason: Literal["severity_floor", "event_filter", "email_off"]
    severity_floor: str | None = None


class _AlertRouting(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    will_send: bool
    email: str | None = None
    channels: list[_RoutingChannel] = Field(default_factory=list)
    source: Literal["override", "project_default", "account_default"]
    muted: _RoutingMuted | None = None


class _ThresholdStateBase(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    rule_key: str
    channel_ids: list[str]
    routing: _AlertRouting
    snoozed_until: datetime | None = None
    paused: bool = False


class _BudgetThresholdState(_ThresholdStateBase):
    type: Literal["budget_threshold"]
    percent: Annotated[int, Field(ge=1, le=100)]


class _ForecastOverrunState(_ThresholdStateBase):
    type: Literal["forecast_overrun"]
    spend_threshold_usd: Annotated[FiniteFloat, Field(gt=0)]
    period: Literal["month"] = "month"


class _RunawayRunState(_ThresholdStateBase):
    mode: BudgetMode = BudgetMode.ALERT_ONLY
    type: Literal["runaway_run"]
    spend_threshold_usd: Annotated[FiniteFloat, Field(gt=0, le=1999999.99)]
    call_threshold: Annotated[int, Field(ge=1)] | None = None
    window_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None


class _DailyAnomalyState(_ThresholdStateBase):
    type: Literal["daily_anomaly"]
    multiplier: Annotated[FiniteFloat, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7


class _UnitCostAnomalyState(_ThresholdStateBase):
    type: Literal["unit_cost_anomaly"]
    multiplier: Annotated[FiniteFloat, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7


class _ScopedBudgetState(_ThresholdStateBase):
    type: Literal["scoped_budget"]
    scope: Literal["model", "provider", "agent_run", "tag"]
    match_value: Annotated[str, Field(max_length=256)] | None = None
    current_usage: FiniteFloat | None
    match: Annotated[str, Field(min_length=1, max_length=2048)]
    limit: Annotated[FiniteFloat, Field(gt=0, le=999999.999999)]
    mode: BudgetMode


_ThresholdState = Annotated[
    _BudgetThresholdState
    | _ForecastOverrunState
    | _RunawayRunState
    | _DailyAnomalyState
    | _UnitCostAnomalyState
    | _ScopedBudgetState,
    Field(discriminator="type"),
]


class _BudgetConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    allowed_modes: list[BudgetMode]
    allowed_periods: list[BudgetPeriod]
    sub_period_enabled: bool
    max_thresholds: int


class _BudgetStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    project_id: str
    budget_limit: FiniteFloat
    current_usage: FiniteFloat
    remaining_budget: FiniteFloat
    utilization_pct: FiniteFloat
    budget_mode: BudgetMode
    budget_period: BudgetPeriod
    period_date: str
    thresholds: list[_ThresholdState]
    sub_period: BudgetPeriod | None = None
    sub_limit: FiniteFloat | None = None
    constraints: _BudgetConstraints
    outage_overspend_ceiling_usd: FiniteFloat
    lease_float_usd: FiniteFloat | None = None


BudgetStatusData = _BudgetStatusData


def fetch_budget_status(
    *, api_url: str, credential: JwtCredential | ProjectKeyCredential, project_id: str
) -> BudgetStatusData:
    """Fetch and validate one project's pure-read budget status."""
    with SolwynClient(api_url=api_url, credential=credential) as client:
        return request_budget_status(client=client, project_id=project_id)


def request_budget_status(*, client: SolwynClient, project_id: str) -> BudgetStatusData:
    """Read and validate budget status through an existing shared client."""
    project_path = quote(project_id, safe="")
    response = client.request("GET", f"/api/v1/projects/{project_path}/budget")
    data = _BudgetStatusData.model_validate(response.json())
    if data.project_id != project_id:
        raise ValueError("Budget response project did not match the request.")
    return data


class _BudgetStatusOutput(BaseModel):
    outage_overspend_ceiling_usd: FiniteFloat
    lease_float_usd: FiniteFloat | None = None
    thresholds: list[_ThresholdState]
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    project_id: str
    budget_limit: FiniteFloat
    current_usage: FiniteFloat
    remaining_budget: FiniteFloat
    utilization_pct: FiniteFloat
    budget_mode: BudgetMode
    budget_period: BudgetPeriod


class _BudgetSetData(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    project_id: str
    budget_limit: FiniteFloat
    current_usage: FiniteFloat
    remaining_budget: FiniteFloat
    budget_period: BudgetPeriod
    budget_mode: BudgetMode
    period_date: str
    utilization_pct: FiniteFloat
    sub_period: BudgetPeriod | None
    sub_limit: FiniteFloat | None
    thresholds: list[dict[str, object]]


class _BudgetCheckData(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    allowed: bool
    remaining_budget: FiniteFloat
    reservation_id: Annotated[str, Field(min_length=1)] | None = None
    mode: BudgetMode
    budget_limit: FiniteFloat
    current_usage: FiniteFloat
    denied_by_period: str | None = None
    call_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    recorded: RecordingResult | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    project_id: str
    price_hints: dict[ProviderName, FiniteFloat] | None = None
    run_control: "_RunControlDirective | None" = None
    reason: Literal["run_terminated"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_guardrail_outcome(self) -> "_BudgetCheckData":
        """Reject contradictory reservation and denial states."""
        if not self.allowed and self.reservation_id is not None:
            raise ValueError("Denied checks cannot carry a reservation")
        if not self.allowed and self.mode is not BudgetMode.HARD_DENY:
            raise ValueError("Only hard-deny mode can deny a check")
        if self.allowed and self.denied_by_period is not None:
            raise ValueError("Allowed checks cannot carry a denial period")
        if self.run_control is not None and (
            self.allowed or self.denied_by_period != "run_stopped"
        ):
            raise ValueError("Run-control directives require a stopped-run denial")
        return self


class _RunControlDirective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: Literal["1"]
    action: Literal["terminate"]
    agent_run_id: Annotated[str, Field(max_length=256)]
    reason: Literal["manual_kill"]


class _BudgetConfirmData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_id: str
    call_id: str
    recorded: RecordingResult | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


BudgetCheckData = _BudgetCheckData
BudgetConfirmData = _BudgetConfirmData


def request_budget_check(
    *, client: SolwynClient, request: BudgetCheckRequest
) -> BudgetCheckData:
    """Reserve and validate one caller-made un-instrumented spend."""
    response = client.request(
        "POST",
        "/api/v1/budgets/check",
        # Unset optional fields are omitted rather than sent as explicit nulls:
        # an omitted agent_run_id is the API's project-only default, so callers
        # that do not opt in keep posting the exact body they post today.
        json=request.model_dump(mode="json", exclude_none=True),
    )
    data = _BudgetCheckData.model_validate(response.json())
    return validate_check_directive(request=request, data=data)


def validate_check_directive(
    *, request: BudgetCheckRequest, data: BudgetCheckData
) -> BudgetCheckData:
    """Validate the same opt-in directive for single and batch admission."""
    if request.run_directive_version is not None:
        if data.denied_by_period == "run_stopped":
            if data.run_control is None:
                raise ValueError("Stopped-run denial omitted the requested directive")
            if data.run_control.agent_run_id != request.agent_run_id:
                raise ValueError("Run-control directive did not echo the requested run")
            data = data.model_copy(update={"reason": "run_terminated"})
        elif data.run_control is not None:
            raise ValueError("Unexpected run-control directive")
    return data


class BudgetCheckBatchItem(BaseModel):
    """Batch envelopes retain nullable result/error fields unlike single checks."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    result: BudgetCheckData | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "BudgetCheckBatchItem":
        if self.result is not None:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("Batch item mixes a result and an error")
        elif not self.error_code or not self.error_message:
            raise ValueError("Batch item requires a result or complete error")
        return self


class _BudgetCheckBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    results: list[BudgetCheckBatchItem]


class BudgetCheckBatchData(_BudgetCheckBatchResponse):
    failed_chunk_start: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    total_checks: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


BATCH_FAILURE_HINT = (
    "Completed results remain valid; do not replay completed checks. "
    "Reservation state within the failed chunk is unknown; later chunks were not submitted."
)


class BudgetCheckBatchFailure(Exception):
    """Keep known reservations available when a later chunk fails."""

    def __init__(self, cause: Exception, data: BudgetCheckBatchData) -> None:
        super().__init__("Batch check failed after completed chunks")
        self.cause = cause
        self.data = data


def prepare_batch_checks(checks: object) -> list[BudgetCheckRequest]:
    """Validate the entire input before admitting any chunk."""
    if not isinstance(checks, list) or not checks:
        raise ValueError("Batch input must be a non-empty JSON array of check bodies")
    prepared = []
    for index, raw in enumerate(checks):
        if not isinstance(raw, dict):
            raise ValueError(f"Check {index} must be an object")
        body = dict(raw)
        if "agent_run_id" not in body:
            body["agent_run_id"] = os.environ.get("SOLWYN_AGENT_RUN_ID")
        body["tags"] = validate_tags(body["tags"]) if "tags" in body else parse_tags()
        # Validate supplied versions before applying the CLI's opt-ins.
        request = BudgetCheckRequest.model_validate(body)
        if len(request.fallback_models) != len(request.fallback_providers):
            raise ValueError(f"Check {index} fallback providers and models must align")
        prepared.append(
            request.model_copy(
                update={
                    "price_hints_version": "1",
                    "run_directive_version": "1"
                    if request.agent_run_id is not None
                    else None,
                }
            )
        )
    return prepared


def request_budget_check_batch(
    *,
    client: SolwynClient,
    checks: list[BudgetCheckRequest],
    no_record: bool = False,
    estimated_output_tokens: int | None = None,
) -> BudgetCheckBatchData:
    """Admit chunks in order, then record each validated denial exactly once."""
    invocation_id = str(uuid4())
    results = []
    for offset in range(0, len(checks), 32):
        batch = BudgetCheckBatchRequest(checks=checks[offset : offset + 32])
        try:
            response = client.request(
                "POST",
                "/api/v1/budgets/check/batch",
                json=batch.model_dump(mode="json", exclude_none=True),
            )
            data = _BudgetCheckBatchResponse.model_validate(response.json())
            if len(data.results) != len(batch.checks):
                raise ValueError("Batch result count does not match submitted checks")
            # Validate the complete chunk before any receipt side effect.
            for request, item in zip(batch.checks, data.results, strict=True):
                if item.result is not None:
                    item.result = validate_check_directive(
                        request=request, data=item.result
                    )
        except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
            if results:
                raise BudgetCheckBatchFailure(
                    error,
                    BudgetCheckBatchData(
                        results=results,
                        failed_chunk_start=offset,
                        total_checks=len(checks),
                    ),
                ) from error
            raise
        for request, item in zip(batch.checks, data.results, strict=True):
            if item.result is not None:
                item.result = record_denial(
                    client=client,
                    request=request,
                    data=item.result,
                    sdk_instance_id=invocation_id,
                    no_record=no_record,
                    estimated_output_tokens=estimated_output_tokens,
                )
        results.extend(data.results)
    return BudgetCheckBatchData(results=results)


def _batch_check_table(data: BudgetCheckBatchData) -> TableData:
    return TableData(
        columns=(
            "Index",
            "Outcome",
            "Details",
            "Call ID",
            "Recorded",
        ),
        rows=tuple(
            (
                index,
                "allowed" if item.result.allowed else "denied",
                item.result.reservation_id
                or (
                    "run stopped by an operator — do not retry under a new run id"
                    if item.result.reason == "run_terminated"
                    else item.result.denied_by_period or "fail-open"
                ),
                item.result.call_id or "-",
                item.result.recorded.status if item.result.recorded else "-",
            )
            if item.result is not None
            else (index, item.error_code, item.error_message, "-", "-")
            for index, item in enumerate(data.results)
        ),
        fold_cells=True,
    )


def _check_batch_command(
    context: typer.Context,
    batch: str,
    *,
    no_record: bool,
    estimated_output_tokens: int | None,
) -> None:
    try:
        raw = (
            sys.stdin.read()
            if batch == "-"
            else Path(batch).read_text(encoding="utf-8")
        )
        checks = prepare_batch_checks(json.loads(raw))
    except (OSError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_check_batch_request",
                message="Batch input must be a non-empty JSON array of valid check bodies.",
                hint="Check required fields, tag bounds and aligned fallback lists before retrying.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="project_key_required",
                message="Budget checks require a project API key.",
                hint="Set SOLWYN_API_KEY or store a project key with solwyn login --with-key.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            data = request_budget_check_batch(
                client=client,
                checks=checks,
                no_record=no_record,
                estimated_output_tokens=estimated_output_tokens,
            )
    except BudgetCheckBatchFailure as error:
        output_engine(context).write_success(
            schema="solwyn.budget.check.batch.v1",
            data=error.data,
            table=_batch_check_table(error.data),
        )
        typer.echo(
            f"Batch failed at input index {error.data.failed_chunk_start} "
            f"of {error.data.total_checks} checks. {BATCH_FAILURE_HINT}",
            err=True,
        )
        if isinstance(error.cause, ApiError):
            exit_with_error(context, _read_only_key_hinted(error.cause))
        if isinstance(error.cause, AuthError):
            exit_with_error(
                context,
                OutputError(code="authentication_failed", message=str(error.cause)),
                semantic=ErrorSemantic.AUTH,
            )
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_check_batch_response",
                message="The batch check response was invalid.",
            ),
        )
    except ApiError as error:
        exit_with_error(context, _read_only_key_hinted(error))
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
                code="invalid_budget_check_batch_response",
                message="The batch check response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.budget.check.batch.v1",
        data=data,
        table=_batch_check_table(data),
    )
    typer.echo(
        "Each allowed reservation must be confirmed after its provider call. Batch checks consume checks_per_minute per item; do not poll or replay to refresh reservations.",
        err=True,
    )
    if any(
        item.result is not None and not item.result.allowed for item in data.results
    ):
        raise typer.Exit(code=ExitCode.BUDGET_DENIED)
    if any(item.error_code is not None for item in data.results):
        raise typer.Exit(code=ExitCode.GENERIC_ERROR)


def request_budget_confirm(
    *, client: SolwynClient, request: BudgetConfirmRequest
) -> BudgetConfirmData:
    """Settle one reservation with the exact no-replay 204 contract."""
    response = client.request(
        "POST",
        "/api/v1/budgets/confirm",
        # Unset optional fields are omitted rather than sent as explicit nulls,
        # matching the SDK serializer: the endpoint parses an omitted
        # provider_region or service_tier exactly as it parses an explicit null,
        # and settles from the value either way.
        json=request.model_dump(mode="json", exclude_none=True),
        retry_rate_limit=False,
    )
    if response.status_code != 204 or response.content:
        raise ValueError("Budget confirmation did not return an empty 204 response")
    return _BudgetConfirmData(
        reservation_id=request.reservation_id,
        call_id=request.call_id,
    )


def record_denial(
    *,
    client: SolwynClient,
    request: BudgetCheckRequest,
    data: BudgetCheckData,
    estimated_output_tokens: int | None = None,
    no_record: bool = False,
    sdk_instance_id: str | None = None,
) -> BudgetCheckData:
    """Attach a denial receipt only to a denied check; shared with batch/MCP."""
    if data.allowed:
        return data
    call_id = str(uuid4())
    inputs = dict(
        model=request.model,
        provider=request.provider,
        call_id=call_id,
        input_tokens=request.estimated_input_tokens,
        output_tokens=0,
        latency_ms=0,
        status="budget_denied",
        deny_source="server",
        deny_reason=data.denied_by_period,
        denied_by_period=data.denied_by_period,
        estimated_output_bound=estimated_output_tokens,
        agent_run_id=request.agent_run_id,
        tags=request.tags,
        media_usage=request.estimated_media,
    )
    if sdk_instance_id is not None:
        inputs["sdk_instance_id"] = sdk_instance_id
    try:
        event = build_event(**inputs)
        recorded = record_best_effort(client=client, event=event, no_record=no_record)
    except (ValueError, TypeError) as error:
        typer.echo(f"Warning: denial attribution failed: {error}", err=True)
        recorded = RecordingResult(status="failed", ingested=0, call_ids=[call_id])
    return data.model_copy(update={"call_id": call_id, "recorded": recorded})


def confirm_event(
    *, request: BudgetConfirmRequest, **attribution: object
) -> IngestMetadataEvent:
    """Prepare and validate attribution before committing the settlement."""
    return build_event(
        call_id=request.call_id,
        model=request.model,
        provider=request.provider,
        token_details=request.token_details,
        input_tokens=request.token_details.input_tokens,
        output_tokens=request.token_details.output_tokens,
        provider_region=request.provider_region,
        service_tier=request.service_tier,
        **attribution,
    )


def confirm_and_record(
    *,
    client: SolwynClient,
    request: BudgetConfirmRequest,
    event: IngestMetadataEvent,
    no_record: bool = False,
) -> BudgetConfirmData:
    data = request_budget_confirm(client=client, request=request)
    recorded = record_best_effort(client=client, event=event, no_record=no_record)
    return data.model_copy(update={"recorded": recorded})


def _table(data: _BudgetStatusOutput) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Budget limit", f"{data.budget_limit:.2f}"),
            ("Current usage", f"{data.current_usage:.2f}"),
            ("Remaining budget", f"{data.remaining_budget:.2f}"),
            ("Budget period", data.budget_period),
            ("Budget mode", data.budget_mode),
            ("Utilization", f"{data.utilization_pct:.2f}%"),
            ("Outage overspend ceiling", f"{data.outage_overspend_ceiling_usd:.2f}"),
            (
                "Lease float (included in usage)",
                f"{data.lease_float_usd:.2f}"
                if data.lease_float_usd is not None
                else "—",
            ),
            *tuple(_rule_rows(data.thresholds)),
        ),
        right_align=("Value",),
    )


def _rule_rows(thresholds: list[_ThresholdState]) -> list[tuple[str, str]]:
    rows = []
    for rule in thresholds:
        if isinstance(rule, _ScopedBudgetState):
            selector = (
                f"{rule.match}={rule.match_value}"
                if rule.scope == "tag"
                else rule.match
            )
            usage = "—" if rule.current_usage is None else f"{rule.current_usage:.2f}"
            rows.append(
                (
                    f"Scoped {rule.scope}: {selector}",
                    f"{usage} / {rule.limit:.2f} ({rule.mode.value})",
                )
            )
        elif isinstance(rule, _RunawayRunState):
            rows.append(
                ("Runaway run", f"{rule.spend_threshold_usd:.2f} ({rule.mode.value})")
            )
    return rows


def _check_table(data: _BudgetCheckData) -> TableData:
    table = TableData(
        columns=("Field", "Value"),
        rows=(
            ("Allowed", "yes" if data.allowed else "no"),
            ("Reservation ID", data.reservation_id or "-"),
            ("Project", data.project_id),
            ("Budget mode", data.mode.value),
            ("Budget limit", f"{data.budget_limit:.2f}"),
            ("Current usage", f"{data.current_usage:.2f}"),
            ("Remaining budget", f"{data.remaining_budget:.2f}"),
            (
                "Denied by period",
                data.denied_by_period or "-",
            ),
            *(
                (
                    (
                        "Reason",
                        "run stopped by an operator — do not retry under a new run id",
                    ),
                )
                if data.reason == "run_terminated"
                else ()
            ),
        ),
        right_align=("Value",),
    )

    if data.recorded is not None:
        table = TableData(
            columns=table.columns,
            rows=(
                *table.rows,
                ("Call ID", data.call_id or "-"),
                ("Recorded", data.recorded.status),
            ),
            right_align=table.right_align,
        )
    if data.price_hints:
        return TableData(
            columns=(*table.columns, "rel. price"),
            rows=(
                *tuple((*row, "-") for row in table.rows),
                *tuple(
                    ("Provider", provider.value, f"{price:.6g}")
                    for provider, price in sorted(
                        data.price_hints.items(), key=lambda item: item[0].value
                    )
                ),
            ),
            right_align=(*table.right_align, "rel. price"),
        )
    return table


def _confirm_table(data: _BudgetConfirmData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Reservation ID", data.reservation_id),
            ("Call ID", data.call_id),
            ("Recorded", data.recorded.status if data.recorded else "-"),
        ),
        right_align=("Value",),
    )


def _threshold_configs(data: _BudgetStatusData) -> list[dict[str, object]]:
    """Convert validated response state to request config without server-only data."""
    return [
        threshold.model_dump(
            mode="json",
            exclude={"rule_key", "routing", "current_usage"},
        )
        for threshold in data.thresholds
    ]


def _configuration_from_status(data: BudgetStatusData) -> BudgetUpdateRequest:
    """Normalize persisted configuration, excluding all server-only state."""
    return BudgetUpdateRequest(
        limit_usd=data.budget_limit,
        period=data.budget_period,
        mode=data.budget_mode,
        sub_period=data.sub_period,
        sub_limit_usd=data.sub_limit,
        thresholds=_threshold_configs(data),
    )


def request_budget_update(
    *, client: SolwynClient, project_id: str, request: BudgetUpdateRequest
) -> BudgetStatusData:
    """Write configuration; reconcile a possibly committed 503 without replaying it.

    Core commits before reading the response snapshot. A matching follow-up GET
    proves the intended configuration is now present; any other result leaves
    the write outcome indeterminate. Ordinary bounded 429 handling is unchanged.
    """
    project_path = quote(project_id, safe="")
    try:
        response = client.request(
            "PUT",
            f"/api/v1/projects/{project_path}/budget",
            json=request.model_dump(mode="json"),
        )
    except ApiError as error:
        if error.status_code != 503:
            raise
        try:
            updated = request_budget_status(client=client, project_id=project_id)
            if _configuration_from_status(updated) == request:
                return updated
        except (ApiError, AuthError, ValueError, TypeError):
            pass
        raise ApiError(
            status_code=503,
            code="budget_update_indeterminate",
            message="The budget update may have been saved, but its result could not be confirmed.",
            hint="Run solwyn budget status and review the full configuration before retrying the update.",
            detail=None,
        ) from error
    updated = _BudgetStatusData.model_validate(response.json())
    if updated.project_id != project_id:
        raise ValueError("Budget response project did not match the request.")
    return updated


def _set_data(data: _BudgetStatusData) -> _BudgetSetData:
    return _BudgetSetData(
        project_id=data.project_id,
        budget_limit=data.budget_limit,
        current_usage=data.current_usage,
        remaining_budget=data.remaining_budget,
        budget_period=data.budget_period,
        budget_mode=data.budget_mode,
        period_date=data.period_date,
        utilization_pct=data.utilization_pct,
        sub_period=data.sub_period,
        sub_limit=data.sub_limit,
        thresholds=_threshold_configs(data),
    )


def _set_table(data: _BudgetSetData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Budget limit", f"{data.budget_limit:.2f}"),
            ("Current usage", f"{data.current_usage:.2f}"),
            ("Remaining budget", f"{data.remaining_budget:.2f}"),
            ("Budget period", data.budget_period.value),
            ("Budget mode", data.budget_mode.value),
            (
                "Sub-period",
                data.sub_period.value if data.sub_period is not None else "-",
            ),
            (
                "Sub-limit",
                f"{data.sub_limit:.2f}" if data.sub_limit is not None else "-",
            ),
            ("Threshold rules", len(data.thresholds)),
            ("Utilization", f"{data.utilization_pct:.2f}%"),
        ),
        right_align=("Value",),
    )


@app.command(
    "check",
    epilog=help_contract(
        endpoint="POST /api/v1/budgets/check; POST /api/v1/budgets/check/batch (--batch)",
        auth="key",
        exits=(
            "0 allowed or fail-open; 1 generic/API or item error; 2 usage; 3 auth; 4 budget denial; 5 not found"
        ),
        schema="solwyn.budget-check.v1; solwyn.budget.check.batch.v1 (--batch)",
        guidance=(
            "Run immediately before caller-made un-instrumented provider spend.",
            "Denied checks record a receipt by default; --no-record skips ingest. Copy the emitted call_id when referring to that attempt.",
            "Do not use with Solwyn SDK-instrumented calls. Do not poll with check.",
            "--batch FILE|- reads a JSON array, validates all items, and chunks at 32. Each allowed item holds its own reservation; confirm each after its call. Rate limits count every item against checks_per_minute. Item errors exit 1; any denial takes precedence with exit 4.",
            "Set the media quantities for an image, video, audio, or speech call; "
            "the API prices the reservation from them instead of from tokens.",
        ),
    ),
)
def check(
    context: typer.Context,
    estimated_input_tokens: Annotated[
        int | None,
        typer.Option(
            "--estimated-input-tokens",
            min=0,
            help="Estimated input tokens for the pending provider call.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Primary provider model name."),
    ] = None,
    provider: Annotated[
        ProviderName | None,
        typer.Option("--provider", help="Primary provider."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Verbatim key=value tag; repeatable. Flags override SOLWYN_TAGS.",
        ),
    ] = None,
    estimated_output_tokens: Annotated[
        int | None,
        typer.Option(
            "--estimated-output-tokens",
            min=0,
            max=100_000_000,
            help="Output upper bound for denial savings attribution.",
        ),
    ] = None,
    no_record: Annotated[
        bool, typer.Option("--no-record", help="Skip the denial receipt.")
    ] = False,
    fallback_providers: Annotated[
        list[ProviderName] | None,
        typer.Option(
            "--fallback-provider",
            help="Fallback provider in attempt order; repeat with fallback model.",
        ),
    ] = None,
    fallback_models: Annotated[
        list[str] | None,
        typer.Option(
            "--fallback-model",
            help="Fallback model aligned with each fallback provider.",
        ),
    ] = None,
    agent_run_id: Annotated[
        str | None,
        typer.Option(
            "--agent-run-id",
            envvar="SOLWYN_AGENT_RUN_ID",
            help=(
                "Optional agent run identifier for per-run budget enforcement; "
                "omit for project-only behavior."
            ),
        ),
    ] = None,
    image_count: Annotated[
        int | None,
        typer.Option(
            "--image-count",
            min=0,
            help="Images the pending call generates or edits.",
        ),
    ] = None,
    generation_count: Annotated[
        int | None,
        typer.Option(
            "--generation-count",
            min=0,
            help="Per-video, per-generation, or per-song units the call bills.",
        ),
    ] = None,
    video_seconds: Annotated[
        float | None,
        typer.Option(
            "--video-seconds",
            min=0.0,
            help="Video duration the pending call bills, in seconds.",
        ),
    ] = None,
    audio_seconds: Annotated[
        float | None,
        typer.Option(
            "--audio-seconds",
            min=0.0,
            help="Audio duration the pending call bills, in seconds.",
        ),
    ] = None,
    input_characters: Annotated[
        int | None,
        typer.Option(
            "--input-characters",
            min=0,
            help="Speech-synthesis input length the pending call bills.",
        ),
    ] = None,
    resolution: Annotated[
        str | None,
        typer.Option(
            "--resolution",
            help="Image resolution variant the call is priced at, e.g. 1024x1024.",
        ),
    ] = None,
    quality: Annotated[
        str | None,
        typer.Option(
            "--quality",
            help="Image quality variant the call is priced at, e.g. standard or hd.",
        ),
    ] = None,
    is_estimated: Annotated[
        bool,
        typer.Option(
            "--is-estimated",
            help="Mark the media quantities as estimates rather than measured.",
        ),
    ] = False,
    batch: Annotated[
        str | None,
        typer.Option(
            "--batch",
            help="JSON array of check bodies from FILE or - for stdin; chunked at 32.",
        ),
    ] = None,
) -> None:
    """Reserve budget immediately before one un-instrumented provider call."""
    if batch is not None:
        single_options = (
            estimated_input_tokens,
            model,
            provider,
            tag,
            fallback_providers,
            fallback_models,
            image_count,
            generation_count,
            video_seconds,
            audio_seconds,
            input_characters,
            resolution,
            quality,
        )
        if (
            any(value is not None for value in single_options)
            or is_estimated
            or (context.get_parameter_source("agent_run_id").name == "COMMANDLINE")
        ):
            exit_with_error(
                context,
                OutputError(
                    code="conflicting_batch_options",
                    message="Put per-check inputs in the batch file; single-check flags cannot accompany --batch.",
                ),
                semantic=ErrorSemantic.USAGE,
            )
        _check_batch_command(
            context,
            batch,
            no_record=no_record,
            estimated_output_tokens=estimated_output_tokens,
        )
        return
    if estimated_input_tokens is None or model is None or provider is None:
        exit_with_error(
            context,
            OutputError(
                code="missing_budget_check_options",
                message="Single checks require --estimated-input-tokens, --model and --provider.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    providers = fallback_providers or []
    models = fallback_models or []
    if len(providers) != len(models):
        exit_with_error(
            context,
            OutputError(
                code="fallback_alignment_required",
                message="Fallback providers and models must have aligned counts.",
                hint=(
                    "Repeat --fallback-provider and --fallback-model together "
                    "in attempt order."
                ),
            ),
            semantic=ErrorSemantic.USAGE,
        )
    quantities = (
        image_count,
        generation_count,
        video_seconds,
        audio_seconds,
        input_characters,
        resolution,
        quality,
    )
    try:
        # No media option at all means a text-only call: the request omits
        # estimated_media entirely rather than posting an all-null object.
        estimated_media = (
            MediaUsage(
                image_count=image_count,
                generation_count=generation_count,
                video_seconds=video_seconds,
                audio_seconds=audio_seconds,
                input_characters=input_characters,
                resolution=resolution,
                quality=quality,
                is_estimated=is_estimated,
            )
            if is_estimated or any(value is not None for value in quantities)
            else None
        )
        request = BudgetCheckRequest(
            price_hints_version="1",
            run_directive_version="1" if agent_run_id is not None else None,
            estimated_input_tokens=estimated_input_tokens,
            model=model,
            provider=provider,
            estimated_media=estimated_media,
            fallback_providers=providers,
            fallback_models=models,
            agent_run_id=agent_run_id,
            tags=parse_tags(tag),
        )
    except (ValidationError, ValueError) as error:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_check_request",
                message="The budget check request is invalid.",
                hint=(
                    f"{error}. Check tag bounds and use no more than eight fallbacks."
                ),
            ),
            semantic=ErrorSemantic.USAGE,
        )

    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="project_key_required",
                message="Budget checks require a project API key.",
                hint=(
                    "Set SOLWYN_API_KEY or store a project key with "
                    "solwyn login --with-key."
                ),
            ),
            semantic=ErrorSemantic.AUTH,
        )

    runtime = command_context(context)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = request_budget_check(client=client, request=request)
            data = record_denial(
                client=client,
                request=request,
                data=data,
                estimated_output_tokens=estimated_output_tokens,
                no_record=no_record,
            )
    except ApiError as error:
        exit_with_error(context, _read_only_key_hinted(error))
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
                code="invalid_budget_check_response",
                message="The budget check response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.budget-check.v1",
        data=data,
        table=_check_table(data),
    )
    if not data.allowed:
        if data.denied_by_period == "tag":
            keys = list(request.tags or {})
            suffix = f" `{keys[0]}`" if len(keys) == 1 else ""
            typer.echo(f"denied by tag cap{suffix}", err=True)
        raise typer.Exit(code=ExitCode.BUDGET_DENIED)
    if data.reservation_id is None:
        typer.echo(_FAIL_OPEN_NOTICE, err=True)


@app.command(
    "confirm",
    epilog=help_contract(
        endpoint="POST /api/v1/budgets/confirm",
        auth="key",
        exits=("0 success; 1 generic/API; 2 usage; 3 auth; 5 reservation not found"),
        schema="solwyn.budget-confirm.v1",
        guidance=(
            "Settle only when check returned a non-null reservation after the call.",
            "Skip confirm when the reservation is null.",
            "Records attribution with the same call ID by default; --no-record skips ingest. Recording failure warns without undoing settlement.",
        ),
    ),
)
def confirm(
    context: typer.Context,
    call_id: Annotated[
        str,
        typer.Option("--call-id", help="Unique identifier for the provider call."),
    ],
    model: Annotated[
        str,
        typer.Option("--model", help="Model that actually served the call."),
    ],
    provider: Annotated[
        ProviderName,
        typer.Option("--provider", help="Provider that actually served the call."),
    ],
    latency_ms: Annotated[float, typer.Option("--latency-ms", min=0)] = 0,
    status: Annotated[
        str, typer.Option("--status", help="success or error.")
    ] = "success",
    agent_run_id: Annotated[
        str | None, typer.Option("--agent-run-id", envvar="SOLWYN_AGENT_RUN_ID")
    ] = None,
    parent_run_id: Annotated[str | None, typer.Option("--parent-run-id")] = None,
    run_name: Annotated[str | None, typer.Option("--run-name")] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag", help="Verbatim key=value; repeatable; overrides SOLWYN_TAGS."
        ),
    ] = None,
    no_record: Annotated[
        bool, typer.Option("--no-record", help="Settle without recording attribution.")
    ] = False,
    reservation_id: Annotated[
        str | None,
        typer.Option(
            "--reservation-id",
            help="Reservation returned by the immediately preceding budget check.",
        ),
    ] = None,
    is_provider_fallback: Annotated[
        bool,
        typer.Option(
            "--is-provider-fallback",
            help="Mark that a fallback provider served the call.",
        ),
    ] = False,
    provider_region: Annotated[
        str | None,
        typer.Option("--provider-region", help="Serving cloud region, when relevant."),
    ] = None,
    service_tier: Annotated[
        ServiceTier | None,
        typer.Option("--service-tier", help="Provider service tier used for the call."),
    ] = None,
    input_tokens: Annotated[
        int,
        typer.Option("--input-tokens", min=0, max=100_000_000),
    ] = 0,
    output_tokens: Annotated[
        int,
        typer.Option("--output-tokens", min=0, max=100_000_000),
    ] = 0,
    cached_input_tokens: Annotated[
        int,
        typer.Option("--cached-input-tokens", min=0, max=100_000_000),
    ] = 0,
    cache_creation_5m_tokens: Annotated[
        int,
        typer.Option("--cache-creation-5m-tokens", min=0, max=100_000_000),
    ] = 0,
    cache_creation_1h_tokens: Annotated[
        int,
        typer.Option("--cache-creation-1h-tokens", min=0, max=100_000_000),
    ] = 0,
    reasoning_tokens: Annotated[
        int,
        typer.Option("--reasoning-tokens", min=0, max=100_000_000),
    ] = 0,
    audio_input_tokens: Annotated[
        int,
        typer.Option("--audio-input-tokens", min=0, max=100_000_000),
    ] = 0,
    audio_output_tokens: Annotated[
        int,
        typer.Option("--audio-output-tokens", min=0, max=100_000_000),
    ] = 0,
    image_input_tokens: Annotated[
        int,
        typer.Option(
            "--image-input-tokens",
            min=0,
            max=100_000_000,
            help="Image input tokens, already counted within --input-tokens.",
        ),
    ] = 0,
    image_output_tokens: Annotated[
        int,
        typer.Option(
            "--image-output-tokens",
            min=0,
            max=100_000_000,
            help="Image output tokens, already counted within --output-tokens.",
        ),
    ] = 0,
    accepted_prediction_tokens: Annotated[
        int,
        typer.Option("--accepted-prediction-tokens", min=0, max=100_000_000),
    ] = 0,
    rejected_prediction_tokens: Annotated[
        int,
        typer.Option("--rejected-prediction-tokens", min=0, max=100_000_000),
    ] = 0,
    tool_use_input_tokens: Annotated[
        int,
        typer.Option("--tool-use-input-tokens", min=0, max=100_000_000),
    ] = 0,
    is_estimated: Annotated[
        bool,
        typer.Option(
            "--is-estimated",
            help="Mark the token breakdown as estimated rather than provider-reported.",
        ),
    ] = False,
) -> None:
    """Settle one reservation after an un-instrumented provider call."""
    if reservation_id is None:
        exit_with_error(
            context,
            OutputError(
                code="reservation_required",
                message="No budget reservation was supplied.",
                hint=("reservation_id=null means fail-open/unreserved; skip confirm."),
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if not reservation_id.strip():
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_confirm_request",
                message="The budget confirmation request is invalid.",
                hint="Provide a nonempty reservation ID returned by budget check.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if (
        not call_id.strip()
        or not model.strip()
        or (provider_region is not None and not provider_region.strip())
    ):
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_confirm_request",
                message="The budget confirmation request is invalid.",
                hint="Provide nonempty call, model, and optional region values.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        validate_call_id(call_id)
        if status not in ("success", "error"):
            raise ValueError("Status must be success or error.")
        token_details = TokenDetails(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_5m_tokens=cache_creation_5m_tokens,
            cache_creation_1h_tokens=cache_creation_1h_tokens,
            reasoning_tokens=reasoning_tokens,
            audio_input_tokens=audio_input_tokens,
            audio_output_tokens=audio_output_tokens,
            image_input_tokens=image_input_tokens,
            image_output_tokens=image_output_tokens,
            accepted_prediction_tokens=accepted_prediction_tokens,
            rejected_prediction_tokens=rejected_prediction_tokens,
            tool_use_input_tokens=tool_use_input_tokens,
            is_estimated=is_estimated,
        )
        request = BudgetConfirmRequest(
            reservation_id=reservation_id,
            call_id=call_id,
            model=model,
            provider=provider,
            is_provider_fallback=is_provider_fallback,
            provider_region=provider_region,
            service_tier=service_tier.value if service_tier is not None else None,
            token_details=token_details,
        )
        event = confirm_event(
            request=request,
            latency_ms=latency_ms,
            status=status,
            agent_run_id=agent_run_id,
            parent_agent_run_id=parent_run_id,
            agent_run_name=run_name,
            tags=parse_tags(tag),
        )
    except (ValidationError, ValueError) as error:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_confirm_request",
                message="The budget confirmation request is invalid.",
                hint=f"{error}. {CALL_ID_HINT}",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    try:
        credential = resolve_credential()
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(
                code="authentication_failed",
                message=str(error),
                hint=(
                    "Set SOLWYN_API_KEY or store a project key with "
                    "solwyn login --with-key."
                ),
            ),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is None or isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="project_key_required",
                message="Budget confirmation requires a project API key.",
                hint=(
                    "Set SOLWYN_API_KEY or store a project key with "
                    "solwyn login --with-key."
                ),
            ),
            semantic=ErrorSemantic.AUTH,
        )

    runtime = command_context(context)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = confirm_and_record(
                client=client, request=request, event=event, no_record=no_record
            )
    except ApiError as error:
        if error.status_code == 404:
            error = ApiError(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
                hint=_CONFIRM_NOT_FOUND_HINT,
                detail=error.detail,
            )
        exit_with_error(context, _read_only_key_hinted(error))
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
                code="invalid_budget_confirm_response",
                message="The budget confirmation response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.budget-confirm.v1",
        data=data,
        table=_confirm_table(data),
    )


@app.command(
    "set",
    epilog=help_contract(
        endpoint="GET then PUT /api/v1/projects/{project}/budget",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.budget-set.v1",
    ),
)
def set_budget(
    context: typer.Context,
    limit: Annotated[
        float | None,
        typer.Option("--limit", help="Set the project budget limit in USD."),
    ] = None,
    period: Annotated[
        BudgetPeriod | None,
        typer.Option("--period", help="Set the project budget period."),
    ] = None,
    mode: Annotated[
        BudgetMode | None,
        typer.Option("--mode", help="Set the project budget enforcement mode."),
    ] = None,
) -> None:
    """Update selected budget settings while preserving all other configuration."""
    if limit is None and period is None and mode is None:
        exit_with_error(
            context,
            OutputError(
                code="budget_changes_required",
                message="No budget changes were supplied.",
                hint="Provide at least one of --limit, --period, or --mode.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if limit is not None and (not math.isfinite(limit) or limit <= 0):
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_update_request",
                message="The budget limit must be a finite positive number.",
                hint="Provide --limit with a value greater than zero.",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    runtime = command_context(context)
    if runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint=(
                    "Choose a project with --project, SOLWYN_PROJECT_ID, "
                    "or config default_project."
                ),
            ),
            semantic=ErrorSemantic.USAGE,
        )

    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry budget set."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry budget set."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to update a budget.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )

    project_id = runtime.project
    project_path = quote(project_id, safe="")
    endpoint = f"/api/v1/projects/{project_path}/budget"
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            current_response = client.request("GET", endpoint)
            current = _BudgetStatusData.model_validate(current_response.json())
            if current.project_id != project_id:
                raise ValueError("Budget response project did not match the request.")

            request = BudgetUpdateRequest(
                limit_usd=limit if limit is not None else current.budget_limit,
                period=period if period is not None else current.budget_period,
                mode=mode if mode is not None else current.budget_mode,
                sub_period=current.sub_period,
                sub_limit_usd=current.sub_limit,
                thresholds=_threshold_configs(current),
            )
            updated = request_budget_update(
                client=client, project_id=project_id, request=request
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
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_response",
                message="The budget response was invalid.",
            ),
        )

    data = _set_data(updated)
    output_engine(context).write_success(
        schema="solwyn.budget-set.v1",
        data=data,
        table=_set_table(data),
    )


@app.command(
    "status",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/budget",
        auth="any",
        exits=(
            "0 success; 1 generic/API; 2 usage; 3 auth; 4 utilization gate; 5 not found"
        ),
        schema="solwyn.budget-status.v1",
        guidance=(
            "Pure read/poll; never reserves or authorizes spend. "
            "Poll with status, never check.",
        ),
    ),
)
def status(
    context: typer.Context,
    max_utilization: Annotated[
        float | None,
        typer.Option(
            "--max-utilization",
            min=0.0,
            max=100.0,
            help=(
                "CI deploy gate: exit 4 when utilization is at or above this "
                "percentage."
            ),
        ),
    ] = None,
) -> None:
    """Read current project budget status without authorizing spend."""
    if max_utilization is not None and not math.isfinite(max_utilization):
        raise typer.BadParameter(
            "Must be a finite percentage from 0 through 100.",
            param_hint="max-utilization",
        )

    runtime = command_context(context)
    if runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint=(
                    "Choose a project with --project, SOLWYN_PROJECT_ID, "
                    "or config default_project."
                ),
            ),
            semantic=ErrorSemantic.USAGE,
        )

    credential = required_credential(context, resolve=resolve_credential)
    try:
        data = fetch_budget_status(
            api_url=runtime.api_url,
            credential=credential,
            project_id=runtime.project,
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
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_response",
                message="The budget response was invalid.",
            ),
        )

    rendered = _BudgetStatusOutput(
        outage_overspend_ceiling_usd=data.outage_overspend_ceiling_usd,
        lease_float_usd=data.lease_float_usd,
        thresholds=data.thresholds,
        project_id=data.project_id,
        budget_limit=data.budget_limit,
        current_usage=data.current_usage,
        remaining_budget=data.remaining_budget,
        utilization_pct=data.utilization_pct,
        budget_mode=data.budget_mode,
        budget_period=data.budget_period,
    )
    output_engine(context).write_success(
        schema="solwyn.budget-status.v1",
        data=rendered,
        table=_table(rendered),
    )
    if max_utilization is not None and data.utilization_pct >= max_utilization:
        raise typer.Exit(code=ExitCode.BUDGET_DENIED)
