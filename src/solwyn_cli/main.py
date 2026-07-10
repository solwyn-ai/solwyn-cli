"""Command-line entrypoint for Solwyn."""

from typing import Annotated

import typer

from solwyn_cli import __version__
from solwyn_cli.commands.config import app as config_app

app = typer.Typer(
    add_completion=False, invoke_without_command=True, no_args_is_help=True
)
app.add_typer(config_app, name="config")


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Show the installed version and exit.", is_eager=True
        ),
    ] = False,
) -> None:
    """Work with Solwyn from the command line."""
    if version:
        typer.echo(f"solwyn {__version__}")
        raise typer.Exit()
