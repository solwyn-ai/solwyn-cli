"""Shared command authentication and error dispatch."""

from collections.abc import Callable

import typer

from solwyn_cli.auth import AuthError, ResolvedCredential, resolve_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic, exit_code_for_error
from solwyn_cli.output import OutputError
from solwyn_cli.runtime import output_engine


def required_credential(
    context: typer.Context,
    *,
    resolve: Callable[[], ResolvedCredential | None] = resolve_credential,
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
            OutputError(
                code="authentication_required",
                message="Authentication is required.",
                hint="Set SOLWYN_API_KEY or run solwyn login.",
            ),
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
