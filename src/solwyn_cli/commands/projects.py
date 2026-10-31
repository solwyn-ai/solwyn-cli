"""Project creation, metadata inspection, and lifecycle commands."""

import math
import sys
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
from solwyn_cli._vendor.wire import (
    BudgetMode,
    BudgetPeriod,
    ProjectCreateRequest,
    ProjectKeyScope,
    ProjectUpdateRequest,
)
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    LoginCredential,
    ProjectKeyCredential,
    resolve_credential,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.config import ConfigLoadError, ConfigWriteError, clear_default_project
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.commands.keys import export_line
from solwyn_cli.output import (
    OneTimeSecretPanel,
    OutputError,
    TableData,
    _terminal_safe,
)
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(
    help="Inspect Solwyn projects.",
    epilog=help_contract(
        endpoint="composite router for project subcommands",
        auth="varies by subcommand",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    ),
)


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
Project = _Project
ProjectsListData = _ProjectsListData


class _CreatedProject(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    budget_limit: float = Field(gt=0, allow_inf_nan=False)
    budget_period: BudgetPeriod
    budget_mode: BudgetMode
    created_at: AwareDatetime
    key_prefix: str = Field(pattern=r"^sk_proj_[a-f0-9]{16}$")
    scope: ProjectKeyScope


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


ProjectCreateResponse = _ProjectCreateResponse


def fetch_projects(*, client: SolwynClient) -> list[Project]:
    """Fetch and validate the projects visible to a login."""
    response = client.request("GET", "/api/v1/projects")
    return _PROJECTS.validate_python(response.json())


def request_project_creation(
    *,
    client: SolwynClient,
    request: ProjectCreateRequest,
) -> ProjectCreateResponse:
    """Create and validate a project through the no-replay wire boundary."""
    response = client.request(
        "POST",
        "/api/v1/projects",
        # exclude_unset posts exactly the fields the caller chose; anything left
        # unset falls to the API default the mirror declares and the drift guard
        # pins.
        json=request.model_dump(mode="json", exclude_unset=True),
        retry_rate_limit=False,
    )
    if response.status_code != 201:
        raise ValueError("Project creation did not return 201")
    return _ProjectCreateResponse.model_validate(response.json())


class _ProjectCreateDisplayData(_CreatedProject):
    key: SecretStr

    @field_serializer("key")
    def serialize_key(self, value: SecretStr) -> str:
        return value.get_secret_value()


class _ProjectCreateStoredData(_CreatedProject):
    stored: bool = True


class _ProjectDeleteData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    archived: bool = True
    api_keys_revoked: bool = True


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
        ("Scope", project.scope.value),
    )
    if stored:
        rows = (*rows, ("Stored", "true"))
    return TableData(
        columns=("Field", "Value"),
        rows=rows,
        right_align=("Value",),
    )


def _delete_table(data: _ProjectDeleteData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Archived", "true"),
            ("API keys revoked", "true"),
        ),
        right_align=("Value",),
    )


def _login_credential_for_create(context: typer.Context) -> LoginCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = "Run solwyn login, then retry projects create."
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


def _login_credential_for_mutation(
    context: typer.Context, *, operation: str
) -> LoginCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = f"Run solwyn login, then retry projects {operation}."
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to manage project lifecycle.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def _validated_project_id(context: typer.Context, project_id: str) -> str:
    normalized = project_id.strip()
    if not normalized:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project identifier is required.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return normalized


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _invalid_projects_response(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="invalid_projects_response",
            message="The projects response was invalid.",
        ),
    )


@app.command(
    "create",
    epilog=help_contract(
        endpoint="POST /api/v1/projects",
        auth="login",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.projects-create.v1",
        guidance=(
            "The new project key is shown exactly once or stored directly in the OS keychain.",
        ),
    ),
)
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
    scope: Annotated[
        ProjectKeyScope | None,
        typer.Option(
            "--scope",
            help="Scope for the project's first key; omit for the API default.",
        ),
    ] = None,
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
            # Left unset when no scope is chosen so the field is omitted rather
            # than posted: the API rejects a null scope on creation.
            **({} if scope is None else {"scope": scope.value}),
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

    credential = _login_credential_for_create(context)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            created = request_project_creation(client=client, request=request)
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
            footer=export_line(raw_key),
        ),
    )


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/projects",
        auth="login",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.projects-list.v1",
    ),
)
def list_projects(context: typer.Context) -> None:
    """List projects available to the logged-in user."""
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = "Run solwyn login, then retry projects list."
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
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = _ProjectsListData(projects=fetch_projects(client=client))
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


@app.command(
    "show",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}",
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.projects-show.v1",
    ),
)
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


@app.command(
    "rename",
    epilog=help_contract(
        endpoint="PUT /api/v1/projects/{id}",
        auth="login (owner or admin)",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.projects-rename.v1",
    ),
)
def rename_project(
    context: typer.Context,
    project_id: Annotated[str, typer.Argument(help="Project identifier to rename.")],
    name: Annotated[str, typer.Option("--name", help="New project name.")],
) -> None:
    """Rename a project as an account owner or admin."""
    validated_project_id = _validated_project_id(context, project_id)
    try:
        request = ProjectUpdateRequest(name=name.strip())
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_update_request",
                message="The project rename request is invalid.",
                hint="Provide a nonblank name of at most 100 characters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _login_credential_for_mutation(context, operation="rename")
    runtime = command_context(context)
    path = quote(validated_project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "PUT",
                f"/api/v1/projects/{path}",
                json=request.model_dump(mode="json"),
            )
        if response.status_code != 200:
            raise ValueError("Project rename did not return 200")
        project = _Project.model_validate(response.json())
    except ApiError as error:
        semantic = (
            ErrorSemantic.USAGE
            if error.status_code == 400 and error.message == "No fields to update"
            else None
        )
        exit_with_error(context, error, semantic=semantic)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_projects_response(context)

    output_engine(context).write_success(
        schema="solwyn.projects-rename.v1",
        data=project,
        table=_show_table(project),
    )


@app.command(
    "delete",
    epilog=help_contract(
        endpoint="DELETE /api/v1/projects/{id}",
        auth="login (owner or admin)",
        exits=(
            "0 success; 1 generic/API or local config cleanup; "
            "2 usage or confirmation; 3 auth; 5 not found"
        ),
        schema="solwyn.projects-delete.v1 (CLI output after bodyless 204)",
        guidance=(
            "Irreversible: archives the project, revokes every project key in the same transaction, and the project immediately becomes unavailable.",
            "Pass --yes; an interactive terminal must also type the exact project name.",
        ),
    ),
)
def delete_project(
    context: typer.Context,
    project_id: Annotated[str, typer.Argument(help="Project identifier to archive.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Acknowledge the irreversible project deletion."),
    ] = False,
) -> None:
    """Irreversibly archive a project and revoke all of its keys."""
    validated_project_id = _validated_project_id(context, project_id)
    if not yes:
        exit_with_error(
            context,
            OutputError(
                code="confirmation_required",
                message="Project deletion requires --yes.",
                hint="This action is irreversible and revokes every project key.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _login_credential_for_mutation(context, operation="delete")
    runtime = command_context(context)
    path = quote(validated_project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            if _stdin_is_tty():
                response = client.request("GET", f"/api/v1/projects/{path}")
                if response.status_code != 200:
                    raise ValueError("Project lookup did not return 200")
                project = _Project.model_validate(response.json())
                shown = _terminal_safe(project.name)
                # A sanitized name can equal another project's real name, so it
                # never confirms; the ID being deleted does instead.
                if shown == project.name:
                    label, expected = f"project name '{shown}'", project.name
                else:
                    label = f"project ID '{_terminal_safe(validated_project_id)}'"
                    expected = validated_project_id
                confirmation = typer.prompt(
                    _terminal_safe(f"Type the {label} to confirm"),
                    err=True,
                    hide_input=True,
                )
                if confirmation != expected:
                    exit_with_error(
                        context,
                        OutputError(
                            code="confirmation_failed",
                            message=(
                                "The project name did not match; nothing was deleted."
                                if shown == project.name
                                else "The project ID did not match; nothing was deleted."
                            ),
                        ),
                        semantic=ErrorSemantic.USAGE,
                    )
            response = client.request(
                "DELETE",
                f"/api/v1/projects/{path}",
            )
        if response.status_code != 204 or response.content:
            raise ValueError("Project deletion did not return a bodyless 204")
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_projects_response(context)

    try:
        clear_default_project(validated_project_id)
    except (ConfigLoadError, ConfigWriteError):
        exit_with_error(
            context,
            OutputError(
                code="project_deleted_config_update_failed",
                message=(
                    "The project was archived and its keys were revoked, but the "
                    "matching local default_project could not be cleared."
                ),
                hint=(
                    "Repair config.toml or select a live project with "
                    "solwyn config set default_project PROJECT."
                ),
            ),
        )

    data = _ProjectDeleteData(project_id=validated_project_id)
    output_engine(context).write_success(
        schema="solwyn.projects-delete.v1",
        data=data,
        table=_delete_table(data),
    )
