"""Project budget inspection and pre-flight guardrail commands."""

import math
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
    BudgetConfirmRequest,
    BudgetMode,
    BudgetPeriod,
    BudgetUpdateRequest,
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
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, ExitCode
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(help="Inspect budgets and run pre-flight guardrail checks.")

_FAIL_OPEN_NOTICE = "fail-open, unreserved — skip confirm"
_CONFIRM_NOT_FOUND_HINT = (
    "Expired or already consumed. Run a fresh check before spend; confirm once."
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


class _DailyAnomalyState(_ThresholdStateBase):
    type: Literal["daily_anomaly"]
    multiplier: Annotated[FiniteFloat, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7


class _UnitCostAnomalyState(_ThresholdStateBase):
    type: Literal["unit_cost_anomaly"]
    multiplier: Annotated[FiniteFloat, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7


_ThresholdState = Annotated[
    _BudgetThresholdState
    | _ForecastOverrunState
    | _DailyAnomalyState
    | _UnitCostAnomalyState,
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


class _BudgetStatusOutput(BaseModel):
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
    denied_by_period: BudgetPeriod | None = None
    project_id: str
    price_hints: dict[ProviderName, FiniteFloat] | None = None

    @model_validator(mode="after")
    def validate_guardrail_outcome(self) -> "_BudgetCheckData":
        """Reject contradictory reservation and denial states."""
        if not self.allowed and self.reservation_id is not None:
            raise ValueError("Denied checks cannot carry a reservation")
        if not self.allowed and self.mode is not BudgetMode.HARD_DENY:
            raise ValueError("Only hard-deny mode can deny a check")
        if self.allowed and self.denied_by_period is not None:
            raise ValueError("Allowed checks cannot carry a denial period")
        return self


class _BudgetConfirmData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_id: str
    call_id: str


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
        ),
        right_align=("Value",),
    )


def _check_table(data: _BudgetCheckData) -> TableData:
    price_hints = (
        ", ".join(
            f"{provider.value}={price:.6g}"
            for provider, price in sorted(
                data.price_hints.items(), key=lambda item: item[0].value
            )
        )
        if data.price_hints
        else "-"
    )
    return TableData(
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
                data.denied_by_period.value if data.denied_by_period else "-",
            ),
            ("Price hints", price_hints),
        ),
        right_align=("Value",),
    )


def _confirm_table(data: _BudgetConfirmData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Reservation ID", data.reservation_id),
            ("Call ID", data.call_id),
        ),
        right_align=("Value",),
    )


def _threshold_configs(data: _BudgetStatusData) -> list[dict[str, object]]:
    """Convert validated response state to request config without server-only data."""
    return [
        threshold.model_dump(
            mode="json",
            exclude={"rule_key", "routing"},
        )
        for threshold in data.thresholds
    ]


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


@app.command("check")
def check(
    context: typer.Context,
    estimated_input_tokens: Annotated[
        int,
        typer.Option(
            "--estimated-input-tokens",
            min=0,
            help="Estimated input tokens for the pending provider call.",
        ),
    ],
    model: Annotated[
        str,
        typer.Option("--model", help="Primary provider model name."),
    ],
    provider: Annotated[
        ProviderName,
        typer.Option("--provider", help="Primary provider."),
    ],
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
) -> None:
    """Reserve budget immediately before one un-instrumented provider call."""
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
    try:
        request = BudgetCheckRequest(
            estimated_input_tokens=estimated_input_tokens,
            model=model,
            provider=provider,
            fallback_providers=providers,
            fallback_models=models,
        )
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_check_request",
                message="The budget check request is invalid.",
                hint="Check model lengths and use no more than eight fallbacks.",
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
            response = client.request(
                "POST",
                "/api/v1/budgets/check",
                json=request.model_dump(mode="json"),
            )
        data = _BudgetCheckData.model_validate(response.json())
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
        raise typer.Exit(code=ExitCode.BUDGET_DENIED)
    if data.reservation_id is None:
        typer.echo(_FAIL_OPEN_NOTICE, err=True)


@app.command("confirm")
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
        token_details = TokenDetails(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_5m_tokens=cache_creation_5m_tokens,
            cache_creation_1h_tokens=cache_creation_1h_tokens,
            reasoning_tokens=reasoning_tokens,
            audio_input_tokens=audio_input_tokens,
            audio_output_tokens=audio_output_tokens,
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
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_confirm_request",
                message="The budget confirmation request is invalid.",
                hint="Check identifier lengths, provider metadata, and token counts.",
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
            response = client.request(
                "POST",
                "/api/v1/budgets/confirm",
                json=request.model_dump(mode="json"),
                retry_rate_limit=False,
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
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )

    if response.status_code != 204 or response.content:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_confirm_response",
                message="The budget confirmation response was invalid.",
            ),
        )

    data = _BudgetConfirmData(reservation_id=reservation_id, call_id=call_id)
    output_engine(context).write_success(
        schema="solwyn.budget-confirm.v1",
        data=data,
        table=_confirm_table(data),
    )


@app.command("set")
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
            updated_response = client.request(
                "PUT",
                endpoint,
                json=request.model_dump(mode="json"),
            )
            updated = _BudgetStatusData.model_validate(updated_response.json())
            if updated.project_id != project_id:
                raise ValueError("Budget response project did not match the request.")
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


@app.command("status")
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
    project = quote(runtime.project, safe="")
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            response = client.request("GET", f"/api/v1/projects/{project}/budget")
        data = _BudgetStatusData.model_validate(response.json())
        if data.project_id != runtime.project:
            raise ValueError("Budget response project did not match the request.")
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
