"""Aggregated project cost reporting command."""

from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


class CostRange(StrEnum):
    """Supported aggregate windows frozen for the v1 command."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    YEAR = "365d"


class CostGrouping(StrEnum):
    """Supported aggregate dimensions frozen for the v1 command."""

    DAY = "day"
    MODEL = "model"
    PROVIDER = "provider"


class _CostGroup(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    group_key: str
    provider: str | None = None
    total_cost: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    unpriced_call_count: int = 0
    local_call_count: int = 0


class _CostsData(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    project_id: str
    range: Literal["7d", "30d", "90d", "365d"]
    group_by: Literal["day", "model", "provider"]
    groups: list[_CostGroup]


def _table(data: _CostsData) -> TableData:
    return TableData(
        columns=(
            "Group",
            "Provider",
            "Cost",
            "Input",
            "Output",
            "Calls",
            "Unpriced",
            "Local",
        ),
        rows=tuple(
            (
                group.group_key,
                group.provider or "-",
                f"{group.total_cost:.6f}",
                str(group.total_input_tokens),
                str(group.total_output_tokens),
                str(group.call_count),
                str(group.unpriced_call_count),
                str(group.local_call_count),
            )
            for group in data.groups
        ),
        right_align=(
            "Cost",
            "Input",
            "Output",
            "Calls",
            "Unpriced",
            "Local",
        ),
    )


def costs(
    context: typer.Context,
    time_range: Annotated[
        CostRange,
        typer.Option("--range", help="Aggregation range."),
    ] = CostRange.SEVEN_DAYS,
    group_by: Annotated[
        CostGrouping,
        typer.Option("--group-by", help="Aggregation dimension."),
    ] = CostGrouping.MODEL,
) -> None:
    """Show aggregate project costs."""
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
            response = client.request(
                "GET",
                f"/api/v1/projects/{project}/costs",
                params={"range": time_range.value, "group_by": group_by.value},
            )
        data = _CostsData.model_validate(response.json())
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
                code="invalid_costs_response",
                message="The costs response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.costs.v1",
        data=data,
        table=_table(data),
    )
