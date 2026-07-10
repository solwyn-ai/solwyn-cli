"""Project metadata list and show commands."""

from urllib.parse import quote

import typer
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli.auth import (
    AuthError,
    ProjectKeyCredential,
    ResolvedCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(help="Inspect Solwyn projects.")


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str
    name: str
    budget_limit: float
    budget_period: str
    budget_mode: str
    created_at: AwareDatetime


class _ProjectsListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: list[_Project]


_PROJECTS = TypeAdapter(list[_Project])


def _date_time(value: AwareDatetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _credential(context: typer.Context) -> ResolvedCredential:
    try:
        credential = resolve_credential()
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is None:
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Authentication is required.",
                hint="Set SOLWYN_API_KEY or run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def _list_table(data: _ProjectsListData) -> TableData:
    return TableData(
        columns=("ID", "Name", "Budget", "Period", "Mode", "Created"),
        rows=tuple(
            (
                project.id,
                project.name,
                f"{project.budget_limit:.2f}",
                project.budget_period,
                project.budget_mode,
                _date_time(project.created_at),
            )
            for project in data.projects
        ),
        right_align=("Budget", "Created"),
    )


def _show_table(project: _Project) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("ID", project.id),
            ("Name", project.name),
            ("Budget limit", f"{project.budget_limit:.2f}"),
            ("Budget period", project.budget_period),
            ("Budget mode", project.budget_mode),
            ("Created", _date_time(project.created_at)),
        ),
        right_align=("Value",),
    )


@app.command("list")
def list_projects(context: typer.Context) -> None:
    """List projects available to the logged-in user."""
    credential = _credential(context)
    if isinstance(credential, ProjectKeyCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to list projects.",
                hint=("Run solwyn login without --with-key, then retry projects list."),
            ),
            semantic=ErrorSemantic.AUTH,
        )

    runtime = command_context(context)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            response = client.request("GET", "/api/v1/projects")
        data = _ProjectsListData(projects=_PROJECTS.validate_python(response.json()))
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
                code="invalid_projects_response",
                message="The projects response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.projects-list.v1",
        data=data,
        table=_list_table(data),
    )


@app.command("show")
def show_project(context: typer.Context) -> None:
    """Show the project selected by the global project context."""
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

    credential = _credential(context)
    project_id = runtime.project
    project_path = quote(project_id, safe="")
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            response = client.request("GET", f"/api/v1/projects/{project_path}")
        project = _Project.model_validate(response.json())
        if isinstance(credential, ProjectKeyCredential) and project.id != project_id:
            raise ValueError("Key-scoped project response did not match the request.")
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
                code="invalid_projects_response",
                message="The projects response was invalid.",
            ),
        )

    output_engine(context).write_success(
        schema="solwyn.projects-show.v1",
        data=project,
        table=_show_table(project),
    )
