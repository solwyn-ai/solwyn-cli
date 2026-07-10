"""At-a-glance project status command."""

from typing import Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


class _BudgetSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    budget_limit: float
    current_usage: float
    remaining_budget: float
    budget_period: Literal["daily", "weekly", "monthly"]
    budget_mode: Literal["alert_only", "hard_deny"]
    period_date: str
    utilization_pct: float


class _BudgetResponse(_BudgetSummary):
    project_id: str


class _ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    provider: str
    total_calls: int
    success_count: int
    error_count: int
    failover_count: int
    success_rate: float
    avg_latency_ms: float
    circuit_state: str


class _StatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    budget: _BudgetSummary
    providers: list[_ProviderHealth]


_PROVIDERS = TypeAdapter(list[_ProviderHealth])


def _table(data: _StatusData) -> TableData:
    budget = data.budget
    rows: list[tuple[str, str]] = [
        ("Project", data.project_id),
        ("Budget limit", f"{budget.budget_limit:.2f}"),
        ("Current usage", f"{budget.current_usage:.2f}"),
        ("Remaining budget", f"{budget.remaining_budget:.2f}"),
        (
            "Budget period",
            f"{budget.budget_period} ({budget.period_date})",
        ),
        ("Budget mode", budget.budget_mode),
        ("Utilization", f"{budget.utilization_pct:.2f}%"),
    ]
    for provider in data.providers:
        rows.extend(
            [
                (f"{provider.provider} circuit", provider.circuit_state),
                (
                    f"{provider.provider} success",
                    f"{provider.success_count} / {provider.total_calls} "
                    f"({provider.success_rate:.2f}%)",
                ),
                (
                    f"{provider.provider} latency",
                    f"{provider.avg_latency_ms:.2f} ms",
                ),
                (
                    f"{provider.provider} errors / failovers",
                    f"{provider.error_count} / {provider.failover_count}",
                ),
            ]
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def status(context: typer.Context) -> None:
    """Show project budget and provider health."""
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
            budget_response = client.request(
                "GET", f"/api/v1/projects/{project}/budget"
            )
            providers_response = client.request(
                "GET", f"/api/v1/projects/{project}/providers"
            )
        budget = _BudgetResponse.model_validate(budget_response.json())
        providers = _PROVIDERS.validate_python(providers_response.json())
        data = _StatusData(
            project_id=budget.project_id,
            budget=_BudgetSummary.model_validate(budget.model_dump()),
            providers=providers,
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
                code="invalid_status_response",
                message="The status response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.status.v1",
        data=data,
        table=_table(data),
    )
