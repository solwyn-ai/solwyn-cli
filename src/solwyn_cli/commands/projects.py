"""Project creation and metadata inspection commands."""

import math
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from solwyn_cli._vendor.validation import validate_project_key_format
from solwyn_cli._vendor.wire import BudgetMode, BudgetPeriod, ProjectCreateRequest
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OneTimeSecretPanel, OutputError, TableData
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


class _CreatedProject(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    budget_limit: float = Field(gt=0, allow_inf_nan=False)
    budget_period: BudgetPeriod
    budget_mode: BudgetMode
    created_at: AwareDatetime
    key_prefix: str = Field(pattern=r"^sk_proj_[a-f0-9]{16}$")


class _ProjectCreateResponse(_CreatedProject):
    key: SecretStr

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        validate_project_key_format(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def validate_key_prefix(self) -> "_ProjectCreateResponse":
        raw_key = self.key.get_secret_value()
        if self.key_prefix != raw_key[:24]:
            raise ValueError("Invalid project key prefix")
        return self


class _ProjectCreateDisplayData(_CreatedProject):
    key: SecretStr

    @field_serializer("key")
    def serialize_key(self, value: SecretStr) -> str:
        return value.get_secret_value()


class _ProjectCreateStoredData(_CreatedProject):
    stored: bool = True


def _date_time(value: AwareDatetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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


def _create_table(project: _CreatedProject, *, stored: bool = False) -> TableData:
    rows = (
        ("ID", project.id),
        ("Name", project.name),
        ("Budget limit", f"{project.budget_limit:.2f}"),
        ("Budget period", project.budget_period),
        ("Budget mode", project.budget_mode),
        ("Created", _date_time(project.created_at)),
        ("Key prefix", project.key_prefix),
    )
    if stored:
        rows = (*rows, ("Stored", "true"))
    return TableData(
        columns=("Field", "Value"),
        rows=rows,
        right_align=("Value",),
    )


def _jwt_credential_for_create(context: typer.Context) -> JwtCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry projects create."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry projects create."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to create projects.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


@app.command("create")
def create_project(
    context: typer.Context,
    name: Annotated[str, typer.Option("--name", help="Name for the new project.")],
    budget: Annotated[
        float,
        typer.Option("--budget", help="Initial project budget in USD."),
    ] = 100.0,
    period: Annotated[
        BudgetPeriod,
        typer.Option("--period", help="Initial budget period."),
    ] = BudgetPeriod.MONTHLY,
    mode: Annotated[
        BudgetMode,
        typer.Option("--mode", help="Initial budget enforcement mode."),
    ] = BudgetMode.ALERT_ONLY,
    store_key: Annotated[
        bool,
        typer.Option("--store-key", help="Store the new key in the OS keychain."),
    ] = False,
) -> None:
    """Create a project and capture its first API key exactly once."""
    if not name.strip() or not math.isfinite(budget) or budget <= 0:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_create_request",
                message="Project name and budget are invalid.",
                hint="Provide a nonblank name and a finite positive budget.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        request = ProjectCreateRequest(
            name=name.strip(),
            budget_limit=budget,
            budget_period=period.value,
            budget_mode=mode.value,
        )
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_create_request",
                message="The project creation request is invalid.",
                hint="Check the project name and initial budget settings.",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    credential = _jwt_credential_for_create(context)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                "/api/v1/projects",
                json=request.model_dump(mode="json"),
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 201:
            raise ValueError("Project creation did not return 201")
        created = _ProjectCreateResponse.model_validate(response.json())
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
                message="The project creation response was invalid.",
            ),
        )

    raw_key = created.key.get_secret_value()
    if store_key:
        try:
            store_project_key(raw_key)
        except CredentialStorageError as error:
            exit_with_error(
                context,
                OutputError(
                    code="credential_storage_failed",
                    message=str(error),
                    hint="Retry after configuring an operating-system keychain.",
                ),
                semantic=ErrorSemantic.AUTH,
            )
        stored_data = _ProjectCreateStoredData.model_validate(created.model_dump())
        output_engine(context).write_success(
            schema="solwyn.projects-create.v1",
            data=stored_data,
            table=_create_table(stored_data, stored=True),
        )
        return

    display_data = _ProjectCreateDisplayData.model_validate(created.model_dump())
    output_engine(context).write_success(
        schema="solwyn.projects-create.v1",
        data=display_data,
        table=_create_table(display_data),
        one_time_secret=OneTimeSecretPanel(
            title="One-time project key",
            value=created.key,
        ),
    )


@app.command("list")
def list_projects(context: typer.Context) -> None:
    """List projects available to the logged-in user."""
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry projects list."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry projects list."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to list projects.",
                hint=hint,
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

    credential = required_credential(context, resolve=resolve_credential)
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
