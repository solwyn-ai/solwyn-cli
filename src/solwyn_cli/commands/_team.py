"""Strict customer team contracts, shared reads, and CLI write boundaries."""

import re
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
import sys
from typing import Literal
from urllib.parse import quote

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    JwtRequiredError,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands.whoami import _AccountSelf, _Identity
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, _terminal_safe
from solwyn_cli.runtime import command_context, output_engine

BASE = "/api/v1/accounts/me"
Role = Literal["owner", "admin", "member"]
InvitableRole = Literal["admin", "member"]
UNKNOWN_HINT = "The write may already have completed. Inspect team/account state before a deliberate retry; do not blindly replay."
ACCEPT_UNKNOWN_HINT = "Acceptance may already have completed. Try solwyn login and inspect solwyn whoami before a deliberate retry; do not automatically accept again."
JWT_HINT = "Run solwyn login; unset SOLWYN_API_KEY and clear any selected stored project key to use JWT."


class TeamModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class MemberOut(TeamModel):
    user_id: str
    email: str
    name: str | None
    role: Role
    joined_at: datetime


class InvitationOut(TeamModel):
    id: str
    invited_email: str
    invited_role: Role
    expires_at: datetime
    delivery_status: str


class InvitationPreviewResponse(TeamModel):
    account_name: str
    inviter_email: str | None
    role: Role
    expires_at: datetime


class RoleUpdateRequest(TeamModel):
    role: InvitableRole


class InviteRequest(TeamModel):
    email: str
    role: InvitableRole = "member"


class AccountUpdateRequest(TeamModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    alert_email_override: str | None = None


class AcceptInvitationRequest(TeamModel):
    email: str | None = None
    password: SecretStr | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)


class InvitationTokenResponse(TeamModel):
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    token_type: Literal["bearer"] = "bearer"


class TeamMembersData(TeamModel):
    members: list[MemberOut]


class TeamInvitesData(TeamModel):
    invitations: list[InvitationOut]


_MEMBERS = TypeAdapter(list[MemberOut])
_INVITES = TypeAdapter(list[InvitationOut])


def require_team_jwt(client: SolwynClient) -> None:
    if not isinstance(client.current_credential, JwtCredential):
        raise JwtRequiredError(JWT_HINT)


def fetch_members(*, client: SolwynClient) -> TeamMembersData:
    require_team_jwt(client)
    return TeamMembersData(
        members=_MEMBERS.validate_python(
            client.request("GET", BASE + "/members").json()
        )
    )


def fetch_invites(*, client: SolwynClient) -> TeamInvitesData:
    require_team_jwt(client)
    return TeamInvitesData(
        invitations=_INVITES.validate_python(
            client.request("GET", BASE + "/invitations").json()
        )
    )


def fetch_account(client: SolwynClient) -> _AccountSelf:
    return _AccountSelf.model_validate(client.request("GET", BASE).json())


def fetch_current_user(client: SolwynClient, account: _AccountSelf) -> str:
    identity = _Identity.model_validate(
        client.request("GET", "/api/v1/account/me").json()
    )
    if identity.account_id != account.id:
        raise ValueError("Identity account mismatch")
    return identity.user_id


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def preflight(context: typer.Context, yes: bool) -> None:
    if not yes and not is_interactive():
        fail(
            context,
            "confirmation_required",
            "Confirmation is required before reading credentials or submitting this action.",
            "Use an interactive terminal or pass --yes to acknowledge the displayed effects.",
            ErrorSemantic.USAGE,
        )


def fail(context, code, message, hint=None, semantic=None):
    exit_with_error(
        context, OutputError(code=code, message=message, hint=hint), semantic=semantic
    )


def effects(text: str) -> None:
    typer.echo(_terminal_safe(text), err=True)


def confirm(context: typer.Context, yes: bool) -> None:
    if yes:
        return
    try:
        with redirect_stdout(sys.stderr):
            accepted = typer.confirm(
                "Continue with these account/team effects?", default=False, err=True
            )
    except (EOFError, typer.Abort, KeyboardInterrupt):
        accepted = False
    if not accepted:
        fail(
            context,
            "confirmation_declined",
            "The action was not submitted.",
            semantic=ErrorSemantic.USAGE,
        )


_TEAM_HINTS = {
    "seat_limit_exceeded": "Inspect solwyn whoami and solwyn plan list; the account needs enough seats before inviting or joining.",
    "email_provider_unavailable": "Email service is unavailable. Inspect solwyn team invites list before deliberately retrying; a previous attempt may have persisted invitation state.",
    "last_owner_cannot_leave": "The last owner cannot leave. Keep this membership; arrange ownership with the dashboard or support before trying again. CLI role changes cannot transfer ownership.",
    "owner_change_not_supported": "Owner role changes are unsupported. Use admin or member for non-owner targets; arrange ownership through the dashboard or support.",
    "role_cannot_remove": "Owners may remove admins/members; admins may remove members. Any role may leave its own membership.",
    "user_already_has_account": "The invited user must have no active account membership. Inspect solwyn whoami before proceeding.",
    "user_already_exists": "This user already exists. Run solwyn login, then use signed-in invite accept.",
    "invite_already_pending": "Inspect solwyn team invites list before deciding whether to resend.",
    "invitation_not_found": "Inspect solwyn team invites list for current invitations.",
    "member_not_found": "Inspect solwyn team members list for current members.",
    "invitation_resend_unavailable": "This invitation cannot be resent. Inspect the dashboard before creating a replacement.",
    "cannot_invite_self": "Choose another recipient; inspect solwyn whoami for your identity.",
    "auth_capacity_exceeded": "Authentication is busy; retry in a moment. No invitation acceptance occurred for this capacity rejection.",
    "account_membership_revoked": "Run solwyn login, then solwyn account recover if you need a personal account.",
}


def safe_api_error(
    context: typer.Context, error: ApiError, *, accepting=False, submitted=False
):
    # Team/invitation endpoints can reflect validation inputs or arbitrary error
    # bodies. Expose only fixed codes, status, and local guidance.
    code = (
        error.code
        if error.code in _TEAM_HINTS
        else error.detail
        if isinstance(error.detail, str) and error.detail in _TEAM_HINTS
        else None
    )
    hint = _TEAM_HINTS.get(code) if code else None
    if error.status_code == 410:
        code, hint = (
            "invitation_gone",
            "The invitation is invalid, expired, revoked, or accepted. Ask the account owner/admin to inspect its state.",
        )
    elif error.status_code in (401, 403) and code is None:
        code, hint = (
            "team_access_denied",
            JWT_HINT + " Check your account role and invited identity.",
        )
    if (
        submitted
        and (error.status_code is None or error.status_code >= 500)
        and code != "auth_capacity_exceeded"
    ):
        hint = ACCEPT_UNKNOWN_HINT if accepting else UNKNOWN_HINT
    fail(
        context,
        code or ("rate_limited" if error.status_code == 429 else "team_request_failed"),
        "The invitation request failed."
        if accepting
        else "The team/account request failed.",
        hint,
        ErrorSemantic.AUTH
        if error.status_code in (401, 403)
        else ErrorSemantic.NOT_FOUND
        if error.status_code == 404
        else ErrorSemantic.GENERIC,
    )


@contextmanager
def team_client(context: typer.Context):
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            require_team_jwt(client)
            yield client
    except ApiError as error:
        safe_api_error(context, error)
    except AuthError:
        fail(
            context,
            "authentication_required",
            "Team/account management requires a valid user login.",
            JWT_HINT,
            ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        fail(
            context,
            "invalid_team_response",
            "The team/account response was invalid.",
            UNKNOWN_HINT,
        )


def require_role(context: typer.Context, account: _AccountSelf, roles: tuple[str, ...]):
    if account.your_role not in roles:
        fail(
            context,
            "team_role_required",
            "Your account role cannot perform this action.",
            "Check solwyn whoami and the command's required role.",
            ErrorSemantic.AUTH,
        )


def segment(context: typer.Context, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        fail(
            context,
            "invalid_team_id",
            "Provide a valid member or invitation ID.",
            semantic=ErrorSemantic.USAGE,
        )
    return quote(value, safe="")


def empty_204(response: httpx.Response):
    if response.status_code != 204 or response.content:
        raise ValueError("Expected empty 204")


def write_request(context: typer.Context, client: SolwynClient, method, path, **kwargs):
    try:
        return client.request(method, path, **kwargs)
    except ApiError as error:
        safe_api_error(context, error, submitted=True)


def emit(context, schema, data):
    if isinstance(data, BaseModel):
        values = data.model_dump(mode="json")
    else:
        values = data
    output_engine(context).write_success(
        schema=schema,
        data=data,
        table=TableData(
            columns=("Field", "Value"),
            rows=tuple(
                (k, str(v) if v is not None else "none") for k, v in values.items()
            ),
            fold_cells=True,
        ),
    )


def command_help(endpoint, schema, auth="JWT; account role enforced by Core"):
    return help_contract(
        endpoint=endpoint,
        auth=auth,
        exits="0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
        schema=schema,
        guidance=(
            "Writes show effects and require interactive confirmation or --yes. No automatic replay of unknown write outcomes.",
        ),
    )
