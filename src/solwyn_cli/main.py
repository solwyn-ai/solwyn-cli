"""Command-line entrypoint for Solwyn."""

import re
from typing import Annotated

import typer
from pydantic import ValidationError

from solwyn_cli import __version__
from solwyn_cli.approvals import ApprovalSettings, activate, default_wait_seconds
from solwyn_cli.auth import auth_selection, use_api_url
from solwyn_cli.commands.account import app as account_app
from solwyn_cli.commands.team import app as team_app
from solwyn_cli.commands.invite import app as invite_app
from solwyn_cli.commands.channels import app as channels_app
from solwyn_cli.commands.notifications import app as notifications_app
from solwyn_cli.commands.agent_context import AGENT_CONTEXT_HELP, agent_context
from solwyn_cli.commands.attest import ATTEST_HELP, attest
from solwyn_cli.commands.audit import AUDIT_HELP, audit
from solwyn_cli.commands.overview import OVERVIEW_HELP, overview
from solwyn_cli.commands.budget import app as budget_app
from solwyn_cli.commands.alerts import app as alerts_app
from solwyn_cli.commands.approvals import app as approvals_app
from solwyn_cli.commands.budget_rules import register as register_budget_rules
from solwyn_cli.commands.config import app as config_app
from solwyn_cli.commands.cost_analysis import register_cost_analysis_commands
from solwyn_cli.commands.cost_pricing import register_cost_pricing_commands
from solwyn_cli.commands.costs import app as costs_app
from solwyn_cli.commands.credentials import app as credentials_app
from solwyn_cli.commands.forecast import FORECAST_HELP, forecast
from solwyn_cli.commands.doctor import DOCTOR_HELP, doctor
from solwyn_cli.commands.init import INIT_HELP, init
from solwyn_cli.commands.login import LOGIN_HELP, LOGOUT_HELP, login, logout
from solwyn_cli.commands.mcp import MCP_HELP, McpLaunchOptions, mcp
from solwyn_cli.commands.mcp_config import MCP_CONFIG_HELP, mcp_config
from solwyn_cli.commands.plan import app as plan_app
from solwyn_cli.commands.keys import app as keys_app
from solwyn_cli.commands.projects import app as projects_app
from solwyn_cli.commands.providers import app as providers_app
from solwyn_cli.commands.record import RECORD_HELP, record
from solwyn_cli.commands.tags import app as tags_app
from solwyn_cli.commands.runs import app as runs_app
from solwyn_cli.commands.status import STATUS_HELP, status
from solwyn_cli.commands.webhooks import app as webhooks_app
from solwyn_cli.commands.whoami import WHOAMI_HELP, whoami
from solwyn_cli.config import ConfigLoadError
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, OutputUsageError
from solwyn_cli.runtime import (
    bootstrap_output_engine,
    resolve_command_context,
    validate_output_intent,
)

_APPROVAL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")

app = typer.Typer(
    add_completion=False,
    # A crash must never print local variables: they can hold a key read from
    # a hidden prompt or a token. Errors are rendered as structured output.
    pretty_exceptions_show_locals=False,
    invoke_without_command=True,
    no_args_is_help=True,
    epilog=help_contract(
        endpoint="composite command router",
        auth="varies by command",
        exits="0 success; 2 usage",
        schema="varies by command",
        guidance=(
            "Agents: run solwyn agent-context for the compact capability contract.",
        ),
    ),
)
register_budget_rules(budget_app)
app.add_typer(budget_app, name="budget")
app.add_typer(account_app, name="account")
app.add_typer(team_app, name="team")
app.add_typer(invite_app, name="invite")
app.add_typer(channels_app, name="channels")
app.add_typer(notifications_app, name="notifications")
app.add_typer(tags_app, name="tags")
app.add_typer(config_app, name="config")
app.add_typer(keys_app, name="keys")
app.add_typer(projects_app, name="projects")
app.add_typer(webhooks_app, name="webhooks")
app.add_typer(runs_app, name="runs")
app.command("agent-context", epilog=AGENT_CONTEXT_HELP)(agent_context)
app.command("attest", epilog=ATTEST_HELP)(attest)
register_cost_analysis_commands(costs_app)
register_cost_pricing_commands(costs_app)
app.add_typer(costs_app, name="costs")
app.add_typer(providers_app, name="providers")
app.command("forecast", epilog=FORECAST_HELP)(forecast)
app.command("doctor", epilog=DOCTOR_HELP)(doctor)
app.add_typer(alerts_app, name="alerts")
app.add_typer(approvals_app, name="approvals")
app.add_typer(credentials_app, name="credentials")
app.command("audit", epilog=AUDIT_HELP)(audit)
app.command("init", epilog=INIT_HELP)(init)
app.command("login", epilog=LOGIN_HELP)(login)
app.command("logout", epilog=LOGOUT_HELP)(logout)
app.command("mcp", epilog=MCP_HELP)(mcp)
app.command("mcp-config", epilog=MCP_CONFIG_HELP)(mcp_config)
app.command("record", epilog=RECORD_HELP)(record)
app.add_typer(plan_app, name="plan")
app.command("overview", epilog=OVERVIEW_HELP)(overview)
app.command("status", epilog=STATUS_HELP)(status)
app.command("whoami", epilog=WHOAMI_HELP)(whoami)


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
    approval_wait: Annotated[
        int | None,
        typer.Option(
            "--approval-wait",
            min=0,
            help=(
                "Seconds to wait for a browser approval before exiting 6. "
                "Defaults to 600 with a terminal, else 90."
            ),
        ),
    ] = None,
    approval: Annotated[
        str | None,
        typer.Option(
            "--approval",
            help="Approval ID to attach to this invocation's first change request.",
        ),
    ] = None,
) -> None:
    """Work with Solwyn from the command line."""
    if version:
        typer.echo(f"solwyn {__version__}")
        raise typer.Exit()
    if context.invoked_subcommand == "mcp":
        context.obj = McpLaunchOptions(api_url=api_url, project_id=project)
        return
    if context.invoked_subcommand in {"agent-context", "config"}:
        return
    try:
        auth_selection()
    except ValueError:
        raise typer.BadParameter(
            "SOLWYN_AUTH must be login or project_key.", param_hint="SOLWYN_AUTH"
        ) from None
    if approval is not None and _APPROVAL_ID.fullmatch(approval) is None:
        raise typer.BadParameter("Invalid approval ID.", param_hint="approval")
    context.call_on_close(
        activate(
            ApprovalSettings(
                wait_seconds=(
                    default_wait_seconds() if approval_wait is None else approval_wait
                ),
                approval_id=approval,
            )
        )
    )
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
            allow_config_error=context.invoked_subcommand == "doctor",
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
    # Stored credentials are read and written for this invocation's API only.
    context.call_on_close(use_api_url(context.obj.api_url))
