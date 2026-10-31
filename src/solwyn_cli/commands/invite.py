"""Private stdin/hidden-prompt invitation preview. Acceptance is retired."""

from contextlib import contextmanager, redirect_stdout
import logging
import re
import sys
from urllib.parse import quote

import typer

try:
    from typer._click.exceptions import UsageError
except ImportError:  # Typer releases before the bundled Click boundary.
    from click.exceptions import UsageError

from typer.core import TyperCommand, TyperGroup
from pydantic import SecretStr

from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import _team as shared
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
    help="Preview an invitation privately.",
    epilog=shared.command_help(
        "/api/v1/invitations/{token}",
        "varies by command",
        "public preview",
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
        shared.safe_api_error(context, error, preview=True)
    except (ValueError, TypeError):
        shared.fail(
            context,
            "invalid_invitation_response",
            "The invitation preview response was invalid.",
            shared.READ_HINT,
        )
    shared.emit(context, "solwyn.invite.show.v1", data)


@app.command(
    "accept",
    cls=_PrivateCommand,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    epilog=shared.command_help(
        "none (retired)",
        "solwyn.error.v1",
        "none",
    ),
)
def accept(context: typer.Context):
    """Retired. Accept invitations from the emailed link, then run solwyn login."""
    shared.fail(
        context,
        "invite_accept_retired",
        "Accept the invitation from the link in your email, then run solwyn login.",
        semantic=ErrorSemantic.USAGE,
    )
