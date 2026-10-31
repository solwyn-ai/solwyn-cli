"""At-a-glance project status command."""

from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli.auth import AuthError, LoginCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands.forecast import ForecastData, fetch_forecast
from solwyn_cli.commands._cost_models import SavingsSummary
from solwyn_cli.commands.overview import fetch_overview
from solwyn_cli.commands.providers import (
    FailoverRange,
    ProviderFailoversData,
    failovers_table,
    fetch_provider_failovers,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
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


STATUS_HELP = help_contract(
    endpoint=(
        "GET /api/v1/projects/{project}/budget and "
        "GET /api/v1/projects/{project}/providers; optionally GET forecast "
        "and provider failovers; login also GET /api/v1/overview"
    ),
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.status.v1",
)


class _BudgetResponse(_BudgetSummary):
    project_id: str


class _ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    provider: str
    total_calls: int
    success_count: int
    error_count: int
    failover_count: int
    model_failover_count: int
    provider_failover_count: int
    success_rate: float
    avg_latency_ms: float
    circuit_state: Literal["closed", "half_open", "open", "unknown"]
    circuit_source: Literal["reported", "inferred"]


class _ProjectStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    budget: _BudgetSummary
    providers: list[_ProviderHealth]


class _StatusData(_ProjectStatusData):
    forecast: ForecastData | None = None
    failovers: ProviderFailoversData | None = None
    savings: SavingsSummary | None = None


_PROVIDERS = TypeAdapter(list[_ProviderHealth])
ProjectStatusData = _ProjectStatusData


def fetch_project_status(
    *,
    client: SolwynClient,
    project_id: str,
) -> ProjectStatusData:
    """Read and validate budget plus provider health through one shared client."""
    project = quote(project_id, safe="")
    budget_response = client.request("GET", f"/api/v1/projects/{project}/budget")
    providers_response = client.request("GET", f"/api/v1/projects/{project}/providers")
    budget = _BudgetResponse.model_validate(budget_response.json())
    providers = _PROVIDERS.validate_python(providers_response.json())
    data = _ProjectStatusData(
        project_id=budget.project_id,
        budget=_BudgetSummary.model_validate(budget.model_dump()),
        providers=providers,
    )
    if data.project_id != project_id:
        raise ValueError("Status response project did not match the request.")
    return data


def _table(data: _StatusData, *, include_savings: bool = False) -> TableData:
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
    if include_savings:
        if data.savings is None:
            rows.append(("Savings", "—"))
        else:
            upper = (
                "—"
                if data.savings.saved_cost_upper is None
                else f"${data.savings.saved_cost_upper:.6f}"
            )
            rows.append(
                (
                    "Savings",
                    f"${data.savings.saved_cost:.6f}; upper {upper}; "
                    f"{data.savings.blocked_call_count} blocked calls",
                )
            )
    rows.append(("Provider health window", "24h (fixed)"))
    for provider in data.providers:
        rows.extend(
            [
                (f"{provider.provider} circuit", provider.circuit_state),
                (f"{provider.provider} circuit source", provider.circuit_source),
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
                (
                    f"{provider.provider} model failovers",
                    str(provider.model_failover_count),
                ),
                (
                    f"{provider.provider} provider failovers",
                    str(provider.provider_failover_count),
                ),
            ]
        )
    if data.forecast is not None:
        rows.extend(
            (
                ("Forecast EOM", f"${data.forecast.forecast_eom:.6f}"),
                (
                    "Forecast burn rate",
                    f"${data.forecast.burn_rate_per_day:.6f} / day",
                ),
                (
                    "Forecast 95% CI",
                    f"${data.forecast.ci_low:.6f} - ${data.forecast.ci_high:.6f}",
                ),
                (
                    "Forecast insufficient data",
                    "yes" if data.forecast.insufficient_data else "no",
                ),
            )
        )
    if data.failovers is not None:
        for field, value in failovers_table(data.failovers).rows:
            if field == "Project":
                continue
            label = "Failover range" if field == "Range" else f"Failover {field}"
            rows.append((label, str(value)))
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def status(
    context: typer.Context,
    include_forecast: Annotated[
        bool,
        typer.Option("--forecast", help="Include the month-to-date spend forecast."),
    ] = False,
    include_failovers: Annotated[
        bool,
        typer.Option("--failovers", help="Include top failover groups for 7d."),
    ] = False,
) -> None:
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
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            project_status = fetch_project_status(
                client=client,
                project_id=runtime.project,
            )
            data = _StatusData(
                **project_status.model_dump(),
                forecast=(
                    fetch_forecast(client=client, project_id=runtime.project)
                    if include_forecast
                    else None
                ),
                failovers=(
                    fetch_provider_failovers(
                        client=client,
                        project_id=runtime.project,
                        time_range=FailoverRange.SEVEN_DAYS,
                    )
                    if include_failovers
                    else None
                ),
                savings=(
                    fetch_overview(client=client).savings
                    if isinstance(credential, LoginCredential)
                    else None
                ),
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

    if include_failovers:
        typer.echo("Showing top 50 groups of an unknown total.", err=True)
    response_data = data.model_dump()
    if data.forecast is None:
        response_data.pop("forecast")
    if data.failovers is None:
        response_data.pop("failovers")
    if data.savings is None:
        response_data.pop("savings")
    output_engine(context).write_success(
        schema="solwyn.status.v1",
        data=response_data,
        table=_table(data, include_savings=isinstance(credential, LoginCredential)),
    )
