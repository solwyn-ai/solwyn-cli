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


phone_app = typer.Typer(
    help="Inspect phone verification and SMS consent.",
    epilog=help_contract(
        endpoint="GET /api/v1/account/phone",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.account.phone.v1",
    ),
)
app.add_typer(phone_app, name="phone")


@phone_app.command(
    "show",
    epilog=help_contract(
        endpoint="GET /api/v1/account/phone",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.account.phone.v1",
    ),
)
def phone_show(context: typer.Context) -> None:
    """Show phone and SMS prerequisites; verify and consent in the dashboard."""
    from solwyn_cli.commands._account_reads import fetch_phone

    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            data = fetch_phone(client=client)
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_required", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_phone_response", message="The phone response was invalid."
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.account.phone.v1",
        data=data,
        table=TableData(
            columns=("Field", "Value"),
            rows=tuple(
                (k, str(v) if v is not None else "none")
                for k, v in data.model_dump(mode="json").items()
            ),
            fold_cells=True,
        ),
    )


@app.command(
    "rename",
    epilog=help_contract(
        endpoint="PATCH /api/v1/accounts/me",
        auth="owner JWT",
        exits="0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
        schema="solwyn.account.rename.v1",
        guidance=(
            "Shows old/new account names before interactive confirmation or --yes. Sends only display_name; use account alert-email for its independent override.",
        ),
    ),
)
def rename(
    context: typer.Context,
    name: Annotated[
        str, typer.Argument(help="New account display name (1–120 characters).")
    ],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Acknowledge the displayed rename without prompting."
        ),
    ] = False,
) -> None:
    """Rename your account (owner only), preserving its alert-email override."""
    from solwyn_cli.commands import _team as shared

    shared.preflight(context, yes)
    try:
        body = shared.AccountUpdateRequest(display_name=name)
    except ValidationError:
        shared.fail(
            context,
            "invalid_account_name",
            "Provide an account display name of 1–120 characters.",
            semantic=ErrorSemantic.USAGE,
        )
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        shared.require_role(context, account, ("owner",))
        shared.effects(
            f"Rename account {account.id} from {account.display_name} to {name}. The account display name is used in invitation emails."
        )
        shared.confirm(context, yes)
        # Override changes belong to the dedicated account alert-email commands.
        with shared.write_response(context):
            data = shared._AccountSelf.model_validate(
                shared.write_request(
                    context,
                    client,
                    "PATCH",
                    shared.BASE,
                    json=body.model_dump(include={"display_name"}),
                ).json()
            )
            if data.id != account.id:
                raise ValueError("Account mismatch")
    shared.emit(context, "solwyn.account.rename.v1", data)
