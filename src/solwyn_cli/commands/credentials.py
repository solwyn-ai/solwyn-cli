"""List and revoke CLI logins."""

from datetime import datetime
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from solwyn_cli import auth
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    LoginCredential,
    ApiKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

BASE = "/api/v1/cli/credentials"
MIN_PREFIX = 8

app = typer.Typer(
    help="List and revoke CLI logins.",
    epilog=help_contract(
        endpoint="composite router for CLI login subcommands",
        auth="login",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    ),
)


class CliCredentialItem(BaseModel):
    """One active CLI login, as Core lists it. Never includes the token."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    label: str
    client_id: str
    client_name: str
    client_version: str | None
    scope: str
    token_prefix: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_used_at: AwareDatetime | None
    created_ip: str | None = None
    current: bool


class _CredentialList(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    credentials: list[CliCredentialItem]


class _RevokeData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    revoked: bool
    current: bool
    cleared_local_login: bool


def _login(context: typer.Context) -> LoginCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ApiKeyCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="CLI logins can only be managed with a Solwyn login.",
                hint="Run solwyn login, then retry.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def fetch_credentials(*, client: SolwynClient) -> list[CliCredentialItem]:
    """Read the caller's active CLI logins, newest first."""
    response = client.request("GET", BASE)
    return _CredentialList.model_validate(response.json()).credentials


def _invalid_response(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="invalid_credentials_response",
            message="The CLI login response was invalid.",
        ),
    )


def _auth_failed(context: typer.Context, error: AuthError) -> None:
    exit_with_error(
        context,
        OutputError(code="authentication_failed", message=str(error)),
        semantic=ErrorSemantic.AUTH,
    )


def _date(value: datetime | None) -> str:
    return "never" if value is None else value.date().isoformat()


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/cli/credentials",
        auth="login",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.credentials.list.v1",
        guidance=("Lists active logins only. Tokens are never shown.",),
    ),
)
def list_credentials(context: typer.Context) -> None:
    """List your active CLI logins."""
    credential = _login(context)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            items = fetch_credentials(client=client)
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        _auth_failed(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)
    output_engine(context).write_success(
        schema="solwyn.credentials.list.v1",
        data={"credentials": items},
        table=TableData(
            columns=("ID", "Label", "Scope", "Created", "Last used", "Expires", ""),
            rows=tuple(
                (
                    item.id[:MIN_PREFIX],
                    item.label,
                    ",".join(item.scope.split()),
                    _date(item.created_at),
                    _date(item.last_used_at),
                    _date(item.expires_at),
                    "current" if item.current else "",
                )
                for item in items
            ),
        ),
    )


def _select(
    context: typer.Context,
    items: list[CliCredentialItem],
    credential_id: str | None,
) -> CliCredentialItem:
    if credential_id is None:
        matches = [item for item in items if item.current]
    else:
        matches = [item for item in items if item.id.startswith(credential_id)]
    if not matches:
        exit_with_error(
            context,
            OutputError(
                code="cli_credential_not_found",
                message="No active CLI login matches.",
                hint="Run solwyn credentials list to see your logins.",
            ),
            semantic=ErrorSemantic.NOT_FOUND,
        )
    if len(matches) > 1:
        exit_with_error(
            context,
            OutputError(
                code="cli_credential_ambiguous",
                message="More than one CLI login matches that prefix.",
                hint="Use more characters of the ID.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return matches[0]


@app.command(
    "revoke",
    epilog=help_contract(
        endpoint="GET /api/v1/cli/credentials; DELETE /api/v1/cli/credentials/{id}",
        auth="login",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.credentials.revoke.v1",
        guidance=(
            "Accepts an ID or a unique prefix of at least 8 characters.",
            "--current revokes the login in use and clears it from the credential store.",
        ),
    ),
)
def revoke_credential(
    context: typer.Context,
    credential_id: Annotated[
        str | None,
        typer.Argument(
            help="CLI login ID, or a unique prefix of 8 or more characters."
        ),
    ] = None,
    current: Annotated[
        bool,
        typer.Option("--current", help="Revoke the login in use."),
    ] = False,
) -> None:
    """Revoke one of your CLI logins. Revocation is immediate."""
    if (credential_id is None) == (not current):
        exit_with_error(
            context,
            OutputError(
                code="invalid_revoke_request",
                message="Give a CLI login ID or --current, not both.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if credential_id is not None and len(credential_id) < MIN_PREFIX:
        exit_with_error(
            context,
            OutputError(
                code="invalid_revoke_request",
                message=f"Give at least {MIN_PREFIX} characters of the CLI login ID.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _login(context)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            target = _select(context, fetch_credentials(client=client), credential_id)
            response = client.request("DELETE", f"{BASE}/{quote(target.id, safe='')}")
            if response.status_code != 204:
                raise ValueError("Expected a bodyless revoke response")
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        _auth_failed(context, error)
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)
    # Clear the stored login only when it is the one revoked; a SOLWYN_TOKEN
    # login is not stored, so revoking it leaves any stored login alone.
    try:
        stored = (
            credential
            if credential.source != "environment"
            else auth.stored_login(include_expired=True)
        )
    except CredentialStorageError:
        stored = None
    stored_id = stored.meta.id if stored is not None and stored.meta else None
    is_local = stored_id == target.id or (
        target.current and credential.source != "environment"
    )
    if is_local:
        try:
            auth.clear_login()
        except CredentialStorageError as error:
            exit_with_error(
                context,
                OutputError(
                    code="credential_storage_failed",
                    message=(
                        "The login was revoked, but the credential store could "
                        "not be cleared."
                    ),
                    hint=str(error),
                ),
                semantic=ErrorSemantic.AUTH,
            )
    data = _RevokeData(
        id=target.id,
        label=target.label,
        revoked=True,
        current=target.current,
        cleared_local_login=is_local,
    )
    output_engine(context).write_success(
        schema="solwyn.credentials.revoke.v1",
        data=data,
        table=TableData(
            columns=("ID", "Label", "Status"),
            rows=((target.id[:MIN_PREFIX], target.label, "revoked"),),
        ),
    )
