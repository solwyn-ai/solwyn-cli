"""Project priced-model discovery and historical cost simulation commands."""

from typing import Annotated, NoReturn
from urllib.parse import quote

import typer
from pydantic import ValidationError

from solwyn_cli._vendor.wire import (
    CostSimulationRequest,
    ProviderName,
    ServiceTier,
)
from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._cost_advice_models import (
    CostSimulationResponse,
)
from solwyn_cli.commands._cost_filters import window_params
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands.costs import report_window
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import CommandContext, command_context, output_engine


def _runtime_project(context: typer.Context) -> tuple[CommandContext, str]:
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


def _handle_failure(context: typer.Context, error: BaseException) -> NoReturn:
    if isinstance(error, ApiError):
        exit_with_error(
            context,
            error,
            semantic=ErrorSemantic.USAGE if error.status_code in {400, 422} else None,
        )
    if isinstance(error, AuthError):
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    exit_with_error(
        context,
        OutputError(
            code="invalid_cost_pricing_response",
            message="The cost pricing response was invalid.",
        ),
    )


def _counted(noun: str, count: int) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _report_simulation_caveats(data: CostSimulationResponse) -> None:
    baseline_only = data.excluded.modality_mismatch + data.excluded.missing_breakdown
    outside_both = data.excluded.unpriced + data.excluded.local
    typer.echo(
        "Simulation populations: "
        f"scanned {_counted('event', data.scanned_event_count)}; "
        "baseline uses stored actual cost for "
        f"{_counted('priced served event', data.baseline_event_count)}; "
        "current-rate repricing includes "
        f"{_counted('compatible event', data.included_event_count)}.",
        err=True,
    )
    typer.echo(
        f"{_counted('modality-mismatch/missing-breakdown event', baseline_only)} "
        f"{'remains' if baseline_only == 1 else 'remain'} in the baseline but "
        f"{'is' if baseline_only == 1 else 'are'} excluded from repricing; "
        f"{_counted('unpriced/local event', outside_both)} "
        f"{'is' if outside_both == 1 else 'are'} excluded from both costs.",
        err=True,
    )
    if data.baseline_event_count != data.included_event_count:
        typer.echo(
            "Unequal event sets: the delta is current-rate repriced cost minus the "
            "stored-cost baseline across different populations.",
            err=True,
        )
    typer.echo(
        f"Pricing caveats: {data.caveats.pricing_version} current-rate snapshot; "
        "tokenizer equivalence is approximate.",
        err=True,
    )


def priced_models(context: typer.Context) -> None:
    """List exact models available to the current pricing catalog."""
    runtime, _ = _runtime_project(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            from solwyn_cli.commands._cost_reads import fetch_priced_models

            data = fetch_priced_models(client=client, project_id=runtime.project)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        _handle_failure(context, error)
    output_engine(context).write_success(
        schema="solwyn.costs.priced-models.v1",
        data=data,
        table=TableData(
            columns=("Provider", "Model", "Modality", "Pricing version"),
            rows=tuple(
                (
                    target.provider.value,
                    target.model,
                    target.modality,
                    data.pricing_version,
                )
                for target in data.models
            ),
        ),
    )


def simulate(
    context: typer.Context,
    provider: Annotated[ProviderName, typer.Option("--provider")],
    model: Annotated[
        str,
        typer.Option("--model", help="Exact model identifier from priced-models."),
    ],
    region: Annotated[
        str | None,
        typer.Option("--region", help="Required with --service-tier for Bedrock."),
    ] = None,
    service_tier: Annotated[
        ServiceTier | None,
        typer.Option("--service-tier", help="Required with --region for Bedrock."),
    ] = None,
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
    """Replay historical usage against one current priced target."""
    stripped_region = region.strip() if region is not None else None
    if provider is ProviderName.BEDROCK:
        if not stripped_region or service_tier is None:
            exit_with_error(
                context,
                OutputError(
                    code="invalid_cost_simulation",
                    message="Bedrock simulation requires explicit target_region and target_service_tier",
                ),
                semantic=ErrorSemantic.USAGE,
            )
    elif region is not None or service_tier is not None:
        exit_with_error(
            context,
            OutputError(
                code="invalid_cost_simulation",
                message="target_region and target_service_tier are only supported for Bedrock",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        params = window_params(time_range=time_range, from_=from_, to=to)
        request = CostSimulationRequest(
            target_provider=provider,
            target_model=model,
            target_region=stripped_region,
            target_service_tier=service_tier,
        )
    except (ValidationError, ValueError) as error:
        exit_with_error(
            context,
            OutputError(code="invalid_cost_simulation", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    runtime, _ = _runtime_project(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            from solwyn_cli.commands._cost_reads import fetch_project_read

            data = fetch_project_read(
                client=client,
                project_id=runtime.project,
                path="costs/simulate",
                params=params,
                body=request,
                response_model=CostSimulationResponse,
            )
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        _handle_failure(context, error)
    report_window(data.window)
    _report_simulation_caveats(data)
    output_engine(context).write_success(
        schema="solwyn.costs.simulation.v1",
        data=data,
        table=TableData(
            columns=("Metric", "Value"),
            rows=(
                ("Provider", data.target.provider.value),
                ("Model", data.target.model),
                ("Modality", data.target.modality),
                ("Region", data.target.region or "—"),
                (
                    "Service tier",
                    data.target.service_tier.value
                    if data.target.service_tier is not None
                    else "—",
                ),
                ("Baseline", f"{data.baseline_actual_cost:.6f}"),
                ("Simulated", f"{data.simulated_cost:.6f}"),
                ("Delta (repriced - baseline)", f"{data.delta_cost:+.6f}"),
                ("Scanned events", str(data.scanned_event_count)),
                ("Baseline events", str(data.baseline_event_count)),
                ("Repriced events", str(data.included_event_count)),
                ("Excluded events", str(data.excluded_event_count)),
            ),
        ),
    )


def register_cost_pricing_commands(app: typer.Typer) -> None:
    """Attach pricing commands to the public costs group."""
    app.command(
        "priced-models",
        epilog=help_contract(
            endpoint="GET /api/v1/projects/{project}/costs/priced-models",
            auth="any",
            exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
            schema="solwyn.costs.priced-models.v1",
        ),
    )(priced_models)
    app.command(
        "simulate",
        epilog=help_contract(
            endpoint="POST /api/v1/projects/{project}/costs/simulate",
            auth="any",
            exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
            schema="solwyn.costs.simulation.v1",
        ),
    )(simulate)
