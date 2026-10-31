"""Shared command authentication and error dispatch."""

from collections.abc import Callable

import typer

from solwyn_cli.auth import (
    METERING_KEY_HINT,
    AuthError,
    CredentialClass,
    ResolvedCredential,
    auth_selection,
    expired_login_date,
    resolve_credential,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, exit_code_for_error
from solwyn_cli.output import OutputError
from solwyn_cli.runtime import output_engine


def metering_credential() -> ResolvedCredential | None:
    """Resolve a project key for metering; the login is never used."""
    return resolve_credential(purpose="metering")


def _missing_credential_error(purpose: CredentialClass) -> OutputError:
    if purpose == "metering":
        return OutputError(
            code="authentication_required",
            message="Authentication is required.",
            hint=METERING_KEY_HINT,
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
        hint="Run solwyn login, or set SOLWYN_API_KEY.",
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
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is None:
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
