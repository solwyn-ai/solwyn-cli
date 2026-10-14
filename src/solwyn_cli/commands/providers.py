"""Provider visibility commands."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._entitlement import get_capabilities
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(
    help="Inspect provider failovers and advisory untracked surfaces.",
    add_completion=False,
    no_args_is_help=True,
    epilog=help_contract(
        endpoint="composite router for provider visibility subcommands",
        auth="any",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    ),
)


FAILOVERS_HELP = help_contract(
    endpoint="GET /api/v1/projects/{project}/providers/failovers",
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.providers.failovers.v1",
)


UNTRACKED_HELP = help_contract(
    endpoint="GET /api/v1/projects/{project}/untracked-surfaces",
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
    schema="solwyn.providers.untracked.v1",
)


class FailoverRange(StrEnum):
    """Supported failover aggregation windows."""

    DAY = "24h"
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"


class UntrackedRange(StrEnum):
    """Supported untracked-surface observation windows."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"


_UNTRACKED_RANGE_DAYS = {
    UntrackedRange.SEVEN_DAYS: 7,
    UntrackedRange.THIRTY_DAYS: 30,
    UntrackedRange.NINETY_DAYS: 90,
}
_FAILOVER_REASON_LABELS = {
    "circuit_open": "Circuit open",
    "primary_error": "Primary error",
    "model_fallback": "Model fallback",
    "cost_routed": "Cost routed",
}


class ProviderFailoverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    requested_provider: str | None
    requested_model: str | None
    served_provider: str
    served_model: str
    failover_reason: str | None
    failover_error_class: str | None
    event_count: int
    total_cost_usd: float
    first_seen: datetime
    last_seen: datetime
    is_unpriced: bool


class ProviderFailoversData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    range: Literal["24h", "7d", "30d"]
    failovers: list[ProviderFailoverResponse]


class UntrackedSurfaceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: str
    client_shape: str
    mode: str
    surface: str
    rule_kind: str
    capability_scope: str | None
    posture: str
    occurrences: Annotated[int, Field(ge=0)]
    first_seen_at: datetime
    last_seen_at: datetime


class UntrackedSurfacesData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    requested_range: Literal["7d", "30d", "90d"]
    requested_window_days: int
    effective_window_days: int | None
    data_retention_days: int | None
    window_source: Literal["billing_status", "unknown"]
    surfaces: list[UntrackedSurfaceResponse]


_FAILOVERS = TypeAdapter(list[ProviderFailoverResponse])
_UNTRACKED_SURFACES = TypeAdapter(list[UntrackedSurfaceResponse])


def fetch_provider_failovers(
    *,
    client: SolwynClient,
    project_id: str,
    time_range: FailoverRange,
) -> ProviderFailoversData:
    """Read and validate recent provider/model failover groups."""
    project = quote(project_id, safe="")
    response = client.request(
        "GET",
        f"/api/v1/projects/{project}/providers/failovers",
        params={"range": time_range.value},
    )
    return ProviderFailoversData(
        project_id=project_id,
        range=time_range.value,
        failovers=_FAILOVERS.validate_python(response.json()),
    )


def fetch_untracked_surfaces(
    *,
    client: SolwynClient,
    project_id: str,
    time_range: UntrackedRange,
) -> UntrackedSurfacesData:
    """Read observations and state the effective retention-clamped window."""
    requested_days = _UNTRACKED_RANGE_DAYS[time_range]
    capabilities = get_capabilities(client=client)
    retention_days = (
        capabilities.data_retention_days if capabilities is not None else None
    )
    effective_days = (
        min(requested_days, retention_days) if retention_days is not None else None
    )
    project = quote(project_id, safe="")
    response = client.request(
        "GET",
        f"/api/v1/projects/{project}/untracked-surfaces",
        params={"range": time_range.value},
    )
    return UntrackedSurfacesData(
        project_id=project_id,
        requested_range=time_range.value,
        requested_window_days=requested_days,
        effective_window_days=effective_days,
        data_retention_days=retention_days,
        window_source="billing_status" if capabilities is not None else "unknown",
        surfaces=_UNTRACKED_SURFACES.validate_python(response.json()),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def failovers_table(data: ProviderFailoversData) -> TableData:
    """Render requested-to-served identity and truthful cost posture."""
    rows: list[tuple[str, str]] = [
        ("Project", data.project_id),
        ("Range", data.range),
    ]
    for index, failover in enumerate(data.failovers, start=1):
        requested_provider = failover.requested_provider or "-"
        requested_model = failover.requested_model or "-"
        reason = (
            "-"
            if failover.failover_reason is None
            else _FAILOVER_REASON_LABELS.get(
                failover.failover_reason,
                failover.failover_reason,
            )
        )
        cost = (
            f"unpriced (${failover.total_cost_usd:.6f} reported)"
            if failover.is_unpriced
            else f"${failover.total_cost_usd:.6f}"
        )
        rows.extend(
            (
                (
                    f"{index} route",
                    f"{requested_provider}/{requested_model} -> "
                    f"{failover.served_provider}/{failover.served_model}",
                ),
                (f"{index} reason", reason),
                (f"{index} error class", failover.failover_error_class or "-"),
                (f"{index} events", str(failover.event_count)),
                (f"{index} cost", cost),
                (
                    f"{index} first/last",
                    f"{_timestamp(failover.first_seen)} / "
                    f"{_timestamp(failover.last_seen)}",
                ),
            )
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def untracked_table(data: UntrackedSurfacesData) -> TableData:
    """Render five-part identity, posture, and advisory occurrence counts."""
    effective = (
        f"{data.effective_window_days} days"
        if data.effective_window_days is not None
        else "unknown"
    )
    retention = (
        f"{data.data_retention_days} days"
        if data.data_retention_days is not None
        else "unknown"
    )
    rows: list[tuple[str, str]] = [
        ("Project", data.project_id),
        ("Requested window", f"{data.requested_window_days} days"),
        ("Effective window", effective),
        ("Data retention", retention),
    ]
    for index, surface in enumerate(data.surfaces, start=1):
        rows.extend(
            (
                (
                    f"{index} identity",
                    " | ".join(
                        (
                            surface.provider,
                            surface.client_shape,
                            surface.mode,
                            surface.surface,
                            surface.rule_kind,
                        )
                    ),
                ),
                (f"{index} capability scope", surface.capability_scope or "-"),
                (f"{index} posture", surface.posture),
                (
                    f"{index} occurrences",
                    f"{surface.occurrences} (advisory; never billing truth)",
                ),
                (
                    f"{index} first/last",
                    f"{_timestamp(surface.first_seen_at)} / "
                    f"{_timestamp(surface.last_seen_at)}",
                ),
            )
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def _project_required(context: typer.Context) -> str:
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
    return runtime.project


@app.command("failovers", epilog=FAILOVERS_HELP)
def failovers(
    context: typer.Context,
    time_range: Annotated[
        FailoverRange,
        typer.Option("--range", help="Failover aggregation range."),
    ] = FailoverRange.SEVEN_DAYS,
) -> None:
    """Show recent provider and model failover groups."""
    runtime = command_context(context)
    project_id = _project_required(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = fetch_provider_failovers(
                client=client,
                project_id=project_id,
                time_range=time_range,
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
                code="invalid_provider_failovers_response",
                message="The provider failovers response was invalid.",
            ),
        )

    typer.echo("Showing top 50 groups of an unknown total.", err=True)
    output_engine(context).write_success(
        schema="solwyn.providers.failovers.v1",
        data=data,
        table=failovers_table(data),
    )


@app.command("untracked", epilog=UNTRACKED_HELP)
def untracked(
    context: typer.Context,
    time_range: Annotated[
        UntrackedRange,
        typer.Option("--range", help="Observation range before retention clamp."),
    ] = UntrackedRange.SEVEN_DAYS,
) -> None:
    """Show advisory observations for unmetered provider surfaces."""
    runtime = command_context(context)
    project_id = _project_required(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = fetch_untracked_surfaces(
                client=client,
                project_id=project_id,
                time_range=time_range,
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
                code="invalid_untracked_surfaces_response",
                message="The untracked surfaces response was invalid.",
            ),
        )

    typer.echo("Showing top 25 observations of an unknown total.", err=True)
    output_engine(context).write_success(
        schema="solwyn.providers.untracked.v1",
        data=data,
        table=untracked_table(data),
    )
