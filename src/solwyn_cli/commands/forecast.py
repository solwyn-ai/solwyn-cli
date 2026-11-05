"""Project spend forecast command."""

from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import (
    exit_with_error,
    required_credential,
    with_project,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


FORECAST_HELP = help_contract(
    endpoint="GET /api/v1/projects/{project}/forecast",
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.forecast.v1",
)


class _ForecastResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    current_spend: float
    forecast_eom: float
    burn_rate_per_day: float
    ci_low: float
    ci_high: float
    period_start: str
    period_end: str
    days_elapsed: int
    days_remaining: int
    insufficient_data: bool


class ForecastData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    current_spend: float
    forecast_eom: float
    burn_rate_per_day: float
    ci_low: float
    ci_high: float
    period_start: str
    period_end: str
    days_elapsed: int
    days_remaining: int
    insufficient_data: bool


def fetch_forecast(*, client: SolwynClient, project_id: str) -> ForecastData:
    """Read and validate the month-to-date forecast for one project."""
    project = quote(project_id, safe="")
    response = client.request(
        "GET",
        f"/api/v1/projects/{project}/forecast",
        params={"period": "mtd"},
    )
    forecast_response = _ForecastResponse.model_validate(response.json())
    return ForecastData(
        project_id=project_id,
        **forecast_response.model_dump(),
    )


def forecast_table(data: ForecastData) -> TableData:
    """Render every forecast number alongside the insufficient-data flag."""
    return TableData(
        columns=("Metric", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Period", f"{data.period_start} - {data.period_end}"),
            (
                "Days",
                f"{data.days_elapsed} elapsed / {data.days_remaining} remaining",
            ),
            ("Current spend", f"${data.current_spend:.6f}"),
            ("Forecast EOM", f"${data.forecast_eom:.6f}"),
            ("Burn rate", f"${data.burn_rate_per_day:.6f} / day"),
            ("95% CI", f"${data.ci_low:.6f} - ${data.ci_high:.6f}"),
            ("Insufficient data", "yes" if data.insufficient_data else "no"),
        ),
        right_align=("Value",),
    )


def forecast(context: typer.Context) -> None:
    """Show the month-to-date project spend forecast."""
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
    runtime = with_project(context, credential)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = fetch_forecast(client=client, project_id=runtime.project)
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
                code="invalid_forecast_response",
                message="The forecast response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.forecast.v1",
        data=data,
        table=forecast_table(data),
    )
