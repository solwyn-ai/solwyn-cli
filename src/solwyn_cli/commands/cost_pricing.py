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
    PricedModelsResponse,
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


def priced_models(context: typer.Context) -> None:
    """List exact models available to the current pricing catalog."""
    runtime, project_path = _runtime_project(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = PricedModelsResponse.model_validate(
                client.request(
                    "GET", f"/api/v1/projects/{project_path}/costs/priced-models"
                ).json()
            )
        if data.project_id != runtime.project:
            raise ValueError(
                "Priced-models response project did not match the request."
            )
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
    runtime, project_path = _runtime_project(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = CostSimulationResponse.model_validate(
                client.request(
                    "POST",
                    f"/api/v1/projects/{project_path}/costs/simulate",
                    params=params,
                    json=request.model_dump(mode="json"),
                ).json()
            )
        if data.project_id != runtime.project:
            raise ValueError(
                "Cost simulation response project did not match the request."
            )
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        _handle_failure(context, error)
    report_window(data.window)
    typer.echo(
        f"Simulation pricing snapshot: {data.caveats.pricing_version}; excluded unpriced {data.excluded.unpriced}, local {data.excluded.local}, modality mismatch {data.excluded.modality_mismatch}, missing breakdown {data.excluded.missing_breakdown}.",
        err=True,
    )
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
                ("Delta", f"{data.delta_cost:+.6f}"),
                ("Included events", str(data.included_event_count)),
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
