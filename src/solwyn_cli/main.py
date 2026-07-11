"""Command-line entrypoint for Solwyn."""

from typing import Annotated

import typer
from pydantic import ValidationError

from solwyn_cli import __version__
from solwyn_cli.commands.budget import app as budget_app
from solwyn_cli.commands.config import app as config_app
from solwyn_cli.commands.costs import costs
from solwyn_cli.commands.init import init
from solwyn_cli.commands.login import login, logout
from solwyn_cli.commands.keys import app as keys_app
from solwyn_cli.commands.projects import app as projects_app
from solwyn_cli.commands.status import status
from solwyn_cli.commands.whoami import whoami
from solwyn_cli.config import ConfigLoadError
from solwyn_cli.output import OutputError, OutputUsageError
from solwyn_cli.runtime import (
    bootstrap_output_engine,
    resolve_command_context,
    validate_output_intent,
)

app = typer.Typer(
    add_completion=False, invoke_without_command=True, no_args_is_help=True
)
app.add_typer(budget_app, name="budget")
app.add_typer(config_app, name="config")
app.add_typer(keys_app, name="keys")
app.add_typer(projects_app, name="projects")
app.command("costs")(costs)
app.command("init")(init)
app.command("login")(login)
app.command("logout")(logout)
app.command("status")(status)
app.command("whoami")(whoami)


@app.callback()
def main(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Show the installed version and exit.", is_eager=True
        ),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project ID for this invocation."),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option("--api-url", help="Solwyn API base URL."),
    ] = None,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="Output format: table or json."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit schema-bearing JSON output."),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable terminal color."),
    ] = False,
) -> None:
    """Work with Solwyn from the command line."""
    if version:
        typer.echo(f"solwyn {__version__}")
        raise typer.Exit()
    if context.invoked_subcommand == "config":
        return
    try:
        validate_output_intent(
            output_format=output_format,
            json_flag=json_output,
        )
    except OutputUsageError:
        raise typer.BadParameter(
            "Invalid or conflicting output options.", param_hint="format"
        ) from None
    try:
        context.obj = resolve_command_context(
            api_url=api_url,
            project=project,
            output_format=output_format,
            json_flag=json_output,
            no_color=no_color,
        )
    except OutputUsageError:
        raise typer.BadParameter(
            "Invalid or conflicting output options.", param_hint="format"
        ) from None
    except ValidationError:
        raise typer.BadParameter("Invalid API URL.", param_hint="api-url") from None
    except ConfigLoadError:
        if context.invoked_subcommand == "init":
            bootstrap_output_engine(
                output_format=output_format,
                json_flag=json_output,
                no_color=no_color,
            ).write_error(
                OutputError(
                    code="config_load_failed",
                    message="The existing configuration could not be loaded.",
                    hint="Repair or remove the configuration file, then retry init.",
                )
            )
            raise typer.Exit(code=1) from None
        raise typer.BadParameter(
            "Could not load configuration.", param_hint="config"
        ) from None
