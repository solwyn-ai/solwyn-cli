"""Stable, machine-legible CLI help contract formatting."""

import re

import typer
from typer import _click as click
from typer.core import HAS_RICH, TyperCommand

from solwyn_cli.approvals import may_need_approval
from solwyn_cli.output import _terminal_safe

APPROVAL_EXIT = "6 browser approval required, pending, denied or expired"
_WRITE = re.compile(r"\b(POST|PUT|PATCH|DELETE) (/[^\s;,)]+)")


def help_contract(
    *,
    endpoint: str,
    auth: str,
    exits: str,
    schema: str,
    guidance: tuple[str, ...] = (),
) -> str:
    """Render one semantic help contract without terminal-specific formatting.

    A command whose endpoint names a change that can need a browser approval
    also lists exit 6.
    """
    if not re.search(r"\b6 ", exits) and any(
        may_need_approval(method, route) for method, route in _WRITE.findall(endpoint)
    ):
        exits = f"{exits}; {APPROVAL_EXIT}"
    contract = (
        f"Endpoint: {endpoint}",
        f"Auth: {auth}",
        f"Exit codes: {exits}",
        f"JSON schema: {schema}",
    )
    return "\n".join((*contract, *guidance))


# A guidance line that starts with VERBATIM is a command meant to be copied.
# Rich help joins epilog lines and re-wraps them at the console width, and a
# shell command split across lines can run something else: `eval` then
# "$solwyn_env" on the next line runs the key as a command name, and the
# shell's error prints it. ContractCommand prints these lines on their own,
# exactly as written, never wrapped.
VERBATIM = "\b"


def verbatim(line: str) -> str:
    """A help_contract guidance line shown exactly as written, on one line."""
    return VERBATIM + line


def split_verbatim(epilog: str) -> tuple[str, list[str]]:
    """(the epilog without verbatim lines, the verbatim lines as written)."""
    lines = epilog.split("\n")
    kept = [line for line in lines if not line.startswith(VERBATIM)]
    copied = [line[len(VERBATIM) :] for line in lines if line.startswith(VERBATIM)]
    return "\n".join(kept), copied


class ContractCommand(TyperCommand):
    """A command whose help prints verbatim guidance lines unwrapped."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        epilog = self.epilog or ""
        self.epilog, copied = split_verbatim(epilog)
        try:
            super().format_help(ctx, formatter)
        finally:
            self.epilog = epilog
        if not copied:
            return
        # Each line is neutralized on its own: a newline is a control
        # character, and the lines must stay separate.
        if HAS_RICH and self.rich_markup_mode is not None:
            # Rich printed the rest straight to stdout; never through Rich here.
            for line in copied:
                typer.echo(_terminal_safe(f" {line}"))
            typer.echo()
        else:
            formatter.write_paragraph()
            for line in copied:
                formatter.write(_terminal_safe(f"  {line}") + "\n")
