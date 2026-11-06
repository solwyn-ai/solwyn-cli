"""API key commands: list, create, rotate, revoke, remove a project, hand off.

An API key belongs to the account and holds one or more projects. The CLI
creates a key for exactly one project; giving an existing key another project
is done only in the dashboard, by design.
"""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from solwyn_cli import auth
from solwyn_cli._vendor.validation import (
    API_KEY_PREFIX_LENGTH,
    API_KEY_PREFIX_PATTERN,
    validate_api_key_format,
)
from solwyn_cli._vendor.wire import (
    ApiKeyCreateRequest,
    ApiKeyRotateRequest,
    ApiKeyScope,
)
from solwyn_cli.auth import AuthError, LoginCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._confirmation import check_confirmation, confirm_effects
from solwyn_cli.commands._shared import (
    credential_saved_note,
    exit_for_resolution,
    exit_with_error,
    legacy_keys_note,
    required_login,
)
from solwyn_cli.config import DEFAULT_API_URL
from solwyn_cli.errors import DASHBOARD_KEYS_URL, ApiError, redact_secrets_except
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OneTimeSecretPanel, OutputError, TableData, _terminal_safe
from solwyn_cli.project_resolver import (
    Candidates,
    ProjectResolutionError,
    resolve as resolve_selection,
)
from solwyn_cli.runtime import command_context, output_engine, resolve_project

KEYS_PAGE = DASHBOARD_KEYS_URL

app = typer.Typer(
    help="List, create, rotate and revoke API keys, and hand a stored key to an SDK.",
    epilog=help_contract(
        endpoint="composite router for API key subcommands",
        auth="login, except keys env (local)",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
        guidance=(
            "A key is shown once, when it is created or rotated; listing never "
            "reveals key material.",
            "The CLI creates a key for one project.",
        ),
    ),
)


# --- Response models ----------------------------------------------------------


class _KeyProject(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(pattern=r"^proj_[a-f0-9]{24}$")
    name: str = Field(min_length=1)
    budget_mode: Literal["alert_only", "hard_deny"]
    dedicated_keys_only: bool


class _KeyCreator(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    user_id: str
    display_name: str | None


class _KeySummary(BaseModel):
    """One live key as ``GET /api/v1/keys`` lists it; never key material."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    name: str
    key_prefix: str = Field(pattern=API_KEY_PREFIX_PATTERN.pattern)
    scope: ApiKeyScope
    created_via: Literal["dashboard", "cli"]
    created_by: _KeyCreator | None
    approval_id: str | None
    created_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    last_used_at: AwareDatetime | None
    rotation_grace_until: AwareDatetime | None
    status: Literal["active", "grace", "expired"]
    projects: list[_KeyProject]


class _KeyList(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    keys: list[_KeySummary]
    used: int
    limit: int


class _NewKey(_KeySummary):
    """A response that carries a new key, shown once."""

    key: SecretStr

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        validate_api_key_format(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def validate_key_prefix(self) -> "_NewKey":
        if self.key_prefix != self.key.get_secret_value()[:API_KEY_PREFIX_LENGTH]:
            raise ValueError("Invalid API key prefix")
        return self


class _KeyCreateResponse(_NewKey):
    pass


class _KeyRotateResponse(_NewKey):
    replaced_key_id: str = Field(min_length=1)
    replaced_key_rotation_grace_until: AwareDatetime


# --- Output models ------------------------------------------------------------


class _ListedKey(_KeySummary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stored_here: list[str]


class _KeysListData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keys: list[_ListedKey]
    used: int
    limit: int


class _KeyCreateData(_KeySummary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stored: bool


class _KeyRotateData(_KeySummary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    replaced_key_id: str
    replaced_key_rotation_grace_until: AwareDatetime
    stored: bool
    stored_projects: list[str]


class _KeyRevokeData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    revoked: bool = True
    removed_stored_projects: list[str]


class _KeyProjectRemovedData(_KeySummary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    removed_project_id: str
    removed_stored_project: bool


class _KeysEnvProject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str
    id: str | None


class _KeysEnvData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    names: list[str]
    export: str
    api_url: str | None
    project: _KeysEnvProject


# --- Helpers ------------------------------------------------------------------


def _date_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat().replace("+00:00", "Z")


def _project_names(key: _KeySummary) -> str:
    return ", ".join(project.name for project in key.projects) or "-"


def _summary_rows(key: _KeySummary) -> tuple[tuple[str, str], ...]:
    return (
        ("ID", key.id),
        ("Name", key.name),
        ("Key prefix", key.key_prefix),
        ("Scope", key.scope.value),
        ("Status", key.status),
        ("Projects", _project_names(key)),
        ("Created via", key.created_via),
        ("Expires", _date_time(key.expires_at)),
        ("Rotation grace until", _date_time(key.rotation_grace_until)),
    )


def _field_table(rows: tuple[tuple[str, str], ...]) -> TableData:
    return TableData(columns=("Field", "Value"), rows=rows, right_align=("Value",))


def _summary_fields(key: _KeySummary) -> dict[str, object]:
    return {name: getattr(key, name) for name in _KeySummary.model_fields}


def _quote(value: str) -> str:
    """POSIX single quotes; keys, names and URLs never hold a quote."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def export_lines(api_key: str, project: str, api_url: str) -> tuple[str, list[str]]:
    """The shell lines that hand a key and its project to an SDK.

    A key belongs to one API. Off the default API the URL is exported too, so
    the key is never sent to production from a shell without SOLWYN_API_URL.
    """
    names = ["SOLWYN_API_KEY", "SOLWYN_PROJECT"]
    lines = [
        f"export SOLWYN_API_KEY={_quote(api_key)}",
        f"export SOLWYN_PROJECT={_quote(project)}",
    ]
    if api_url != DEFAULT_API_URL:
        names.append("SOLWYN_API_URL")
        lines.append(f"export SOLWYN_API_URL={_quote(api_url)}")
    return "\n".join(lines), names


def _login(context: typer.Context, operation: str) -> LoginCredential:
    return required_login(  # type: ignore[return-value]
        context,
        resolve=resolve_credential,
        message=f"A Solwyn login is required to {operation}.",
    )


def _local_project(context: typer.Context, project: str | None) -> None:
    """Make a subcommand's --project the selector (the flag source)."""
    if project is None or not project.strip(" \t"):
        return
    resolved = command_context(context)
    value = project.strip(" \t")
    if resolved.project_source == "cli" and resolved.project_selector not in (
        None,
        value,
    ):
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_options",
                message="--project was given twice with different values.",
                hint="Pass --project once.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    context.obj = resolved.model_copy(
        update={
            "project": value,
            "project_selector": value,
            "project_source": "cli",
            "project_resolved": False,
        }
    )


def _validated_key_id(context: typer.Context, key_id: str) -> str:
    value = key_id.strip()
    if not value or len(value) > 64:
        exit_with_error(
            context,
            OutputError(
                code="key_id_required",
                message="A key id is required.",
                hint="Run solwyn keys list to see key ids.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return value


def _invalid_response(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="invalid_keys_response", message="The API key response was invalid."
        ),
    )


def _api_failure(context: typer.Context, error: ApiError | AuthError) -> None:
    if isinstance(error, ApiError):
        exit_with_error(context, error)
    exit_with_error(
        context,
        OutputError(code="authentication_failed", message=str(error)),
        semantic=ErrorSemantic.AUTH,
    )


def _storage_failure(context: typer.Context, error: AuthError) -> None:
    exit_with_error(
        context,
        OutputError(
            code="credential_storage_failed",
            message=str(error),
            hint=error.hint or "Retry after fixing the credential store.",
        ),
        semantic=ErrorSemantic.AUTH,
    )


def _stored_keys(context: typer.Context) -> dict[str, auth.StoredApiKey]:
    try:
        return auth.stored_api_keys(command_context(context).api_url)
    except AuthError as error:
        _storage_failure(context, error)
    return {}


def _note(text: str) -> None:
    typer.echo(_terminal_safe(text), err=True)


# --- Commands -----------------------------------------------------------------


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/keys",
        auth="login",
        exits="0 success; 1 generic/API; 3 auth",
        schema="solwyn.keys-list.v1",
        guidance=(
            "Lists the account's live keys and their projects; never key material.",
            "stored_here names the projects this computer holds the key for.",
        ),
    ),
)
def list_keys(context: typer.Context) -> None:
    """List the account's live API keys, marking the ones stored here."""
    legacy_keys_note(context)
    credential = _login(context, "list API keys")
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request("GET", "/api/v1/keys")
        listed = _KeyList.model_validate(response.json())
    except (ApiError, AuthError) as error:
        _api_failure(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)
    stored = _stored_keys(context)
    data = _KeysListData(
        keys=[
            _ListedKey(
                **_summary_fields(key),
                stored_here=sorted(
                    project_id
                    for project_id, entry in stored.items()
                    if entry.key_id == key.id
                ),
            )
            for key in listed.keys
        ],
        used=listed.used,
        limit=listed.limit,
    )
    output_engine(context).write_success(
        schema="solwyn.keys-list.v1",
        data=data,
        table=TableData(
            columns=(
                "ID",
                "Name",
                "Prefix",
                "Scope",
                "Status",
                "Projects",
                "Expires",
                "Stored",
            ),
            rows=tuple(
                (
                    key.id,
                    key.name,
                    key.key_prefix,
                    key.scope.value,
                    key.status,
                    _project_names(key),
                    _date_time(key.expires_at),
                    "yes" if key.stored_here else "no",
                )
                for key in data.keys
            ),
            right_align=("Expires",),
            # Key ids are what rotate and revoke take: wrap, never truncate.
            fold_cells=True,
        ),
    )


@app.command(
    "create",
    epilog=help_contract(
        endpoint="POST /api/v1/keys",
        auth="login (browser approval)",
        exits="0 success; 1 generic/API; 2 usage; 3 auth or storage; 6 approval",
        schema="solwyn.keys-create.v1",
        guidance=(
            "Creates a key for exactly one project.",
            "--store-key stores it on this computer for that project and prints "
            "no secret; otherwise it is shown once with export lines.",
        ),
    ),
)
def create_key(
    context: typer.Context,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name or proj_ id for the key."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Key name; defaults to the project's name."),
    ] = None,
    scope: Annotated[
        ApiKeyScope | None,
        typer.Option("--scope", help="Key scope; omit for full."),
    ] = None,
    store_key: Annotated[
        bool,
        typer.Option("--store-key", help="Store the new key on this computer."),
    ] = False,
) -> None:
    """Create an API key for one project and capture it exactly once."""
    _local_project(context, project)
    if name is not None and not (1 <= len(name.strip()) <= 100):
        exit_with_error(
            context,
            OutputError(
                code="invalid_key_create_request",
                message="The key name must be 1 to 100 characters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    legacy_keys_note(context)
    credential = _login(context, "create API keys")
    try:
        selection = resolve_project(context, purpose="setup", credential=credential)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    project_id = selection.project_id or ""
    try:
        request = ApiKeyCreateRequest(
            project_id=project_id,
            **({} if name is None else {"name": name.strip()}),
            **({} if scope is None else {"scope": scope}),
        )
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_key_create_request",
                message="The key creation request is invalid.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                "/api/v1/keys",
                json=request.model_dump(mode="json", exclude_unset=True),
                retry_rate_limit=False,
            )
        if response.status_code != 201:
            raise ValueError("Key creation did not return 201")
        created = _KeyCreateResponse.model_validate(response.json())
        if [item.id for item in created.projects] != [project_id]:
            raise ValueError("The new key does not hold exactly the project asked for")
    except (ApiError, AuthError) as error:
        _api_failure(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    project_name = created.projects[0].name
    if store_key:
        try:
            stored = auth.store_api_key(
                project_id=project_id,
                key=created.key.get_secret_value(),
                key_id=created.id,
                account_id=selection.account_id or "",
                project_name=project_name,
                api_url=runtime.api_url,
            )
        except AuthError as error:
            _storage_failure(context, error)
        if note := credential_saved_note(stored):
            _note(note)
    data = _KeyCreateData(**_summary_fields(created), stored=store_key)
    table = _field_table(
        (*_summary_rows(created), ("Stored", "yes" if store_key else "no"))
    )
    if store_key:
        output_engine(context).write_success(
            schema="solwyn.keys-create.v1", data=data, table=table
        )
        return
    lines, _ = export_lines(
        created.key.get_secret_value(),
        selection.selector or project_name,
        runtime.api_url,
    )
    output_engine(context).write_success(
        schema="solwyn.keys-create.v1",
        data=data,
        table=table,
        one_time_secret=OneTimeSecretPanel(
            title="One-time API key", value=created.key, footer=lines
        ),
        reveal={"key": created.key},
    )


@app.command(
    "rotate",
    epilog=help_contract(
        endpoint="POST /api/v1/keys/{key_id}/rotate",
        auth="login (browser approval)",
        exits="0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        schema="solwyn.keys-rotate.v1",
        guidance=(
            "Effects are shown before interactive confirmation or --yes; non-TTY requires --yes.",
            "The replacement keeps the key's projects. When this computer stores "
            "the key, the stored copy is replaced and no secret is printed; "
            "otherwise the replacement is shown once.",
        ),
    ),
)
def rotate_key(
    context: typer.Context,
    key_id: Annotated[
        str, typer.Argument(help="Id of the key to rotate (solwyn keys list).")
    ],
    grace_seconds: Annotated[
        int | None,
        typer.Option(
            "--grace-seconds",
            help="Seconds the old key keeps working (0 through 604800).",
        ),
    ] = None,
    scope: Annotated[
        ApiKeyScope | None,
        typer.Option(
            "--scope",
            help="Scope for the replacement; omit to keep the current scope.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Acknowledge the displayed effects without prompting."
        ),
    ] = False,
) -> None:
    """Rotate a key; the replacement holds the same projects."""
    validated = _validated_key_id(context, key_id)
    try:
        request = ApiKeyRotateRequest(grace_seconds=grace_seconds, scope=scope)
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_key_rotation_request",
                message="The key rotation request is invalid.",
                hint="Use --grace-seconds with a value from 0 through 604800.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    grace = (
        "the API's default grace period"
        if grace_seconds is None
        else f"{grace_seconds} seconds"
    )
    effect = f"Rotate key {validated!r}: the replacement holds the same projects; " + (
        "the old key stops working immediately."
        if grace_seconds == 0
        else f"the old key stops working after {grace}."
    )
    check_confirmation(context, yes=yes, effect=effect)
    legacy_keys_note(context)
    credential = _login(context, "rotate API keys")
    # Read what this computer holds before Core rotates: afterwards nothing
    # may stop the replacement from being stored or shown.
    holdings = {
        project_id: entry
        for project_id, entry in _stored_keys(context).items()
        if entry.key_id == validated
    }
    confirm_effects(context, yes=yes, effect=effect)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                f"/api/v1/keys/{quote(validated, safe='')}/rotate",
                json=request.model_dump(mode="json"),
                retry_rate_limit=False,
            )
        if response.status_code != 200:
            raise ValueError("Key rotation did not return 200")
        rotated = _KeyRotateResponse.model_validate(response.json())
        if rotated.replaced_key_id != validated:
            raise ValueError("The rotation replaced another key")
    except (ApiError, AuthError) as error:
        _api_failure(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    names = {project.id: project.name for project in rotated.projects}
    replaced: list[str] = []
    for project_id, entry in holdings.items():
        try:
            auth.store_api_key(
                project_id=project_id,
                key=rotated.key.get_secret_value(),
                key_id=rotated.id,
                account_id=entry.account_id,
                project_name=names.get(project_id, entry.project_name),
                api_url=runtime.api_url,
            )
        except Exception as error:  # noqa: BLE001 - the replacement must not be lost.
            # Core already rotated: show the replacement once instead (below).
            reason = str(error) if isinstance(error, AuthError) else "the store failed"
            _note(
                f"Could not replace the stored key for {entry.project_name}: "
                f"{reason} The replacement is shown once below; store it again "
                "with solwyn login --with-key."
            )
            continue
        replaced.append(project_id)
    data = _KeyRotateData(
        **_summary_fields(rotated),
        replaced_key_id=rotated.replaced_key_id,
        replaced_key_rotation_grace_until=rotated.replaced_key_rotation_grace_until,
        stored=bool(replaced),
        stored_projects=sorted(replaced),
    )
    table = _field_table(
        (
            *_summary_rows(rotated),
            ("Replaced key", rotated.replaced_key_id),
            (
                "Old key works until",
                _date_time(rotated.replaced_key_rotation_grace_until),
            ),
            ("Stored", ", ".join(sorted(replaced)) or "no"),
        )
    )
    if replaced and len(replaced) == len(holdings):
        _note(
            "Replaced the stored key on this computer; run solwyn keys env to "
            "hand it to an SDK."
        )
        output_engine(context).write_success(
            schema="solwyn.keys-rotate.v1", data=data, table=table
        )
        return
    output_engine(context).write_success(
        schema="solwyn.keys-rotate.v1",
        data=data,
        table=table,
        one_time_secret=OneTimeSecretPanel(
            title="One-time replacement API key", value=rotated.key
        ),
        reveal={"key": rotated.key},
    )


@app.command(
    "revoke",
    epilog=help_contract(
        endpoint="DELETE /api/v1/keys/{key_id}",
        auth="login (browser approval)",
        exits="0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        schema="solwyn.keys-revoke.v1 (CLI output after bodyless 204)",
        guidance=(
            "Accepts only a key id; never pass key material.",
            "Effects are shown before interactive confirmation or --yes; non-TTY requires --yes.",
            "Deletes this computer's stored copies of the key.",
        ),
    ),
)
def revoke_key(
    context: typer.Context,
    key_id: Annotated[
        str, typer.Argument(help="Id of the key to revoke (solwyn keys list).")
    ],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Acknowledge the displayed effects without prompting."
        ),
    ] = False,
) -> None:
    """Revoke a key now, without a replacement."""
    validated = _validated_key_id(context, key_id)
    effect = (
        f"Revoke key {validated!r}: it stops working for every project it holds, "
        "with no replacement."
    )
    check_confirmation(context, yes=yes, effect=effect)
    legacy_keys_note(context)
    credential = _login(context, "revoke API keys")
    removed = tuple(
        sorted(
            project_id
            for project_id, entry in _stored_keys(context).items()
            if entry.key_id == validated
        )
    )
    confirm_effects(context, yes=yes, effect=effect)
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "DELETE",
                f"/api/v1/keys/{quote(validated, safe='')}",
                retry_rate_limit=False,
            )
        if response.status_code != 204 or response.content:
            raise ValueError("Key revocation did not return a bodyless 204")
    except (ApiError, AuthError) as error:
        _api_failure(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    try:
        auth.delete_stored_api_keys(removed, runtime.api_url)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(
                code="credential_storage_failed",
                message=(
                    f"Key {validated} was revoked, but its stored copies could "
                    f"not be deleted: {error}"
                ),
                hint=error.hint or "Run solwyn logout to clear this API's stored keys.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    data = _KeyRevokeData(key_id=validated, removed_stored_projects=list(removed))
    output_engine(context).write_success(
        schema="solwyn.keys-revoke.v1",
        data=data,
        table=_field_table(
            (
                ("Key ID", validated),
                ("Revoked", "true"),
                ("Removed from this computer", ", ".join(removed) or "-"),
            )
        ),
    )


@app.command(
    "remove-project",
    epilog=help_contract(
        endpoint="DELETE /api/v1/keys/{key_id}/projects/{project_id}",
        auth="login (browser approval)",
        exits="0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        schema="solwyn.keys-remove-project.v1",
        guidance=(
            "Effects are shown before interactive confirmation or --yes; non-TTY requires --yes.",
            "A key keeps at least one project; revoke it instead of removing the last.",
        ),
    ),
)
def remove_project(
    context: typer.Context,
    key_id: Annotated[str, typer.Argument(help="Id of the key (solwyn keys list).")],
    project: Annotated[
        str,
        typer.Option("--project", "-p", help="Project name or proj_ id to remove."),
    ],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Acknowledge the displayed effects without prompting."
        ),
    ] = False,
) -> None:
    """Take a project away from a key; the key keeps its other projects."""
    validated = _validated_key_id(context, key_id)
    target = project.strip(" \t")
    effect = (
        f"Remove project {target!r} from key {validated!r}: the key stops working "
        "for that project."
    )
    check_confirmation(context, yes=yes, effect=effect)
    legacy_keys_note(context)
    credential = _login(context, "change API keys")
    runtime = command_context(context)
    try:
        selection = resolve_selection(
            Candidates(selected=target or None, source="cli", repo=None),
            purpose="read",
            api_url=runtime.api_url,
            credential=credential,
        )
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    project_id = selection.project_id or ""
    confirm_effects(context, yes=yes, effect=effect)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "DELETE",
                f"/api/v1/keys/{quote(validated, safe='')}/projects/{project_id}",
                retry_rate_limit=False,
            )
        if response.status_code != 200:
            raise ValueError("Removing a project did not return 200")
        summary = _KeySummary.model_validate(response.json())
        if summary.id != validated or any(p.id == project_id for p in summary.projects):
            raise ValueError("The key still holds the project")
    except (ApiError, AuthError) as error:
        _api_failure(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    entry = _stored_keys(context).get(project_id)
    removed_stored = entry is not None and entry.key_id == validated
    if removed_stored:
        try:
            auth.delete_stored_api_keys((project_id,), runtime.api_url)
        except AuthError as error:
            _storage_failure(context, error)
    data = _KeyProjectRemovedData(
        **_summary_fields(summary),
        removed_project_id=project_id,
        removed_stored_project=removed_stored,
    )
    output_engine(context).write_success(
        schema="solwyn.keys-remove-project.v1",
        data=data,
        table=_field_table(
            (
                *_summary_rows(summary),
                ("Removed project", project_id),
                ("Removed from this computer", "yes" if removed_stored else "no"),
            )
        ),
    )


@app.command(
    "add-project",
    hidden=True,
    epilog=help_contract(
        endpoint="none: only the dashboard adds a project to a key",
        auth="none; no credential lookup",
        exits="2 always",
        schema="solwyn.error.v1",
    ),
)
def add_project(
    context: typer.Context,
    key_id: Annotated[str | None, typer.Argument()] = None,
    project: Annotated[str | None, typer.Option("--project", "-p")] = None,
) -> None:
    """Point at the dashboard: only a person there can widen a key."""
    del key_id, project
    exit_with_error(
        context,
        OutputError(
            code="dashboard_only",
            message=f"Adding a project to a key is done in the dashboard: {KEYS_PAGE}",
        ),
        semantic=ErrorSemantic.USAGE,
    )


# How to hand a stored key to one command: a subshell, so neither the key nor
# the exports outlive the command, and nothing runs when keys env fails.
KEYS_ENV_SUBSHELL = '(solwyn_env="$(solwyn --format table keys env)" && eval "$solwyn_env" && exec <command>)'


@app.command(
    "env",
    epilog=help_contract(
        endpoint=(
            "local credential store read; with a login, GET /api/v1/projects "
            "(and GET /api/v1/accounts/me for SOLWYN_TOKEN)"
        ),
        auth="local (the stored key; SOLWYN_API_KEY is never read)",
        exits="0 success; 2 usage or project mismatch; 3 no stored key or account unresolved",
        schema="solwyn.keys-env.v2",
        guidance=(
            "Prints export lines to stdout. Hand them to one command in a "
            f"subshell: {KEYS_ENV_SUBSHELL}.",
            "Prints JSON only with --json or --format json on the command line; "
            "SOLWYN_FORMAT and config format never apply. Never eval JSON output.",
            "Exports SOLWYN_API_KEY and SOLWYN_PROJECT, and SOLWYN_API_URL when "
            "the API URL is not the default.",
            "Refuses a project other than the one the repo's .solwyn.env names.",
            "Never writes .env or any other file.",
        ),
    ),
)
def env_key(
    context: typer.Context,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project name or proj_ id."),
    ] = None,
) -> None:
    """Print export lines for the stored key of the selected project."""
    _local_project(context, project)
    legacy_keys_note(context)
    try:
        selection = resolve_project(context, purpose="keys_env")
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    credential = selection.credential
    if not isinstance(credential, auth.ApiKeyCredential) or selection.selector is None:
        exit_for_resolution(
            context,
            ProjectResolutionError(
                "api_key_not_stored",
                "No API key is stored for this project.",
                semantic=ErrorSemantic.AUTH,
            ),
        )
        return
    api_url = command_context(context).api_url
    line, names = export_lines(credential.bearer_value(), selection.selector, api_url)
    # JSON only when asked for on this command line. SOLWYN_FORMAT or config
    # format=json must never turn the export lines into one JSON word: a
    # shell that evals it and fails (zsh: "no matches found") echoes the
    # whole word, key included.
    runtime = command_context(context)
    if runtime.output_format.value == "json" and runtime.output_format_explicit:
        output_engine(context).write_success(
            schema="solwyn.keys-env.v2",
            data=_KeysEnvData(
                names=names,
                export="",
                api_url=None if api_url == DEFAULT_API_URL else api_url,
                project=_KeysEnvProject(
                    selector=selection.selector, id=selection.project_id
                ),
            ),
            reveal={"export": SecretStr(line)},
            exempt=credential.api_key,
        )
        return
    # The stored key is the one value shown; the selector is redacted like
    # any other text if it ever holds a secret.
    line = redact_secrets_except(line, credential.bearer_value())
    typer.echo(line)
