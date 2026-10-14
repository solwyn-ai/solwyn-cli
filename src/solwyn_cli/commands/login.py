"""Login and logout commands."""

import sys
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from solwyn_cli._vendor.wire import LoginRequest
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    JwtCredential,
    ProjectKeyCredential,
    clear_credentials,
    store_jwt_pair,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


LOGIN_HELP = help_contract(
    endpoint="POST /api/v1/auth/login or local keychain write with --with-key",
    auth="none",
    exits="0 success; 1 generic/API; 2 usage; 3 auth or credential storage",
    schema="solwyn.login.v1",
    guidance=(
        "Keys and passwords are never accepted as command-line arguments or echoed.",
        "Read project keys only from a hidden prompt or stdin; passwords use a hidden prompt.",
    ),
)

LOGOUT_HELP = help_contract(
    endpoint="local keychain clear",
    auth="local (none)",
    exits="0 success; 2 usage; 3 credential storage",
    schema="solwyn.logout.v1",
)


class _LoginTokenResponse(BaseModel):
    """Validated login response whose tokens remain redacted in memory."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    token_type: Literal["bearer"] = "bearer"


def authenticate_with_project_key(project_key: str) -> ProjectKeyCredential:
    """Validate and store one project key through the shared auth boundary."""
    return store_project_key(project_key)


def authenticate_with_password(
    *, api_url: str, email: str, password: str
) -> JwtCredential:
    """Authenticate, validate the token response, and store the JWT pair."""
    request = LoginRequest(email=email, password=password)
    with SolwynClient(api_url=api_url, credential=None) as client:
        response = client.request_unauthenticated(
            "POST",
            "/api/v1/auth/login",
            json=request.model_dump(exclude_none=True),
        )
    tokens = _LoginTokenResponse.model_validate(response.json())
    return store_jwt_pair(
        tokens.access_token.get_secret_value(),
        tokens.refresh_token.get_secret_value(),
    )


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _login_success(context: typer.Context, auth_type: str) -> None:
    output_engine(context).write_success(
        schema="solwyn.login.v1",
        data={"auth_type": auth_type},
        table=TableData(
            columns=("Authentication", "Status"),
            rows=((auth_type, "authenticated"),),
            right_align=("Status",),
        ),
    )


def _login_with_key(context: typer.Context) -> None:
    project_key = (
        typer.prompt("Project key", hide_input=True, err=True)
        if _stdin_is_tty()
        else sys.stdin.read().strip()
    )
    try:
        authenticate_with_project_key(project_key)
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(code="credential_storage_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except AuthError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_key",
                message="The project key is empty or malformed.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    _login_success(context, "project_key")


def _login_with_password(context: typer.Context) -> None:
    if not _stdin_is_tty():
        exit_with_error(
            context,
            OutputError(
                code="interactive_auth_required",
                message="Email and password login requires an interactive terminal.",
                hint="Set SOLWYN_API_KEY or pipe a project key to login --with-key.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    email = typer.prompt("Email", err=True)
    password = typer.prompt("Password", hide_input=True, err=True)
    try:
        runtime = command_context(context)
        authenticate_with_password(
            api_url=runtime.api_url,
            email=email,
            password=password,
        )
    except ApiError as error:
        if error.status_code == 403 and error.code == "email_not_verified":
            error = ApiError(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
                hint="Verify your email in the dashboard, then try logging in again.",
                detail=error.detail,
            )
        exit_with_error(
            context,
            error,
            semantic=ErrorSemantic.GENERIC
            if error.status_code == 503
            else ErrorSemantic.AUTH,
        )
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(code="credential_storage_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_auth_response",
                message="The authentication response was invalid.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    _login_success(context, "jwt")


def login(
    context: typer.Context,
    with_key: Annotated[
        bool,
        typer.Option(
            "--with-key",
            help="Read and store a project API key from a hidden prompt or stdin.",
        ),
    ] = False,
) -> None:
    """Authenticate with Solwyn."""
    if with_key:
        _login_with_key(context)
    else:
        _login_with_password(context)


def logout(context: typer.Context) -> None:
    """Clear all stored Solwyn credentials."""
    try:
        clear_credentials()
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(code="credential_storage_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    output_engine(context).write_success(
        schema="solwyn.logout.v1",
        data={"cleared": True},
        table=TableData(
            columns=("Credentials", "Status"),
            rows=(("all", "cleared"),),
            right_align=("Status",),
        ),
    )
