"""Account-scoped tag discovery and namespace retirement."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli.auth import AuthError, JwtCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._pagination import read_pages
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands._tags import validate_tags
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


class TagKeySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    values_count: int = Field(ge=0)
    first_seen_at: datetime
    last_seen_at: datetime
    similar: list[str] = Field(default_factory=list)


class TagRegistryUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys_used: int = Field(ge=0, le=100)
    keys_cap: Literal[100] = 100
    values_cap_per_key: Literal[1000] = 1000


class TagDiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[TagKeySummary]
    usage: TagRegistryUsage
    scope: Literal["account"] = "account"


class TagValueSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    first_seen_at: datetime
    last_seen_at: datetime


class TagValuesPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    values: list[TagValueSummary]
    next_cursor: str | None
    has_more: bool


class TagRetirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    retired: bool = True


app = typer.Typer(
    help="Browse account-scoped tag keys and values; retire keys.",
    epilog=help_contract(
        endpoint="composite tag router",
        auth="varies by subcommand",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
        guidance=(
            "Discovery is account-scoped, including when requested through a project URL.",
        ),
    ),
)


def fetch_tags(*, client: SolwynClient, project_id: str) -> TagDiscoveryResponse:
    response = client.request(
        "GET", f"/api/v1/projects/{quote(project_id, safe='')}/tags"
    )
    return TagDiscoveryResponse.model_validate(response.json())


def fetch_tag_values(
    *,
    client: SolwynClient,
    project_id: str,
    key: str,
    prefix: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> TagValuesPage:
    validate_tags({key: prefix or ""})
    if not 1 <= limit <= 100:
        raise ValueError("Tag values limit must be 1–100.")
    params = {"key": key, "q": prefix, "limit": limit, "cursor": cursor}
    response = client.request(
        "GET",
        f"/api/v1/projects/{quote(project_id, safe='')}/tags/values",
        params={key: value for key, value in params.items() if value is not None},
    )
    data = TagValuesPage.model_validate(response.json())
    if data.key != key:
        raise ValueError("Tag values response key did not match the request.")
    return data


def discovery_table(data: TagDiscoveryResponse) -> TableData:
    return TableData(
        columns=("Key", "Values", "First seen", "Last seen", "Similar"),
        rows=tuple(
            (
                row.key,
                row.values_count,
                row.first_seen_at.isoformat(),
                row.last_seen_at.isoformat(),
                ", ".join(row.similar) or "-",
            )
            for row in data.keys
        ),
    )


def values_table(data: TagValuesPage) -> TableData:
    return TableData(
        columns=("Value", "First seen", "Last seen"),
        rows=tuple(
            (row.value, row.first_seen_at.isoformat(), row.last_seen_at.isoformat())
            for row in data.values
        ),
    )


def _read(
    context: typer.Context,
    *,
    key: str | None = None,
    prefix: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
    all_pages: bool = False,
) -> None:
    runtime = command_context(context)
    if runtime.project is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint="Use --project, SOLWYN_PROJECT_ID, or config default_project.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        if key is not None:
            validate_tags({key: prefix or ""})
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_tag_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = (
                fetch_tags(client=client, project_id=runtime.project)
                if key is None
                else read_pages(
                    lambda page_cursor: fetch_tag_values(
                        client=client,
                        project_id=runtime.project,
                        key=key,
                        prefix=prefix,
                        limit=limit,
                        cursor=page_cursor,
                    ),
                    field="values",
                    cursor=cursor,
                    all_pages=all_pages,
                )
            )
    except ApiError as error:
        if error.status_code == 402:
            typer.echo("Tag analytics need Team.", err=True)
        if error.status_code == 422:
            error = ApiError(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
                hint=(
                    "Check the tag key and prefix; use a cursor returned for the "
                    "same key and prefix, or omit --cursor to restart pagination."
                ),
                detail=error.detail,
            )
            exit_with_error(context, error, semantic=ErrorSemantic.USAGE)
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_tags_response", message="The tags response was invalid."
            ),
        )
    if isinstance(data, TagDiscoveryResponse):
        output_engine(context).write_success(
            schema="solwyn.tags.v1", data=data, table=discovery_table(data)
        )
        typer.echo(
            f"Account tag keys: {data.usage.keys_used}/{data.usage.keys_cap}.", err=True
        )
    else:
        output_engine(context).write_success(
            schema="solwyn.tags.values.v1", data=data, table=values_table(data)
        )
        if data.has_more:
            typer.echo(
                f"More values available; next cursor: {data.next_cursor}", err=True
            )


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/tags",
        auth="any",
        exits="0 success; 1 API/upgrade; 2 usage; 3 auth; 5 not found",
        schema="solwyn.tags.v1",
        guidance=(
            "Discovery is account-scoped and needs Team; sending and enforcing tags works on every tier.",
        ),
    ),
)
def list_tags(context: typer.Context) -> None:
    """List active keys across the account, including similar verbatim spellings."""
    _read(context)


@app.command(
    "values",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/tags/values",
        auth="any",
        exits="0 success; 1 API/upgrade; 2 usage; 3 auth; 5 not found",
        schema="solwyn.tags.values.v1",
        guidance=("Discovery is account-scoped; prefix matching is case-sensitive.",),
    ),
)
def tag_values(
    context: typer.Context,
    key: str,
    prefix: Annotated[str | None, typer.Option("--prefix")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    all_pages: Annotated[
        bool, typer.Option("--all", help="Follow all remaining cursor pages.")
    ] = False,
) -> None:
    """Read a page of values for one verbatim key."""
    _read(
        context, key=key, prefix=prefix, limit=limit, cursor=cursor, all_pages=all_pages
    )


@app.command(
    "retire",
    epilog=help_contract(
        endpoint="DELETE /api/v1/account/tags",
        auth="JWT admin/owner",
        exits="0 retired; 1 API; 2 usage; 3 auth; 5 never registered",
        schema="solwyn.tags.v1",
        guidance=(
            "Retirement frees an active key slot; later ingest may reactivate the key. Available on every tier.",
        ),
    ),
)
def retire_tag(context: typer.Context, key: str) -> None:
    """Retire a key while retaining historical values."""
    try:
        validate_tags({key: ""})
    except ValueError as error:
        exit_with_error(
            context,
            OutputError(code="invalid_tag_query", message=str(error)),
            semantic=ErrorSemantic.USAGE,
        )
    credential = required_credential(context, resolve=resolve_credential)
    if not isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="jwt_required",
                message="Tag retirement requires JWT authentication with admin or owner role.",
                hint="Unset SOLWYN_API_KEY and run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "DELETE", "/api/v1/account/tags", params={"key": key}
            )
            if response.status_code != 204 or response.content:
                raise ValueError("Tag retirement did not return empty 204.")
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_tags_response",
                message="The tag retirement response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.tags.v1",
        data=TagRetirement(key=key),
        table=TableData(columns=("Key", "Retired"), rows=((key, "yes"),)),
    )
    typer.echo(
        "Later successful ingest may reactivate this key if account capacity permits.",
        err=True,
    )
