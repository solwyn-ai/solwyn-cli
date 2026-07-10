"""Project budget inspection and pre-flight guardrail commands."""

import math
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
    BudgetMode,
    BudgetPeriod,
    ProviderName,
)
from solwyn_cli.auth import AuthError, JwtCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, ExitCode
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(help="Inspect budgets and run pre-flight guardrail checks.")

_FAIL_OPEN_NOTICE = "fail-open, unreserved — skip confirm"


class _BudgetStatusData(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    project_id: str
    budget_limit: FiniteFloat
    current_usage: FiniteFloat
    remaining_budget: FiniteFloat
    utilization_pct: FiniteFloat
    budget_mode: Literal["alert_only", "hard_deny"]
    budget_period: Literal["daily", "weekly", "monthly"]


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


def _table(data: _BudgetStatusData) -> TableData:
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

    output_engine(context).write_success(
        schema="solwyn.budget-status.v1",
        data=data,
        table=_table(data),
    )
    if max_utilization is not None and data.utilization_pct >= max_utilization:
        raise typer.Exit(code=ExitCode.BUDGET_DENIED)
