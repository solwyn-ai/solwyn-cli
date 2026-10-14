"""Account alert-email preferences."""

import re
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli._vendor.notification_wire import AlertEmailUpdate
from solwyn_cli.commands._notifications_common import (
    AlertEmailResponse,
    command_help,
    emit,
    group_help,
    request,
    usage,
)
from solwyn_cli.auth import AuthError, JwtCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

app = typer.Typer(help="Manage account settings.", epilog=group_help("account"))
alert_email_app = typer.Typer(
    help="Manage the account-wide alert-email override.",
    epilog=group_help("account alert-email"),
)
app.add_typer(alert_email_app, name="alert-email")
BASE = "/api/v1/account/alert-email"
SCHEMA = "solwyn.account.alert-email.v1"


class PersonalAccountResponse(BaseModel):
    """Core's membership-independent recovery acknowledgement."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: str


@app.command(
    "recover",
    epilog=help_contract(
        endpoint="POST /api/v1/accounts/me/personal",
        auth="JWT; active membership not required",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.account.recover.v1",
        guidance=(
            "Idempotently creates a personal account when you have no active membership.",
            "Returns your existing account when an active membership already exists.",
        ),
    ),
)
def recover(context: typer.Context) -> None:
    """Recover a personal account after membership removal."""
    credential = required_credential(context, resolve=resolve_credential)
    if not isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Account recovery requires user login.",
                hint="Unset SOLWYN_API_KEY, clear any stored project key with solwyn logout, and run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            response = client.request("POST", "/api/v1/accounts/me/personal")
        data = PersonalAccountResponse.model_validate(response.json())
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_account_recovery_response",
                message="The account recovery response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.account.recover.v1",
        data=data,
        table=TableData(columns=("Account",), rows=((data.account_id,),)),
    )


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
