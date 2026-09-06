"""Account alert-email preferences."""

import re
from typing import Annotated

import typer

from solwyn_cli._vendor.notification_wire import AlertEmailUpdate
from solwyn_cli.commands._notifications_common import (
    AlertEmailResponse,
    command_help,
    emit,
    group_help,
    request,
    usage,
)

app = typer.Typer(help="Manage account settings.", epilog=group_help("account"))
alert_email_app = typer.Typer(
    help="Manage the account-wide alert-email override.",
    epilog=group_help("account alert-email"),
)
app.add_typer(alert_email_app, name="alert-email")
BASE = "/api/v1/account/alert-email"
SCHEMA = "solwyn.account.alert-email.v1"


@alert_email_app.command("show", epilog=command_help("GET " + BASE, SCHEMA, auth="JWT"))
def show(context: typer.Context) -> None:
    """Show the override and effective alert destination."""
    emit(context, SCHEMA, request(context, "GET", BASE, AlertEmailResponse))


@alert_email_app.command("set", epilog=command_help("PUT " + BASE, SCHEMA))
def set_email(context: typer.Context, email: Annotated[str, typer.Argument()]) -> None:
    """Set the account-wide alert destination (owner only)."""
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        usage(context, "Provide a valid email address.")
    emit(
        context,
        SCHEMA,
        request(
            context,
            "PUT",
            BASE,
            AlertEmailResponse,
            body=AlertEmailUpdate(override=email),
        ),
    )


@alert_email_app.command("clear", epilog=command_help("DELETE " + BASE, SCHEMA))
def clear(context: typer.Context) -> None:
    """Remove the override and use the login email again (owner only)."""
    emit(context, SCHEMA, request(context, "DELETE", BASE, AlertEmailResponse))
