"""Shared command authentication and error dispatch."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NoReturn

import typer

from solwyn_cli import auth
from solwyn_cli.auth import (
    ApiKeyCredential,
    AuthError,
    CredentialClass,
    LoginCredential,
    ResolvedCredential,
    auth_selection,
    expired_login_date,
    legacy_login_notice,
    missing_credential_hint,
    resolve_credential,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, exit_code_for_error
from solwyn_cli.output import OutputError, _terminal_safe
from solwyn_cli.project_resolver import ProjectResolutionError, project_required
from solwyn_cli.runtime import (
    CommandContext,
    command_context,
    output_engine,
    resolve_project,
)


def metering_credential() -> ResolvedCredential | None:
    """Resolve an API key for metering; the login is never used."""
    return resolve_credential(purpose="metering")


def exit_for_resolution(
    context: typer.Context, error: ProjectResolutionError | ApiError | AuthError
) -> None:
    """Render a resolver failure with its stable exit code."""
    if isinstance(error, ProjectResolutionError):
        exit_with_error(
            context,
            OutputError(code=error.code, message=error.message, hint=error.hint),
            semantic=error.semantic,
        )
    if isinstance(error, ApiError):
        exit_with_error(context, error)
    exit_with_error(
        context,
        OutputError(
            code=(
                "credential_storage_failed"
                if isinstance(error, auth.CredentialStorageError)
                else "authentication_failed"
            ),
            message=str(error),
            hint=error.hint,
        ),
        semantic=ErrorSemantic.AUTH,
    )


def _bind_key(
    context: typer.Context,
    credential: ResolvedCredential | None,
    purpose: CredentialClass,
) -> ResolvedCredential | None:
    """Choose and bind the API key for this command's project.

    A login is returned as is (``command_project`` resolves its project when
    the command needs one). A key always needs its project: an env key is
    bound through the resolver, and with no login and no env key the stored
    key for the selected project is chosen there.
    """
    if isinstance(credential, LoginCredential):
        return credential
    if credential is None and purpose != "metering":
        try:
            if auth_selection() == "login":
                return None
        except ValueError:
            return None
    selection = resolve_project(
        context,
        purpose="spend" if purpose == "metering" else "read",
        credential=credential,
    )
    return selection.credential


def exit_project_required(
    context: typer.Context,
    *,
    message: str = "A project is required.",
    hint: str | None = None,
) -> NoReturn:
    """Exit 2: nothing selects a project (or SOLWYN_PROJECT_ID is left over)."""
    exit_for_resolution(context, project_required(message, hint))
    raise AssertionError("unreachable")


def refuse_retired_project_id(context: typer.Context) -> None:
    """Exit 2 before anything is created or stored when nothing selects a
    project but the retired SOLWYN_PROJECT_ID is set: the user meant a project,
    so a command must not pick or create one for them."""
    if (
        command_context(context).project_selector is None
        and os.environ.get("SOLWYN_PROJECT_ID", "").strip()
    ):
        exit_project_required(context)


def selected_project(context: typer.Context) -> str:
    """The project selector this command needs; exits 2 when there is none."""
    project = command_context(context).project
    if project is None:
        exit_project_required(context)
    return project  # type: ignore[return-value]


def command_project(context: typer.Context, credential: ResolvedCredential) -> str:
    """The proj_ id this command's requests use; exits when there is none.

    An API key was bound to its project when it was resolved. A login turns
    the selector into an id through ``GET /api/v1/projects`` (a ``proj_``
    value needs no read).
    """
    if isinstance(credential, ApiKeyCredential):
        if credential.project is None:
            exit_for_resolution(context, project_required())
        return credential.project  # type: ignore[return-value]
    resolved = command_context(context)
    if resolved.project_resolved and resolved.project is not None:
        return resolved.project
    if resolved.project_selector is None:
        exit_for_resolution(context, project_required())
    try:
        selection = resolve_project(context, purpose="read", credential=credential)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    return selection.project_id  # type: ignore[return-value]


def _project_key_selected() -> bool:
    try:
        return auth_selection() == "project_key"
    except ValueError:
        return False


def _login_required_error(message: str) -> OutputError:
    hint = "Run solwyn login, then retry."
    if _project_key_selected():
        hint = "Unset SOLWYN_AUTH=project_key, run solwyn login if needed, then retry."
    return OutputError(code="authentication_required", message=message, hint=hint)


def _missing_credential_error(
    purpose: CredentialClass, *, login_message: str | None = None
) -> OutputError:
    hint = missing_credential_hint(purpose)
    if purpose == "metering":
        return OutputError(
            code="authentication_required",
            message="Authentication is required.",
            hint=hint,
        )
    expired_on = expired_login_date()
    if expired_on is not None:
        return OutputError(
            code="authentication_required",
            message=f"Your Solwyn login expired on {expired_on.date().isoformat()}.",
            hint=(
                "Unset SOLWYN_AUTH=project_key, run solwyn login, then retry."
                if login_message is not None and _project_key_selected()
                else "Run solwyn login."
            ),
        )
    if login_message is not None:
        return _login_required_error(login_message)
    try:
        login_only = auth_selection() == "login"
    except ValueError:
        login_only = False
    if login_only:
        return OutputError(
            code="authentication_required",
            message="SOLWYN_AUTH=login requires a Solwyn login.",
            hint="Run solwyn login.",
        )
    return OutputError(
        code="authentication_required",
        message="Authentication is required.",
        hint=hint,
    )


def with_project(
    context: typer.Context, credential: ResolvedCredential
) -> CommandContext:
    """The command context with ``project`` set to the resolved proj_ id."""
    command_project(context, credential)
    return command_context(context)


def required_credential(
    context: typer.Context,
    *,
    resolve: Callable[[], ResolvedCredential | None] = resolve_credential,
    purpose: CredentialClass = "user",
    login_message: str | None = None,
) -> ResolvedCredential:
    """Resolve one required credential with the stable command auth contract.

    ``login_message`` marks a command that only a login may run: with no
    credential the error names the login instead of every credential source.
    """
    try:
        credential = resolve()
        if login_message is None:
            credential = _bind_key(context, credential, purpose)
    except (ProjectResolutionError, ApiError) as error:
        exit_for_resolution(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(
                code=(
                    "credential_storage_failed"
                    if isinstance(error, auth.CredentialStorageError)
                    else "authentication_failed"
                ),
                message=str(error),
                hint=error.hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is None:
        notice = legacy_login_notice()
        if notice is not None:
            typer.echo(_terminal_safe(notice), err=True)
        exit_with_error(
            context,
            _missing_credential_error(purpose, login_message=login_message),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def required_login(
    context: typer.Context,
    *,
    message: str,
    resolve: Callable[[], ResolvedCredential | None] = resolve_credential,
) -> LoginCredential:
    """Resolve the login for a command that a project API key may never run.

    A stored or environment project key is refused before any request is sent,
    exactly like a missing credential: exit 3 with a hint to run solwyn login.
    """
    credential = required_credential(context, resolve=resolve, login_message=message)
    if isinstance(credential, ApiKeyCredential):
        exit_with_error(
            context, _login_required_error(message), semantic=ErrorSemantic.AUTH
        )
    return credential


_BEFORE_ERROR: ContextVar[tuple[Callable[[], None], ...]] = ContextVar(
    "solwyn_before_error", default=()
)


@contextmanager
def before_error(callback: Callable[[], None]) -> Iterator[None]:
    """Run ``callback`` (innermost first) before exit_with_error writes an error.

    Notes about what a failed run left behind (a created project, a revoked
    key) then come before the error, which stays the last stderr line.
    """
    token = _BEFORE_ERROR.set((callback, *_BEFORE_ERROR.get()))
    try:
        yield
    finally:
        _BEFORE_ERROR.reset(token)


def exit_with_error(
    context: typer.Context,
    error: ApiError | OutputError,
    *,
    semantic: ErrorSemantic | None = None,
) -> None:
    """Render one safe error and stop with its stable process exit code."""
    callbacks = _BEFORE_ERROR.get()
    _BEFORE_ERROR.set(())
    for callback in callbacks:
        callback()
    output_engine(context).write_error(error)
    raise typer.Exit(code=int(exit_code_for_error(error, semantic=semantic)))


def credential_saved_note(credential: ResolvedCredential) -> str:
    """Return the line saying where a credential was just stored."""
    written = auth.last_write()
    kind = credential.source if written is None else written.kind
    location = credential.location if written is None else written.location
    if location is None:
        return ""
    if isinstance(credential, ApiKeyCredential) and kind == "file":
        project = credential.project_name or credential.project
        return (
            f"Stored the API key for {project} in {location}. For CI and agents, "
            "prefer SOLWYN_API_KEY from a secret manager."
        )
    if kind == "keyring":
        return f"Saved to {location}."
    return (
        f"Saved to {location} (readable only by your user, and by any program you run)."
    )


def legacy_keys_note(context: typer.Context, *, strict: bool = True) -> None:
    """Drop 0.4.x project keys from the store in use; say so once on stderr.

    ``strict=False`` (login, logout) leaves a store that cannot be read to the
    command itself, which reports it in its own terms.
    """
    try:
        removed = auth.migrate_legacy_keys(command_context(context).api_url)
    except AuthError as error:
        if not strict:
            return
        exit_for_resolution(context, error)
    if removed:
        typer.echo(_terminal_safe(auth.LEGACY_KEYS_REMOVED_NOTE), err=True)


def unused_store_note() -> str | None:
    """Point at credentials for this API left in the store not in use."""
    found = auth.unused_store_holdings()
    if found is None:
        return None
    return (
        f"This API also has stored credentials in {found.location}, which is not "
        "the credential store in use. Run solwyn logout to clear both stores."
    )
