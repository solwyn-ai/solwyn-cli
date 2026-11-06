"""Project creation, metadata inspection, and lifecycle commands."""

import math
import sys
from pathlib import Path
from typing import Annotated, Any, Literal
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

from solwyn_cli import auth, repo_file
from solwyn_cli._vendor.validation import (
    API_KEY_PREFIX_LENGTH,
    API_KEY_PREFIX_PATTERN,
    validate_api_key_format,
)
from solwyn_cli._vendor.wire import (
    ApiKeyScope,
    BudgetMode,
    BudgetPeriod,
    ProjectCreateRequest,
    ProjectUpdateRequest,
)
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    LoginCredential,
    ApiKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import (
    command_project,
    credential_saved_note,
    exit_for_resolution,
    exit_project_required,
    refuse_retired_project_id,
    exit_with_error,
    legacy_keys_note,
    required_credential,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.commands.keys import export_lines
from solwyn_cli.project_resolver import ProjectResolutionError
from solwyn_cli.output import (
    OneTimeSecretPanel,
    OutputError,
    TableData,
    _terminal_safe,
)
from solwyn_cli.runtime import check_setup, command_context, output_engine
from solwyn_cli.money import money


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

    id: str = Field(pattern=r"^proj_[a-f0-9]{24}$")
    name: str = Field(min_length=1)
    budget_limit: float = Field(gt=0, allow_inf_nan=False)
    budget_period: BudgetPeriod
    budget_mode: BudgetMode
    created_at: AwareDatetime
    dedicated_keys_only: bool
    key_id: str = Field(min_length=1)
    key_prefix: str = Field(pattern=API_KEY_PREFIX_PATTERN.pattern)
    scope: ApiKeyScope
    created_via: Literal["dashboard", "cli"]
    expires_at: AwareDatetime


class _ProjectCreateResponse(_CreatedProject):
    key: SecretStr

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        validate_api_key_format(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def validate_key_prefix(self) -> "_ProjectCreateResponse":
        raw_key = self.key.get_secret_value()
        if self.key_prefix != raw_key[:API_KEY_PREFIX_LENGTH]:
            raise ValueError("Invalid API key prefix")
        return self


ProjectCreateResponse = _ProjectCreateResponse


def write_repo_file(project_name: str) -> Path | None:
    """Write the repo's .solwyn.env for a new project when there is none.

    Never overwrites one: if it names another project, say so on stderr.
    Returns the path written, or None.
    """
    try:
        outcome, path, existing = repo_file.write_if_missing(project_name)
    except (OSError, repo_file.RepoFileError):
        typer.echo(
            _terminal_safe(
                f"Could not write {repo_file.REPO_FILE_NAME}; set "
                f'SOLWYN_PROJECT="{project_name}" in it yourself.'
            ),
            err=True,
        )
        return None
    if outcome == "written":
        typer.echo(
            _terminal_safe(
                f"Wrote {path} (SOLWYN_PROJECT). Commit it; it holds no secret."
            ),
            err=True,
        )
        return path
    if outcome == "other":
        typer.echo(
            _terminal_safe(
                f"{repo_file.REPO_FILE_NAME} already names {existing}; not changed."
            ),
            err=True,
        )
    return None


def create_named_project(
    *,
    client: SolwynClient,
    name: str,
    fields: dict[str, Any],
    derived: bool,
) -> ProjectCreateResponse:
    """Create a project; a name derived from the repo avoids a taken one.

    A derived name becomes ``<name>-2``, ``-3``, ... up to ``-99`` when an
    active project already uses it (case-insensitively); Core's 409
    ``project_name_taken`` (a race) retries the next free name once. A name
    the user chose is posted as given.
    """
    if not derived:
        return request_project_creation(
            client=client, request=ProjectCreateRequest(name=name, **fields)
        )
    taken = {project.name for project in fetch_projects(client=client)}
    for attempt in range(2):
        chosen = repo_file.first_free_name(name, taken)
        if chosen is None:
            raise ProjectResolutionError(
                "project_name_taken",
                f"Projects {name} to {name}-99 already exist.",
                "Pass --name with another name.",
            )
        try:
            return request_project_creation(
                client=client, request=ProjectCreateRequest(name=chosen, **fields)
            )
        except ApiError as error:
            if error.code != "project_name_taken" or attempt == 1:
                raise
            taken.add(chosen)
    raise AssertionError("unreachable")


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
    # The .solwyn.env this command wrote, or null (outside a repo, or one
    # already existed: it is never overwritten).
    repo_file: str | None = None


class _ProjectDeleteData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    archived: bool = True
    removed_stored_key: bool = False


def _date_time(value: AwareDatetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _list_table(data: _ProjectsListData) -> TableData:
    return TableData(
        columns=("ID", "Name", "Budget", "Period", "Mode", "Created"),
        rows=tuple(
            (
                project.id,
                project.name,
                f"{money(project.budget_limit)}",
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
            ("Budget limit", f"{money(project.budget_limit)}"),
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
        ("Budget limit", f"{money(project.budget_limit)}"),
        ("Budget period", project.budget_period),
        ("Budget mode", project.budget_mode),
        ("Created", _date_time(project.created_at)),
        ("Key ID", project.key_id),
        ("Key prefix", project.key_prefix),
        ("Scope", project.scope.value),
        ("Key expires", _date_time(project.expires_at)),
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
            ("Removed stored key", "yes" if data.removed_stored_key else "no"),
        ),
        right_align=("Value",),
    )


def _login_credential_for_create(context: typer.Context) -> LoginCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ApiKeyCredential):
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
    if isinstance(credential, ApiKeyCredential):
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


def _login_account_id(api_url: str, credential: LoginCredential) -> str:
    """The login's account id: stored with the login, else /accounts/me."""
    from solwyn_cli.project_resolver import _Reads

    return _Reads(api_url=api_url, login=credential).account()[0]


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


def _stored_entry(api_url: str, project_id: str) -> str | None:
    """The name this computer stored with the project's key, if any."""
    try:
        entry = auth.stored_api_keys(api_url).get(project_id)
    except AuthError:
        return None
    return None if entry is None else entry.project_name


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
        endpoint=(
            "POST /api/v1/projects (GET /api/v1/projects first when the name "
            "comes from the repo)"
        ),
        auth="login",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.projects-create.v1",
        guidance=(
            "The new project's API key holds only this project. It is shown "
            "exactly once with export lines, or stored on this computer for the "
            "project (--store-key).",
            "--name defaults to the git repo's name; -2, -3, ... is added when "
            "that name is taken.",
            "--store-key also writes the repo's .solwyn.env (SOLWYN_PROJECT) "
            "when there is none; an existing one is never overwritten.",
        ),
    ),
)
def create_project(
    context: typer.Context,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help=(
                "Name for the new project. Defaults to the git repo's name "
                "(with -2, -3, ... when taken); required outside a repo."
            ),
        ),
    ] = None,
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
        ApiKeyScope | None,
        typer.Option(
            "--scope",
            help="Scope for the project's first key; omit for the API default.",
        ),
    ] = None,
    store_key: Annotated[
        bool,
        typer.Option("--store-key", help="Store the new key in the credential store."),
    ] = False,
) -> None:
    """Create a project and capture its first API key exactly once."""
    derived = name is None
    if derived or store_key:
        # The name or the repo file would be chosen for the user; a leftover
        # SOLWYN_PROJECT_ID says they meant another project.
        refuse_retired_project_id(context)
    if derived:
        name = repo_file.default_project_name()
        if name is None:
            exit_with_error(
                context,
                OutputError(
                    code="invalid_project_create_request",
                    message="A project name is required outside a git repo.",
                    hint="Pass --name, or run this inside the repo the project is for.",
                ),
                semantic=ErrorSemantic.USAGE,
            )
    assert name is not None
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
    fields: dict[str, Any] = {
        "budget_limit": budget,
        "budget_period": period.value,
        "budget_mode": mode.value,
        # Left unset when no scope is chosen so the field is omitted rather
        # than posted: the API rejects a null scope on creation.
        **({} if scope is None else {"scope": scope.value}),
    }
    try:
        ProjectCreateRequest(name=name.strip(), **fields)
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

    if store_key:
        legacy_keys_note(context)
    credential = _login_credential_for_create(context)
    runtime = command_context(context)
    # Before anything is created: the repo file agrees with --project and
    # SOLWYN_PROJECT, and the login's account (recorded with a stored key) is
    # known; a SOLWYN_TOKEN login asks /accounts/me and fails closed.
    try:
        account_id = check_setup(context, credential)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            created = create_named_project(
                client=client, name=name.strip(), fields=fields, derived=derived
            )
    except ProjectResolutionError as error:
        exit_for_resolution(context, error)
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
            stored_key = auth.store_api_key(
                project_id=created.id,
                key=raw_key,
                key_id=created.key_id,
                account_id=account_id or "",
                project_name=created.name,
                api_url=runtime.api_url,
            )
        except CredentialStorageError as error:
            exit_with_error(
                context,
                OutputError(
                    code="credential_storage_failed",
                    message=str(error),
                    hint=error.hint or "Retry after fixing the credential store.",
                ),
                semantic=ErrorSemantic.AUTH,
            )
        if isinstance(stored_key, ApiKeyCredential):
            if note := credential_saved_note(stored_key):
                typer.echo(_terminal_safe(note), err=True)
        repo_path = write_repo_file(created.name)
        stored_data = _ProjectCreateStoredData.model_validate(
            {
                **created.model_dump(),
                "repo_file": None if repo_path is None else str(repo_path),
            }
        )
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
            title="One-time API key",
            value=created.key,
            footer=export_lines(raw_key, created.name, runtime.api_url)[0],
        ),
        reveal={"key": created.key},
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
    if isinstance(credential, ApiKeyCredential):
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
        exit_project_required(context)

    credential = required_credential(context, resolve=resolve_credential)
    project_id = command_project(context, credential)
    project_path = quote(project_id, safe="")
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            response = client.request("GET", f"/api/v1/projects/{project_path}")
        project = _Project.model_validate(response.json())
        if isinstance(credential, ApiKeyCredential) and project.id != project_id:
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
    has_repo_file = repo_file.find_repo_file() is not None
    before: str | None = None
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            if has_repo_file:
                # The old name, to update a .solwyn.env that names it.
                before = _Project.model_validate(
                    client.request("GET", f"/api/v1/projects/{path}").json()
                ).name
            response = client.request(
                "PUT",
                f"/api/v1/projects/{path}",
                json=request.model_dump(mode="json"),
            )
        if response.status_code != 200:
            raise ValueError("Project rename did not return 200")
        project = _Project.model_validate(response.json())
        previous = _stored_entry(runtime.api_url, project.id)
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

    old_name = before or previous
    old = (
        f"SOLWYN_PROJECT={old_name}"
        if old_name is not None and old_name != project.name
        else "SOLWYN_PROJECT to the old name"
    )
    if before is not None and before != project.name:
        try:
            updated = repo_file.rename_if_named(before, project.name)
        except repo_file.RepoFileConflictError as conflict:
            updated = None
            typer.echo(
                _terminal_safe(
                    f"Did not update {conflict.path}: it changed while the CLI "
                    "was updating it, and the other change was kept"
                    + ("" if conflict.kept is None else f" in {conflict.kept}")
                    + f'. Check it names the project you want (now "{project.name}").'
                ),
                err=True,
            )
        except (OSError, repo_file.RepoFileError):
            updated = None
            typer.echo(
                _terminal_safe(
                    f"Could not update {repo_file.REPO_FILE_NAME}; set "
                    f'SOLWYN_PROJECT="{project.name}" in it yourself.'
                ),
                err=True,
            )
        if updated is not None:
            typer.echo(
                _terminal_safe(f"Updated {updated} to {project.name}."), err=True
            )
    typer.echo(
        _terminal_safe(
            f"Apps that set {old} will fail at their next start. Update "
            "SOLWYN_PROJECT or use the proj_ id."
        ),
        err=True,
    )
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
            "0 success; 1 generic/API; 2 usage or confirmation; "
            "3 auth or stored-key cleanup; 5 not found"
        ),
        schema="solwyn.projects-delete.v1 (CLI output after bodyless 204)",
        guidance=(
            "Irreversible: archives the project; keys stop reaching it in the same "
            "transaction (a key left with no project is revoked), and the project "
            "immediately becomes unavailable.",
            "Deletes this computer's stored key for the project.",
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
                hint="This action is irreversible; keys stop reaching the project.",
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

    removed_stored_key = False
    try:
        if validated_project_id in auth.stored_api_keys(runtime.api_url):
            auth.delete_stored_api_keys((validated_project_id,), runtime.api_url)
            removed_stored_key = True
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(
                code="credential_storage_failed",
                message=(
                    "The project was archived, but its stored API key could not be "
                    f"deleted: {error}"
                ),
                hint=error.hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    data = _ProjectDeleteData(
        project_id=validated_project_id, removed_stored_key=removed_stored_key
    )
    output_engine(context).write_success(
        schema="solwyn.projects-delete.v1",
        data=data,
        table=_delete_table(data),
    )
