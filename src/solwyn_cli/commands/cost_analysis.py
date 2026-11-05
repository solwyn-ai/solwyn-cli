"""Project cost baselines, unit economics, and estimate drift commands."""

from enum import StrEnum
from typing import Annotated, Any, cast
from urllib.parse import quote

import typer
from pydantic import ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._cost_advice_models import (
    BaselineResult,
    CostDriftResponse,
    DeltaCell,
    UnitEconomicsResponse,
)
from solwyn_cli.commands._cost_filters import window_params
from solwyn_cli.commands._shared import (
    exit_with_error,
    required_credential,
    with_project,
)
from solwyn_cli.commands.costs import report_window
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, _terminal_safe
from solwyn_cli.runtime import CommandContext, command_context, output_engine


class UnitEconomicsRange(StrEnum):
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"


def _project_path(context: typer.Context) -> tuple[CommandContext, str]:
    runtime = command_context(context)
    if runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint="Choose a project with --project, SOLWYN_PROJECT_ID, or config default_project.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return runtime, quote(runtime.project, safe="")


def _request(
    context: typer.Context,
    *,
    path: str,
    params: dict[str, Any],
    response_model: type[BaselineResult | UnitEconomicsResponse | CostDriftResponse],
) -> BaselineResult | UnitEconomicsResponse | CostDriftResponse:
    runtime, _ = _project_path(context)
    credential = required_credential(context, resolve=resolve_credential)
    runtime = with_project(context, credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            from solwyn_cli.commands._cost_reads import fetch_project_read

            data = fetch_project_read(
                client=client,
                project_id=runtime.project,
                path=path,
                params=params,
                response_model=response_model,
            )
        return data
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
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_cost_analysis_response",
                message="The cost analysis response was invalid.",
            ),
        )


def baseline(
    context: typer.Context,
    days: Annotated[
        int,
        typer.Option(
            "--days",
            min=3,
            max=30,
            help="Lookback days for the daily spend baseline (excludes today).",
        ),
    ] = 7,
) -> None:
    """Show the transactional daily spend baseline, excluding today."""
    data = cast(
        BaselineResult,
        _request(
            context,
            path="spend-baseline",
            params={"days": days},
            response_model=BaselineResult,
        ),
    )
    output_engine(context).write_success(
        schema="solwyn.costs.baseline.v1",
        data=data,
        table=TableData(
            columns=(
                "Lookback",
                "Days with data",
                "Mean / day",
                "Stdev / day",
                "Armed",
                "Freshness",
            ),
            rows=(
                (
                    str(data.lookback_days),
                    str(data.days_with_data),
                    f"{data.mean_daily_spend:.6f}",
                    f"{data.stdev_daily_spend:.6f}",
                    "yes" if data.armed else "no",
                    "committed closed days",
                ),
            ),
            right_align=("Lookback", "Days with data", "Mean / day", "Stdev / day"),
        ),
    )


def _delta_row(label: str, cell: DeltaCell) -> tuple[str, str, str, str, str]:
    previous = "—" if cell.prev_value is None else f"{cell.prev_value:.6f}"
    delta = "—" if cell.delta_pct is None else f"{cell.delta_pct:+.2%}"
    return label, f"{cell.value:.6f}", previous, delta, cell.context


def unit_economics(
    context: typer.Context,
    time_range: Annotated[
        UnitEconomicsRange, typer.Option("--range", help="7d, 30d, or 90d.")
    ] = UnitEconomicsRange.THIRTY_DAYS,
    vs_prev: Annotated[
        bool,
        typer.Option(
            "--vs-prev/--no-vs-prev",
            help="Compare with the preceding same-length window.",
        ),
    ] = True,
    cause: Annotated[
        bool, typer.Option("--cause", help="Include server-inferred cost drivers.")
    ] = False,
) -> None:
    """Show unit-cost metrics and optional suspected causes."""
    data = cast(
        UnitEconomicsResponse,
        _request(
            context,
            path="unit-economics",
            params={"range": time_range.value, "vs_prev": vs_prev, "cause": cause},
            response_model=UnitEconomicsResponse,
        ),
    )
    rows = [
        _delta_row("Cost / request", data.cost_per_request),
        _delta_row("Tokens / dollar", data.tokens_per_dollar),
        _delta_row("Avg prompt tokens", data.avg_prompt_tokens),
    ]
    if data.cost_per_agent_run is not None:
        rows.insert(1, _delta_row("Cost / agent run", data.cost_per_agent_run))
    if data.suspected_cause is not None:
        typer.echo(
            _terminal_safe(
                f"Suspected cause ({data.suspected_cause.confidence}): {data.suspected_cause.summary}"
            ),
            err=True,
        )
        for span in data.suspected_cause.spans:
            typer.echo(
                _terminal_safe(
                    f"Cause {span.kind}: {span.label} ({span.contribution_pct:.2f}%)."
                ),
                err=True,
            )
    output_engine(context).write_success(
        schema="solwyn.costs.unit-economics.v1",
        data=data,
        table=TableData(
            columns=("Metric", "Value", "Previous", "Delta", "Context"),
            rows=tuple(rows),
            right_align=("Value", "Previous", "Delta"),
        ),
    )


def drift(
    context: typer.Context,
    time_range: Annotated[
        str, typer.Option("--range", help="Preset analytics window.")
    ] = "7d",
    from_: Annotated[
        str | None,
        typer.Option("--from", help="Timezone-aware ISO start; requires --to."),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Timezone-aware ISO exclusive end; requires --from."),
    ] = None,
) -> None:
    """Compare stored actual spend with pre-flight estimates."""
    try:
        params = window_params(time_range=time_range, from_=from_, to=to)
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_cost_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    data = cast(
        CostDriftResponse,
        _request(
            context,
            path="costs/drift",
            params=params,
            response_model=CostDriftResponse,
        ),
    )
    report_window(data.window)
    estimate_heuristic = (
        "server-side input tokens equal output tokens"
        if data.caveats.estimate_heuristic == "server_side_input_equals_output"
        else data.caveats.estimate_heuristic
    )
    typer.echo(
        _terminal_safe(
            f"Drift caveats: estimate heuristic is {estimate_heuristic}; null estimates "
            "excluded; unit-priced and unpriced estimates are null; denied events included."
        ),
        err=True,
    )
    typer.echo(
        f"Drift coverage: {data.included_event_count} estimates included; "
        f"{data.missing_estimate_count} events missing estimates.",
        err=True,
    )
    rows = [
        (
            group.provider,
            group.model,
            f"{group.actual_cost:.6f}",
            f"{group.estimated_cost:.6f}",
            f"{group.difference:+.6f}",
            str(group.event_count),
        )
        for group in data.groups
    ]
    rows.append(
        (
            "Total",
            "—",
            f"{data.actual_total:.6f}",
            f"{data.estimate_total:.6f}",
            f"{data.difference:+.6f}",
            str(data.included_event_count),
        )
    )
    output_engine(context).write_success(
        schema="solwyn.costs.drift.v1",
        data=data,
        table=TableData(
            columns=(
                "Provider",
                "Model",
                "Actual",
                "Estimated",
                "Difference",
                "Events",
            ),
            rows=tuple(rows),
            right_align=("Actual", "Estimated", "Difference", "Events"),
        ),
    )


def register_cost_analysis_commands(app: typer.Typer) -> None:
    """Attach cost-analysis commands to the public costs group."""
    app.command(
        "baseline",
        epilog=help_contract(
            endpoint="GET /api/v1/projects/{project}/spend-baseline",
            auth="any",
            exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
            schema="solwyn.costs.baseline.v1",
        ),
    )(baseline)
    app.command(
        "unit-economics",
        epilog=help_contract(
            endpoint="GET /api/v1/projects/{project}/unit-economics",
            auth="any",
            exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
            schema="solwyn.costs.unit-economics.v1",
        ),
    )(unit_economics)
    app.command(
        "drift",
        epilog=help_contract(
            endpoint="GET /api/v1/projects/{project}/costs/drift",
            auth="any",
            exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
            schema="solwyn.costs.drift.v1",
        ),
    )(drift)
