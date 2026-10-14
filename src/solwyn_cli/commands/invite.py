"""Private stdin/hidden-prompt invitation preview and acceptance."""

from contextlib import contextmanager, redirect_stdout
import logging
import re
import sys
from typing import Annotated
from urllib.parse import quote

import typer

try:
    from typer._click.exceptions import UsageError
except ImportError:  # Typer releases before the bundled Click boundary.
    from click.exceptions import UsageError

from typer.core import TyperCommand, TyperGroup
from pydantic import SecretStr, ValidationError

from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    JwtCredential,
    resolve_credential,
    store_jwt_pair,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import _team as shared
from solwyn_cli.commands._shared import required_credential
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.errors import ApiError
from solwyn_cli.runtime import command_context


class _PrivateInvocation:
    """Reject invalid secret-entry argv without echoing rejected values."""

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except UsageError:
            raise UsageError(
                "Invalid invitation invocation. Credentials belong only in stdin or hidden prompts.",
                ctx=ctx,
            ) from None


class _PrivateCommand(_PrivateInvocation, TyperCommand):
    pass


class _PrivateGroup(_PrivateInvocation, TyperGroup):
    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except UsageError:
            raise UsageError(
                "Unknown invitation command. Use invite show or invite accept.", ctx=ctx
            ) from None


class _PrivateHttpDiagnostics(logging.Filter):
    def filter(self, record):
        # HTTPX INFO includes the full token URL; HTTPCore DEBUG can include
        # headers/exception objects. Keep only a fixed diagnostic for these
        # libraries during private invitation I/O, before handlers capture it.
        record.msg = "Invitation HTTP diagnostic."
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if hasattr(record, "message"):
            record.message = record.msg
        return True


@contextmanager
def _private_http():
    names = (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    )
    loggers = [logging.getLogger(name) for name in names]
    redaction = _PrivateHttpDiagnostics()
    for logger in loggers:
        logger.addFilter(redaction)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(redaction)


TOKEN_MAX = 4096
app = typer.Typer(
    cls=_PrivateGroup,
    help="Preview or accept an invitation privately.",
    epilog=shared.command_help(
        "/api/v1/invitations/{token}",
        "varies by command",
        "public preview; optional JWT acceptance",
    ),
)


def _stdin_is_tty():
    return sys.stdin.isatty()


def _prompt(label, *, hidden=False):
    # Click's hidden-input newline/abort handling may write to stdout too.
    with redirect_stdout(sys.stderr):
        return typer.prompt(label, hide_input=hidden, err=True)


def _read_token(context):
    try:
        value = (
            _prompt("Invitation token", hidden=True)
            if _stdin_is_tty()
            else sys.stdin.read(TOKEN_MAX + 3)
        )
    except (EOFError, typer.Abort, KeyboardInterrupt):
        shared.fail(
            context,
            "invitation_input_cancelled",
            "Invitation input was cancelled.",
            semantic=ErrorSemantic.USAGE,
        )
    # Permit exactly one conventional final newline for shell pipelines. No
    # whitespace splitting, URL, file, env fallback, or unbounded stdin read.
    value = value.removesuffix("\n").removesuffix("\r")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", value):
        shared.fail(
            context,
            "invalid_invitation_token",
            "Provide one valid invitation token through stdin or the hidden prompt.",
            semantic=ErrorSemantic.USAGE,
        )
    return SecretStr(value)


def _invitation_path(token: SecretStr) -> str:
    # Validation permits only unreserved URL-safe characters; encode again at
    # the HTTP boundary so the token always occupies exactly one path segment.
    return "/api/v1/invitations/" + quote(token.get_secret_value(), safe="")


def _preview(context, token):
    with (
        _private_http(),
        SolwynClient(
            api_url=command_context(context).api_url, credential=None
        ) as client,
    ):
        response = client.request_unauthenticated("GET", _invitation_path(token))
    return shared.InvitationPreviewResponse.model_validate(response.json())


@app.command(
    "show",
    cls=_PrivateCommand,
    epilog=shared.command_help(
        "GET /api/v1/invitations/{token}",
        "solwyn.invite.show.v1",
        "none; public preview never reads stored credentials",
    ),
)
def show(context: typer.Context):
    """Preview an invitation; token comes only from stdin or a hidden prompt."""
    token = _read_token(context)
    try:
        data = _preview(context, token)
    except ApiError as error:
        shared.safe_api_error(context, error)
    except (ValueError, TypeError):
        shared.fail(
            context,
            "invalid_invitation_response",
            "The invitation preview response was invalid.",
        )
    shared.emit(context, "solwyn.invite.show.v1", data)


@app.command(
    "accept",
    cls=_PrivateCommand,
    epilog=shared.command_help(
        "GET /api/v1/invitations/{token}; POST /api/v1/invitations/{token}/accept",
        "solwyn.invite.accept.v1",
        "selected JWT; --new-user uses explicit interactive invitation-only credentials",
    ),
)
def accept(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Acknowledge joining the displayed account without confirming.",
        ),
    ] = False,
    new_user: Annotated[
        bool,
        typer.Option(
            "--new-user",
            help="Explicit invitation-only user creation with interactive email, display name, and hidden password; requires no selected credentials.",
        ),
    ] = False,
):
    """Join the invited account and securely store its returned JWT pair."""
    shared.preflight(context, yes)
    if new_user and not _stdin_is_tty():
        shared.fail(
            context,
            "interactive_invitation_auth_required",
            "New-user invitation acceptance requires an interactive terminal.",
            "Use an interactive terminal for the hidden token/password prompts; existing users should log in and use invite accept.",
            ErrorSemantic.AUTH,
        )
    try:
        if new_user:
            credential = resolve_credential()
            if credential is not None:
                shared.fail(
                    context,
                    "invitation_identity_ambiguous",
                    "New-user mode requires no selected credentials.",
                    "Unset SOLWYN_API_KEY and run solwyn logout before explicitly creating an invited user. Existing users should use signed-in invite accept.",
                    ErrorSemantic.AUTH,
                )
        else:
            credential = required_credential(context, resolve=resolve_credential)
            if not isinstance(credential, JwtCredential):
                shared.fail(
                    context,
                    "authentication_required",
                    "Signed-in invitation acceptance requires JWT.",
                    shared.JWT_HINT,
                    ErrorSemantic.AUTH,
                )
    except AuthError:
        shared.fail(
            context,
            "authentication_failed",
            "The selected credentials could not be resolved.",
            shared.JWT_HINT,
            ErrorSemantic.AUTH,
        )
    token = _read_token(context)
    try:
        preview = _preview(context, token)
    except ApiError as error:
        shared.safe_api_error(context, error)
    except (ValueError, TypeError):
        shared.fail(
            context,
            "invalid_invitation_response",
            "The invitation preview response was invalid.",
        )
    shared.effects(
        f"Join account {preview.account_name} as {preview.role}; invited by {preview.inviter_email or 'unavailable'}, expires {preview.expires_at.isoformat()}. Acceptance requires the invited identity to have no active membership. It consumes the invitation, creates membership, verifies the invited email if needed, may send an acceptance email, and replaces stored JWT credentials with the returned pair."
        + (
            " Explicit new-user mode creates the invited user; token possession proves the invited email."
            if new_user
            else " The selected signed-in JWT is used; no user is created."
        )
    )
    shared.confirm(context, yes)
    body = {}
    if new_user:
        try:
            email = _prompt("Invited email")
            display_name = _prompt("Display name")
            password = _prompt("Password", hidden=True)
            request = shared.AcceptInvitationRequest(
                email=email, display_name=display_name, password=password
            )
            body = request.model_dump(exclude_unset=True, exclude={"password"})
            body["password"] = request.password.get_secret_value()
        except (EOFError, typer.Abort, KeyboardInterrupt):
            shared.fail(
                context,
                "invitation_input_cancelled",
                "Invitation input was cancelled; acceptance was not submitted.",
                semantic=ErrorSemantic.USAGE,
            )
        except (ValidationError, ValueError, TypeError):
            shared.fail(
                context,
                "invalid_invitation_credentials",
                "Provide an invited email, display name of 1–120 characters, and password of 8–128 characters.",
                semantic=ErrorSemantic.USAGE,
            )
    try:
        with (
            _private_http(),
            SolwynClient(
                api_url=command_context(context).api_url,
                credential=None if new_user else credential,
            ) as client,
        ):
            method = client.request_unauthenticated if new_user else client.request
            response = method(
                "POST",
                _invitation_path(token) + "/accept",
                json=body,
            )
        tokens = shared.InvitationTokenResponse.model_validate(response.json())
    except ApiError as error:
        shared.safe_api_error(context, error, accepting=True, submitted=True)
    except AuthError:
        shared.fail(
            context,
            "invitation_auth_failed",
            "Invitation authentication or refresh failed.",
            shared.ACCEPT_UNKNOWN_HINT,
            ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        shared.fail(
            context,
            "invalid_invitation_accept_response",
            "The acceptance response was invalid; credentials were not stored.",
            shared.ACCEPT_UNKNOWN_HINT,
        )
    try:
        store_jwt_pair(
            tokens.access_token.get_secret_value(),
            tokens.refresh_token.get_secret_value(),
        )
    except (CredentialStorageError, AuthError):
        shared.fail(
            context,
            "invitation_credential_storage_failed",
            "Invitation acceptance completed, but the returned credentials could not be stored.",
            shared.ACCEPT_UNKNOWN_HINT,
            ErrorSemantic.AUTH,
        )
    shared.emit(
        context,
        "solwyn.invite.accept.v1",
        {
            "accepted": True,
            "auth_type": "jwt",
            "account_name": preview.account_name,
            "role": preview.role,
        },
    )
