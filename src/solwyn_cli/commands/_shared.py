"""Shared command authentication and error dispatch."""

from collections.abc import Callable

import typer

from solwyn_cli import auth
from solwyn_cli.auth import (
    AuthError,
    CredentialClass,
    LoginCredential,
    ProjectKeyCredential,
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
from solwyn_cli.runtime import output_engine


def metering_credential() -> ResolvedCredential | None:
    """Resolve a project key for metering; the login is never used."""
    return resolve_credential(purpose="metering")


def _login_required_error(message: str) -> OutputError:
    hint = "Run solwyn login, then retry."
    try:
        key_selected = auth_selection() == "project_key"
    except ValueError:
        key_selected = False
    if key_selected:
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
            hint="Run solwyn login.",
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
    if isinstance(credential, ProjectKeyCredential):
        exit_with_error(
            context, _login_required_error(message), semantic=ErrorSemantic.AUTH
        )
    return credential


def exit_with_error(
    context: typer.Context,
    error: ApiError | OutputError,
    *,
    semantic: ErrorSemantic | None = None,
) -> None:
    """Render one safe error and stop with its stable process exit code."""
    output_engine(context).write_error(error)
    raise typer.Exit(code=int(exit_code_for_error(error, semantic=semantic)))


def credential_saved_note(credential: ResolvedCredential) -> str:
    """Return the line saying where a credential was just stored."""
    written = auth.last_write()
    kind = credential.source if written is None else written.kind
    location = credential.location if written is None else written.location
    if location is None:
        return ""
    if isinstance(credential, ProjectKeyCredential) and kind == "file":
        return (
            f"Stored the project key in {location}. It has full project access and "
            "does not expire. For CI and agents, prefer SOLWYN_API_KEY from a secret "
            "manager."
        )
    if kind == "keyring":
        return f"Saved to {location}."
    return (
        f"Saved to {location} (readable only by your user, and by any program you run)."
    )


def unused_store_note() -> str | None:
    """Point at credentials for this API left in the store not in use."""
    found = auth.unused_store_holdings()
    if found is None:
        return None
    return (
        f"This API also has stored credentials in {found.location}, which is not "
        "the credential store in use. Run solwyn logout to clear both stores."
    )
