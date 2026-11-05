"""Customer roster, role, and invitation management (writes are CLI-only)."""

from enum import StrEnum
from typing import Annotated

import typer

from solwyn_cli.commands import _team as shared
from solwyn_cli.commands._team import BASE, command_help
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import TableData
from solwyn_cli.runtime import output_engine

app = typer.Typer(
    help="Manage your account team.", epilog=command_help(BASE, "varies by command")
)
members_app = typer.Typer(
    help="List members and manage access.",
    epilog=command_help(BASE + "/members", "solwyn.team.members.v1"),
)
invites_app = typer.Typer(
    help="Manage account invitations.",
    epilog=command_help(BASE + "/invitations", "solwyn.team.invites.v1"),
)
app.add_typer(members_app, name="members")
app.add_typer(invites_app, name="invites")
Yes = Annotated[
    bool,
    typer.Option("--yes", help="Acknowledge the displayed effects without prompting."),
]


class RoleChoice(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


@members_app.command(
    "list",
    epilog=command_help(
        "GET " + BASE + "/members",
        "solwyn.team.members.v1",
        "login; any account member",
    ),
)
def list_members(context: typer.Context):
    """List the full active roster in Core's join order."""
    with shared.team_client(context) as client:
        data = shared.fetch_members(client=client)
    output_engine(context).write_success(
        schema="solwyn.team.members.v1",
        data=data,
        table=TableData(
            columns=("User ID", "Email", "Name", "Role", "Joined"),
            rows=tuple(
                (
                    m.user_id,
                    m.email,
                    m.name if m.name is not None else "none",
                    m.role,
                    m.joined_at.isoformat(),
                )
                for m in data.members
            ),
            fold_cells=True,
        ),
    )


def _target_member(context, client, user_id):
    target = next(
        (
            m
            for m in shared.fetch_members(client=client).members
            if m.user_id == user_id
        ),
        None,
    )
    if target is None:
        shared.fail(
            context,
            "member_not_found",
            "The member was not found in the current account.",
            semantic=ErrorSemantic.NOT_FOUND,
        )
    return target


@members_app.command(
    "set-role",
    epilog=command_help(
        "PATCH " + BASE + "/members/{user_id}", "solwyn.team.member.v1", "owner login"
    ),
)
def set_role(
    context: typer.Context,
    user_id: Annotated[str, typer.Argument()],
    role: Annotated[RoleChoice, typer.Option("--role")],
    yes: Yes = False,
):
    """Set a non-owner's role to admin or member (owner only)."""
    shared.preflight(context, yes)
    user_path = shared.segment(context, user_id)
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        shared.require_role(context, account, ("owner",))
        target = _target_member(context, client, user_id)
        if target.role == "owner":
            shared.fail(
                context,
                "owner_change_not_supported",
                "Owner role changes are unsupported.",
                shared._TEAM_HINTS["owner_change_not_supported"],
            )
        shared.effects(
            f"Account {account.display_name} ({account.id}): change {target.email} ({target.user_id}) from {target.role} to {role.value}. A changed role invalidates existing access tokens; the member must refresh or log in for current permissions. Refresh sessions and API keys are not revoked by a role change."
        )
        shared.confirm(context, yes)
        with shared.write_response(context):
            data = shared.MemberOut.model_validate(
                shared.write_request(
                    context,
                    client,
                    "PATCH",
                    BASE + f"/members/{user_path}",
                    json=shared.RoleUpdateRequest(role=role.value).model_dump(),
                ).json()
            )
            if data.user_id != user_id or data.role != role.value:
                raise ValueError("Role response mismatch")
    shared.emit(context, "solwyn.team.member.v1", data)


def _remove(context, client, account, target, *, self_leave, yes):
    shared.effects(
        f"Account {account.display_name} ({account.id}): remove {target.email} ({target.user_id}, {target.role}){' (your current membership)' if self_leave else ''}. The member's sessions are revoked and existing access tokens are invalidated. API keys belong to the account and keep working.{' You lose current account access. Log in again; use solwyn account recover deliberately if you need a personal account.' if self_leave else ''}"
    )
    shared.confirm(context, yes)
    with shared.write_response(context):
        response = shared.write_request(
            context,
            client,
            "DELETE",
            BASE + "/members/" + shared.segment(context, target.user_id),
        )
        shared.empty_204(response)
    return {
        "account_id": account.id,
        "user_id": target.user_id,
        "removed": True,
        "self_leave": self_leave,
        "hint": "Log in again; use solwyn account recover deliberately if you need a personal account."
        if self_leave
        else None,
    }


@members_app.command(
    "remove",
    epilog=command_help(
        "DELETE " + BASE + "/members/{user_id}",
        "solwyn.team.remove.v1",
        "login; owner/admin for allowed targets, or self",
    ),
)
def remove(
    context: typer.Context, user_id: Annotated[str, typer.Argument()], yes: Yes = False
):
    """Remove a member and revoke their sessions; API keys keep working."""
    shared.preflight(context, yes)
    shared.segment(context, user_id)
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        actor = shared.fetch_current_user(client, account)
        target = _target_member(context, client, user_id)
        self_leave = actor == user_id
        allowed = {"owner": ("admin", "member"), "admin": ("member",), "member": ()}
        if not self_leave and target.role not in allowed[account.your_role]:
            shared.fail(
                context,
                "role_cannot_remove",
                "Your role cannot remove this member.",
                shared._TEAM_HINTS["role_cannot_remove"],
                ErrorSemantic.AUTH,
            )
        data = _remove(context, client, account, target, self_leave=self_leave, yes=yes)
    shared.emit(context, "solwyn.team.remove.v1", data)


@app.command(
    "leave",
    epilog=command_help(
        "DELETE " + BASE + "/members/{user_id}",
        "solwyn.team.remove.v1",
        "login; your own membership",
    ),
)
def leave(context: typer.Context, yes: Yes = False):
    """Leave this account; the last owner cannot leave."""
    shared.preflight(context, yes)
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        user_id = shared.fetch_current_user(client, account)
        target = _target_member(context, client, user_id)
        data = _remove(context, client, account, target, self_leave=True, yes=yes)
    shared.emit(context, "solwyn.team.remove.v1", data)


@invites_app.command(
    "list",
    epilog=command_help(
        "GET " + BASE + "/invitations", "solwyn.team.invites.v1", "owner/admin login"
    ),
)
def list_invites(context: typer.Context):
    """List all open invitations, including expired rows and delivery status."""
    with shared.team_client(context) as client:
        data = shared.fetch_invites(client=client)
    output_engine(context).write_success(
        schema="solwyn.team.invites.v1",
        data=data,
        table=TableData(
            columns=("Invitation ID", "Email", "Role", "Expires", "Delivery status"),
            rows=tuple(
                (
                    i.id,
                    i.invited_email,
                    i.invited_role,
                    i.expires_at.isoformat(),
                    i.delivery_status,
                )
                for i in data.invitations
            ),
            fold_cells=True,
        ),
    )


@invites_app.command(
    "send",
    epilog=command_help(
        "POST " + BASE + "/invitations",
        "solwyn.team.invitation.v1",
        "owner/admin login",
    ),
)
def send(
    context: typer.Context,
    email: Annotated[str, typer.Argument()],
    role: Annotated[RoleChoice, typer.Option("--role")] = RoleChoice.MEMBER,
    yes: Yes = False,
):
    """Request an invitation; inspect returned status, not HTTP delivery claims."""
    shared.preflight(context, yes)
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        shared.require_role(context, account, ("owner", "admin"))
        shared.effects(
            f"Account {account.display_name} ({account.id}): request an invitation for {email} as {role.value}. Core may create an invitation and attempt email, or return existing/member state. A successful response does not prove that a new email was sent; inspect delivery_status."
        )
        shared.confirm(context, yes)
        with shared.write_response(context):
            data = shared.InvitationOut.model_validate(
                shared.write_request(
                    context,
                    client,
                    "POST",
                    BASE + "/invitations",
                    json=shared.InviteRequest(
                        email=email, role=role.value
                    ).model_dump(),
                ).json()
            )
    shared.emit(
        context,
        "solwyn.team.invitation.v1",
        {
            **data.model_dump(mode="json"),
            "hint": "This response does not prove that a new invitation email was sent. Inspect solwyn team invites list for delivery_status.",
        },
    )


def _invitation_action(context, invitation_id, yes, *, resend):
    shared.preflight(context, yes)
    target_path = shared.segment(context, invitation_id)
    with shared.team_client(context) as client:
        account = shared.fetch_account(client)
        shared.require_role(context, account, ("owner", "admin"))
        target = next(
            (
                i
                for i in shared.fetch_invites(client=client).invitations
                if i.id == invitation_id
            ),
            None,
        )
        if target is None:
            shared.fail(
                context,
                "invitation_not_found",
                "The invitation was not found in this account.",
                semantic=ErrorSemantic.NOT_FOUND,
            )
        action = "resend" if resend else "revoke"
        shared.effects(
            f"Account {account.display_name} ({account.id}): {action} invitation {target.id} for {target.invited_email}, role {target.invited_role}, expires {target.expires_at.isoformat()}, captured delivery_status {target.delivery_status}. "
            + (
                "This attempts another email; empty 204 does not prove delivery. Inspect team invites list afterward."
                if resend
                else "The invitation token will no longer permit joining this account."
            )
        )
        shared.confirm(context, yes)
        with shared.write_response(context):
            response = shared.write_request(
                context,
                client,
                "POST" if resend else "DELETE",
                BASE + f"/invitations/{target_path}" + ("/resend" if resend else ""),
            )
            shared.empty_204(response)
    shared.emit(
        context,
        "solwyn.team.invitation-action.v1",
        {
            "account_id": account.id,
            "invitation_id": invitation_id,
            "action": action,
            "completed": True,
            "hint": "Inspect solwyn team invites list for delivery_status; this response does not establish email delivery."
            if resend
            else None,
        },
    )


@invites_app.command(
    "resend",
    epilog=command_help(
        "POST " + BASE + "/invitations/{invitation_id}/resend",
        "solwyn.team.invitation-action.v1",
        "owner/admin login",
    ),
)
def resend(
    context: typer.Context,
    invitation_id: Annotated[str, typer.Argument()],
    yes: Yes = False,
):
    """Attempt to resend an open invitation; delivery status requires a read."""
    _invitation_action(context, invitation_id, yes, resend=True)


@invites_app.command(
    "revoke",
    epilog=command_help(
        "DELETE " + BASE + "/invitations/{invitation_id}",
        "solwyn.team.invitation-action.v1",
        "owner/admin login",
    ),
)
def revoke(
    context: typer.Context,
    invitation_id: Annotated[str, typer.Argument()],
    yes: Yes = False,
):
    """Revoke an invitation so it cannot be accepted."""
    _invitation_action(context, invitation_id, yes, resend=False)
