"""Shared command authentication and error dispatch."""

from collections.abc import Callable

import typer

from solwyn_cli import auth
from solwyn_cli.auth import (
    AuthError,
    CredentialClass,
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


def _missing_credential_error(purpose: CredentialClass) -> OutputError:
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
) -> ResolvedCredential:
    """Resolve one required credential with the stable command auth contract."""
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
            _missing_credential_error(purpose),
            semantic=ErrorSemantic.AUTH,
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


_ENTRY_NAMES = {
    "login": "login",
    "api_key": "project key",
    "pending_device": "pending login",
}


def credential_saved_note(credential: ResolvedCredential) -> str:
    """Return the line saying where a credential was just stored."""
    written = auth.last_write()
    kind = credential.source if written is None else written.kind
    location = credential.location if written is None else written.location
    if location is None:
        return ""
    is_key = isinstance(credential, ProjectKeyCredential)
    parts: list[str] = []
    if written is not None and written.fell_back:
        parts.append(fallback_warning(written, "the project key" if is_key else "it"))
    if is_key and kind == "file":
        parts.append(
            f"Stored the project key in {location}. It has full project access and "
            "does not expire. For CI and agents, prefer SOLWYN_API_KEY from a secret "
            "manager."
        )
    elif written is not None and written.fell_back:
        pass  # The fallback warning already says where it went.
    elif kind == "keyring":
        parts.append(f"Saved to {location}.")
    else:
        parts.append(
            f"Saved to {location} (readable only by your user, and by any program "
            "you run)."
        )
    return " ".join(parts)


def fallback_warning(written: auth.Written, what: str) -> str:
    """The one warning for a failed keychain write that switched to the file."""
    text = (
        f"Could not use {auth.credential_store.keyring_name()} (it may be locked, "
        f"or unavailable over SSH). Saved {what} to {written.location}. From now on "
        f"the CLI uses this file (credential_store = file in {written.config_path}); "
        "run solwyn login --store auto to switch back."
    )
    if written.copied:
        names = ", ".join(_ENTRY_NAMES[entry] for entry in written.copied)
        text += f" Copied this API's {names} from the keychain into the file."
    return text
