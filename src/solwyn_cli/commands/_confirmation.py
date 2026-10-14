"""CLI-only confirmation for destructive customer actions."""

import sys
from contextlib import redirect_stdout

import typer

from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, _terminal_safe


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def check_confirmation(context: typer.Context, *, yes: bool, effect: str) -> None:
    """Refuse unattended mutations before credential lookup or HTTP."""
    if not yes and not is_interactive():
        exit_with_error(
            context,
            OutputError(
                code="confirmation_required",
                message=_terminal_safe(effect) + " Confirmation is required.",
                hint="Use an interactive terminal or pass --yes to acknowledge these effects.",
            ),
            semantic=ErrorSemantic.USAGE,
        )


class _ConfirmationEcho:
    """Keep Click's input echo on sanitized stderr, including under CliRunner."""

    def write(self, text: str) -> int:
        safe = "\n".join(_terminal_safe(line) for line in text.split("\n"))
        sys.stderr.write(safe)
        return len(text)

    def flush(self) -> None:
        sys.stderr.flush()


def confirm_effects(context: typer.Context, *, yes: bool, effect: str) -> None:
    """Show literal effects and require acceptance before dispatching a write."""
    typer.echo(_terminal_safe(effect), err=True)
    if yes:
        return
    try:
        with redirect_stdout(_ConfirmationEcho()):
            accepted = typer.confirm(
                "Continue with these effects?", default=False, err=True
            )
    except (EOFError, KeyboardInterrupt, typer.Abort):
        typer.echo(err=True)
        accepted = False
    if not accepted:
        exit_with_error(
            context,
            OutputError(
                code="confirmation_declined",
                message="The action was not submitted.",
                hint="Review the effects before confirming or using --yes.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
