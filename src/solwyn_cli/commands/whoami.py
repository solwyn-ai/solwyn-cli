"""Current identity command."""

from datetime import datetime
from typing import Literal

import typer
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli.auth import (
    AuthError,
    LoginCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


WHOAMI_HELP = help_contract(
    endpoint=(
        "local key identity or GET /api/v1/account/me, "
        "GET /api/v1/accounts/me, and GET /api/v1/projects"
    ),
    auth="any",
    exits="0 success; 1 generic/API; 2 usage; 3 auth",
    schema="solwyn.whoami.v1",
)


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email_verified: bool
    first_name: str
    marketing_emails_subscribed: bool
    account_id: str
    alert_email_override: str | None
    user_id: str
    email: str
    display_name: str
    subscription_tier: Literal["free", "production", "team", "scale"]


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class _AccountSelf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    subscription_tier: Literal["free", "production", "team", "scale"]
    alert_email_override: str | None
    seat_limit: int
    seats_used: int
    your_role: Literal["owner", "admin", "member"]


class _CredentialSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["login", "project_key"]
    source: Literal["keyring", "environment"]
    label: str | None
    scope: list[str] | None
    expires_at: datetime | None


class _WhoamiData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_type: Literal["login", "project_key"]
    auth_source: Literal["keyring", "environment"]
    credential: _CredentialSummary
    configured_project: str | None
    account_id: str | None
    your_role: Literal["owner", "admin", "member"] | None
    seats_used: int | None
    seat_limit: int | None
    user: _Identity | None
    projects: list[_Project]
    hint: str | None


_PROJECTS = TypeAdapter(list[_Project])


def _key_data(credential: ProjectKeyCredential, project: str | None) -> _WhoamiData:
    return _WhoamiData(
        auth_type="project_key",
        auth_source=credential.source,
        credential=_CredentialSummary(
            kind="project_key",
            source=credential.source,
            label=None,
            scope=None,
            expires_at=None,
        ),
        configured_project=project,
        account_id=None,
        your_role=None,
        seats_used=None,
        seat_limit=None,
        user=None,
        projects=[],
        hint=(
            None
            if project is not None
            else "Choose a project with --project, SOLWYN_PROJECT_ID, or config."
        ),
    )


def fetch_identity(*, client: SolwynClient, project: str | None = None) -> _WhoamiData:
    credential = client.current_credential
    if isinstance(credential, ProjectKeyCredential):
        return _key_data(credential, project)
    identity = _Identity.model_validate(
        client.request("GET", "/api/v1/account/me").json()
    )
    account = _AccountSelf.model_validate(
        client.request("GET", "/api/v1/accounts/me").json()
    )
    projects = _PROJECTS.validate_python(
        client.request("GET", "/api/v1/projects").json()
    )
    if identity.account_id != account.id:
        raise ValueError("Identity account did not match.")
    meta = credential.meta
    return _WhoamiData(
        auth_type="login",
        auth_source="keyring",
        credential=_CredentialSummary(
            kind="login",
            source="keyring",
            label=meta.label,
            scope=meta.scopes,
            expires_at=meta.expires_at,
        ),
        configured_project=None,
        account_id=account.id,
        your_role=account.your_role,
        seats_used=account.seats_used,
        seat_limit=account.seat_limit,
        user=identity,
        projects=projects,
        hint=None,
    )


def _jwt_data(context: typer.Context, credential: LoginCredential) -> _WhoamiData:
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            return fetch_identity(client=client)
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
                code="invalid_identity_response",
                message="The identity response was invalid.",
            ),
        )


def _credential_text(credential: _CredentialSummary) -> str:
    if credential.kind == "project_key":
        return f"project key ({credential.source})"
    scope = ", ".join(credential.scope or [])
    expires = (
        credential.expires_at.date().isoformat()
        if credential.expires_at is not None
        else "unknown"
    )
    return f"CLI login {credential.label} ({scope}), expires {expires}"


def _table(data: _WhoamiData) -> TableData:
    rows: list[tuple[str, str]] = [
        ("Credential", _credential_text(data.credential)),
        ("Authentication", data.auth_type),
        ("Source", data.auth_source),
    ]
    if data.auth_type == "project_key":
        rows.append(("Configured project", data.configured_project or "not configured"))
        if data.hint is not None:
            rows.append(("Hint", data.hint))
    elif data.user is not None:
        rows.extend(
            [
                ("User", data.user.display_name),
                ("Email", data.user.email),
                ("Email verified", "yes" if data.user.email_verified else "no"),
                ("Tier", data.user.subscription_tier),
                ("Account ID", data.account_id or "unavailable"),
                ("Role", data.your_role or "unavailable"),
                (
                    "Seats",
                    (
                        f"{data.seats_used}/{data.seat_limit}"
                        if data.seats_used is not None and data.seat_limit is not None
                        else "unavailable"
                    ),
                ),
            ]
        )
        rows.extend(
            ("Project", f"{project.id} ({project.name})") for project in data.projects
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def whoami(context: typer.Context) -> None:
    """Show the current Solwyn identity and project context."""
    credential = required_credential(context, resolve=resolve_credential)
    runtime = command_context(context)
    data = (
        _key_data(credential, runtime.project)
        if isinstance(credential, ProjectKeyCredential)
        else _jwt_data(context, credential)
    )
    output_engine(context).write_success(
        schema="solwyn.whoami.v1",
        data=data,
        table=_table(data),
    )
