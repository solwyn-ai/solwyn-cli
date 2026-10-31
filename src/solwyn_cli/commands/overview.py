"""Calendar-month account overview command."""

from datetime import date, datetime

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli.auth import AuthError, LoginCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._cost_models import SavingsSummary
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


OVERVIEW_HELP = help_contract(
    endpoint="GET /api/v1/overview",
    auth="login",
    exits="0 success; 1 generic/API; 3 auth",
    schema="solwyn.overview.v1",
)


class OverviewResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    total_spend: float
    total_requests: int
    project_count: int
    trend_vs_last_period: float
    period_start: date
    period_end: date
    savings: SavingsSummary | None = None
    quota_exceeded: bool
    included_requests: int | None = None
    current_requests: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


OverviewData = OverviewResponse


def fetch_overview(*, client: SolwynClient) -> OverviewData:
    """Read and validate the login-authenticated account overview."""
    return OverviewData.model_validate(client.request("GET", "/api/v1/overview").json())


def _table(data: OverviewData) -> TableData:
    wire = data.model_dump(mode="json")
    trend = f"{data.trend_vs_last_period:+.2%}"
    rows: list[tuple[str, str]] = [
        ("Period", f"{wire['period_start']} - {wire['period_end']}"),
        ("Total spend", f"${data.total_spend:.6f}"),
        ("Total requests", str(data.total_requests)),
        ("Projects", str(data.project_count)),
        ("Trend vs last period", trend),
    ]
    if data.savings is not None:
        rows.extend(
            (
                ("Savings", f"${data.savings.saved_cost:.6f}"),
                (
                    "Savings upper",
                    "—"
                    if data.savings.saved_cost_upper is None
                    else f"${data.savings.saved_cost_upper:.6f}",
                ),
                ("Saved input tokens", str(data.savings.saved_input_tokens)),
                ("Blocked calls", str(data.savings.blocked_call_count)),
            )
        )
    rows.append(("Quota exceeded", "yes" if data.quota_exceeded else "no"))
    if data.included_requests is not None and data.current_requests is not None:
        rows.append(
            (
                "Quota requests",
                f"{data.current_requests} / {data.included_requests}",
            )
        )
    if data.window_start is not None and data.window_end is not None:
        rows.append(("Quota window", f"{wire['window_start']} - {wire['window_end']}"))
    return TableData(
        columns=("Metric", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def overview(context: typer.Context) -> None:
    """Show calendar-month account spend and request totals."""
    runtime = command_context(context)
    credential = required_credential(context, resolve=resolve_credential)
    if not isinstance(credential, LoginCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Account overview requires user login.",
                hint="Run solwyn login, then retry.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = fetch_overview(client=client)
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
                code="invalid_overview_response",
                message="The overview response was invalid.",
            ),
        )
    payload = data.model_dump(mode="json")
    for field in (
        "savings",
        "included_requests",
        "current_requests",
        "window_start",
        "window_end",
    ):
        if payload[field] is None:
            payload.pop(field)
    output_engine(context).write_success(
        schema="solwyn.overview.v1",
        data=payload,
        table=_table(data),
    )
