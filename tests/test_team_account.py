"""Customer team/account contracts; all HTTP and credentials are synthetic."""

from copy import deepcopy
import json
import httpx
from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import ApiError
from solwyn_cli.commands import _team, invite

import pytest
from typer.testing import CliRunner
from solwyn_cli.main import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["team", "members", "list"],
        ["team", "invites", "list"],
        ["invite", "show"],
        ["invite", "accept"],
        ["account", "rename"],
    ],
)
def test_team_help_is_available_without_auth(args):
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    assert "JSON schema:" in " ".join(result.output.split())


JWT = auth.JwtCredential(
    access_token="synthetic-access", refresh_token="synthetic-refresh"
)
ACCOUNT = {
    "id": "acct_one",
    "display_name": "Team One",
    "subscription_tier": "team",
    "alert_email_override": "alerts@example.invalid",
    "seat_limit": 5,
    "seats_used": 2,
    "your_role": "owner",
}
MEMBER = {
    "user_id": "user_two",
    "email": "member@example.invalid",
    "name": None,
    "role": "member",
    "joined_at": "2026-10-14T00:00:00Z",
}
INVITATION = {
    "id": "inv_one",
    "invited_email": "invited@example.invalid",
    "invited_role": "member",
    "expires_at": "2026-10-21T00:00:00Z",
    "delivery_status": "send_failed",
}
PREVIEW = {
    "account_name": "Team One",
    "inviter_email": None,
    "role": "member",
    "expires_at": "2026-10-21T00:00:00Z",
}
IDENTITY = {
    "email_verified": True,
    "first_name": "Owner",
    "marketing_emails_subscribed": False,
    "account_id": "acct_one",
    "alert_email_override": "alerts@example.invalid",
    "user_id": "user_one",
    "email": "owner@example.invalid",
    "display_name": "Owner",
    "subscription_tier": "team",
}
TOKEN = "synthetic_invitation_secret"
TOKENS = {
    "access_token": "synthetic-accepted-access",
    "refresh_token": "synthetic-accepted-refresh",
    "token_type": "bearer",
}


@pytest.fixture
def mock_team(monkeypatch):
    queue, calls, stores, sleeps = [], [], [], []

    def handler(request):
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def factory(**kwargs):
        return SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=sleeps.append
        )

    monkeypatch.setattr(_team, "SolwynClient", factory)
    monkeypatch.setattr(invite, "SolwynClient", factory)
    monkeypatch.setattr(_team, "resolve_credential", lambda: JWT)
    monkeypatch.setattr(invite, "resolve_credential", lambda: JWT)
    monkeypatch.setattr(_team, "is_interactive", lambda: False)
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(invite, "store_jwt_pair", lambda a, r: stores.append((a, r)))
    return queue, calls, stores, sleeps


def invoke(*args, input=None):
    return runner.invoke(app, ["--json", *args], input=input)


def ok(value):
    return httpx.Response(200, json=value)


def assert_private(result):
    for secret in (
        TOKEN,
        *TOKENS.values(),
        "synthetic-access",
        "synthetic-refresh",
        "private-password-canary",
    ):
        if secret != "bearer":
            assert secret not in result.output + result.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["team", "members", "set-role", "user_two", "--role", "admin"],
        ["team", "members", "remove", "user_two"],
        ["team", "leave"],
        ["team", "invites", "send", "a@example.invalid"],
        ["team", "invites", "resend", "inv_one"],
        ["team", "invites", "revoke", "inv_one"],
        ["account", "rename", "New name"],
        ["invite", "accept"],
        ["invite", "accept", "--new-user"],
    ],
)
def test_non_tty_write_refuses_before_credentials_http_or_token(
    mock_team, monkeypatch, command
):
    def forbidden():
        raise AssertionError("credential read before confirmation preflight")

    monkeypatch.setattr(_team, "resolve_credential", forbidden)
    monkeypatch.setattr(invite, "resolve_credential", forbidden)
    result = invoke(*command, input=TOKEN + "\n")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "confirmation_required"
    assert mock_team[1] == []
    assert_private(result)


@pytest.mark.parametrize(
    "kind,payload,key",
    [("members", [MEMBER], "members"), ("invites", [INVITATION], "invitations")],
)
def test_list_full_typed_array_no_project_and_verbatim_delivery(
    mock_team, kind, payload, key
):
    queue, calls, _, _ = mock_team
    queue.append(ok(payload))
    result = invoke("team", kind, "list")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[key][0] == {
        **payload[0],
        ("joined_at" if kind == "members" else "expires_at"): (
            "2026-10-14T00:00:00Z" if kind == "members" else "2026-10-21T00:00:00Z"
        ),
    }
    assert len(calls) == 1


@pytest.mark.parametrize("kind,payload", [("members", MEMBER), ("invites", INVITATION)])
@pytest.mark.parametrize("mutation", ["missing", "unknown", "null", "invalid_role"])
def test_list_fail_closed(mock_team, kind, payload, mutation):
    item = deepcopy(payload)
    if mutation == "missing":
        item.pop("name" if kind == "members" else "delivery_status")
    if mutation == "unknown":
        item["future_private"] = "PRIVATE"
    if mutation == "null":
        item["user_id" if kind == "members" else "id"] = None
    if mutation == "invalid_role":
        item["role" if kind == "members" else "invited_role"] = "operator"
    mock_team[0].append(ok([item]))
    result = invoke("team", kind, "list")
    assert result.exit_code == 1
    assert "PRIVATE" not in result.output


def test_set_role_precise_body_and_effects(mock_team):
    queue, calls, _, _ = mock_team
    queue.extend([ok(ACCOUNT), ok([MEMBER]), ok({**MEMBER, "role": "admin"})])
    result = invoke(
        "team", "members", "set-role", "user_two", "--role", "admin", "--yes"
    )
    assert result.exit_code == 0, result.output
    assert calls[-1].method == "PATCH" and json.loads(calls[-1].content) == {
        "role": "admin"
    }
    assert "invalidates existing access tokens" in result.stderr
    assert "acct_one" in result.stderr and MEMBER["email"] in result.stderr
    assert json.loads(result.stdout)["role"] == "admin"


@pytest.mark.parametrize(
    "args",
    [
        ["team", "members", "set-role", "user_two", "--role", "owner"],
        ["team", "members", "set-role", "user_two", "--role", "operator"],
    ],
)
def test_owner_role_not_a_local_choice(mock_team, args):
    assert invoke(*args, "--yes").exit_code == 2
    assert not mock_team[1]


@pytest.mark.parametrize("action", ["set-role", "send", "rename"])
def test_local_role_gate(mock_team, action):
    mock_team[0].append(ok({**ACCOUNT, "your_role": "member"}))
    args = {
        "set-role": ["team", "members", "set-role", "user_two", "--role", "admin"],
        "send": ["team", "invites", "send", "a@example.invalid"],
        "rename": ["account", "rename", "New"],
    }[action]
    result = invoke(*args, "--yes")
    assert result.exit_code == 3 and len(mock_team[1]) == 1


def test_owner_target_cannot_change(mock_team):
    mock_team[0].extend([ok(ACCOUNT), ok([{**MEMBER, "role": "owner"}])])
    result = invoke(
        "team", "members", "set-role", "user_two", "--role", "admin", "--yes"
    )
    assert result.exit_code == 1
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"]
        == "owner_change_not_supported"
    )
    assert len(mock_team[1]) == 2


@pytest.mark.parametrize("self_leave", [False, True])
def test_removal_revocation_and_honest_self_leave(mock_team, self_leave):
    queue, calls, _, _ = mock_team
    user = "user_one" if self_leave else "user_two"
    queue.extend(
        [
            ok(ACCOUNT),
            ok(IDENTITY),
            ok([{**MEMBER, "user_id": user}]),
            httpx.Response(204),
        ]
    )
    args = ["team", "leave"] if self_leave else ["team", "members", "remove", user]
    result = invoke(*args, "--yes")
    assert result.exit_code == 0, result.output
    assert calls[-1].url.path.endswith("/members/" + user)
    assert calls[-1].method == "DELETE"
    assert "sessions AND project keys they created" in result.stderr
    assert json.loads(result.stdout)["self_leave"] == self_leave
    if self_leave:
        assert "Log in again" in result.output
    assert len(calls) == 4


@pytest.mark.parametrize("status,content", [(200, b""), (204, b"{}")])
def test_removal_requires_exact_empty_204(mock_team, status, content):
    mock_team[0].extend(
        [
            ok(ACCOUNT),
            ok(IDENTITY),
            ok([MEMBER]),
            httpx.Response(status, content=content),
        ]
    )
    result = invoke("team", "members", "remove", "user_two", "--yes")
    assert result.exit_code == 1
    assert (
        "outcome" in json.loads(result.stderr.splitlines()[-1])["hint"]
        or "completed" in result.output
    )


def test_last_owner_conflict_guidance_no_transfer(mock_team):
    mock_team[0].extend(
        [
            ok(ACCOUNT),
            ok(IDENTITY),
            ok([{**MEMBER, "user_id": "user_one", "role": "owner"}]),
            httpx.Response(409, json={"detail": "last_owner_cannot_leave"}),
        ]
    )
    result = invoke("team", "leave", "--yes")
    assert result.exit_code == 1
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"] == "last_owner_cannot_leave"
    )
    assert "cannot transfer ownership" in result.output
    assert len(mock_team[1]) == 4


@pytest.mark.parametrize(
    "actor,target",
    [("admin", "admin"), ("admin", "owner"), ("member", "member"), ("owner", "owner")],
)
def test_remove_role_policy(mock_team, actor, target):
    mock_team[0].extend(
        [
            ok({**ACCOUNT, "your_role": actor}),
            ok(IDENTITY),
            ok([{**MEMBER, "role": target}]),
        ]
    )
    result = invoke("team", "members", "remove", "user_two", "--yes")
    assert result.exit_code == 3 and len(mock_team[1]) == 3


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_any_role_can_self_leave(mock_team, role):
    mock_team[0].extend(
        [
            ok({**ACCOUNT, "your_role": role}),
            ok(IDENTITY),
            ok([{**MEMBER, "user_id": "user_one", "role": role}]),
            httpx.Response(204),
        ]
    )
    assert invoke("team", "leave", "--yes").exit_code == 0


def test_send_keeps_actual_delivery_status_without_claim(mock_team):
    mock_team[0].extend([ok(ACCOUNT), ok(INVITATION)])
    result = invoke("team", "invites", "send", "invited@example.invalid", "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(mock_team[1][-1].content) == {
        "email": "invited@example.invalid",
        "role": "member",
    }
    assert json.loads(result.stdout)["delivery_status"] == "send_failed"
    assert "does not prove" in result.stderr


@pytest.mark.parametrize("action", ["resend", "revoke"])
def test_invitation_action_empty_204_and_captured_state(mock_team, action):
    mock_team[0].extend([ok(ACCOUNT), ok([INVITATION]), httpx.Response(204)])
    result = invoke("team", "invites", action, "inv_one", "--yes")
    assert result.exit_code == 0, result.output
    assert len(mock_team[1]) == 3
    assert (
        INVITATION["invited_email"] in result.stderr and "send_failed" in result.stderr
    )
    data = json.loads(result.stdout)
    assert "delivery_status" not in data
    assert data["completed"] is True


def test_rename_preserves_override_exact_body(mock_team):
    mock_team[0].extend([ok(ACCOUNT), ok({**ACCOUNT, "display_name": "New Team"})])
    result = invoke("account", "rename", "New Team", "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(mock_team[1][-1].content) == {"display_name": "New Team"}
    assert (
        json.loads(result.stdout)["alert_email_override"]
        == ACCOUNT["alert_email_override"]
    )
    assert "Team One" in result.stderr and "New Team" in result.stderr


@pytest.mark.parametrize("answer", ["n\n", "", "\x03"])
def test_decline_eof_interrupt_never_write(mock_team, monkeypatch, answer):
    monkeypatch.setattr(_team, "is_interactive", lambda: True)
    mock_team[0].extend([ok(ACCOUNT), ok([MEMBER])])
    result = invoke(
        "team", "members", "set-role", "user_two", "--role", "admin", input=answer
    )
    assert result.exit_code == 2, result.output
    assert len(mock_team[1]) == 2
    assert all(c.method == "GET" for c in mock_team[1])
    assert "Account Team One" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["team", "members", "list"],
        ["team", "invites", "list"],
        ["account", "rename", "New", "--yes"],
        ["invite", "accept", "--yes"],
    ],
)
def test_selected_project_key_never_bypassed(mock_team, monkeypatch, command):
    key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "ab" * 32, source="environment"
    )
    monkeypatch.setattr(_team, "resolve_credential", lambda: key)
    monkeypatch.setattr(invite, "resolve_credential", lambda: key)
    result = invoke(*command, input=TOKEN + "\n")
    assert result.exit_code == 3
    assert not mock_team[1]
    assert_private(result)


def test_public_preview_never_resolves_or_sends_auth(mock_team, monkeypatch):
    monkeypatch.setattr(
        invite,
        "resolve_credential",
        lambda: (_ for _ in ()).throw(AssertionError("auth read")),
    )
    mock_team[0].append(ok(PREVIEW))
    result = invoke("invite", "show", input=TOKEN + "\n")
    assert result.exit_code == 0, result.output
    assert "Authorization" not in mock_team[1][0].headers
    assert json.loads(result.stdout) == {"schema": "solwyn.invite.show.v1", **PREVIEW}
    assert_private(result)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a/b",
        "..",
        "a%2Fb",
        "a b",
        "a\nb",
        "a\x00b",
        "a?b",
        "a#b",
        "a\\b",
        "a" * 4097,
        "a\n\n",
    ],
)
def test_token_invalid_fixed_secret_free_error(mock_team, token):
    result = invoke("invite", "show", input=token)
    assert result.exit_code == 2, result.output
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"] == "invalid_invitation_token"
    )
    assert not mock_team[1]


def test_signed_in_accept_empty_object_and_one_pair_store(mock_team):
    mock_team[0].extend([ok(PREVIEW), ok(TOKENS)])
    result = invoke("invite", "accept", "--yes", input=TOKEN + "\n")
    assert result.exit_code == 0, result.output
    assert json.loads(mock_team[1][-1].content) == {}
    assert mock_team[1][-1].headers["Authorization"] == "Bearer synthetic-access"
    assert "Authorization" not in mock_team[1][0].headers
    assert mock_team[2] == [(TOKENS["access_token"], TOKENS["refresh_token"])]
    assert_private(result)


def test_new_user_explicit_interactive_payload_no_authorization(mock_team, monkeypatch):
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "resolve_credential", lambda: None)
    answers = iter(
        [TOKEN, "invited@example.invalid", "Invited Name", "private-password-canary"]
    )
    monkeypatch.setattr(invite, "_prompt", lambda *args, **kwargs: next(answers))
    mock_team[0].extend([ok(PREVIEW), ok(TOKENS)])
    result = invoke("invite", "accept", "--new-user", "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(mock_team[1][-1].content) == {
        "email": "invited@example.invalid",
        "display_name": "Invited Name",
        "password": "private-password-canary",
    }
    assert all("Authorization" not in c.headers for c in mock_team[1])
    assert len(mock_team[2]) == 1
    assert_private(result)


def test_new_user_non_tty_refuses_before_token_or_auth(mock_team, monkeypatch):
    monkeypatch.setattr(
        invite,
        "resolve_credential",
        lambda: (_ for _ in ()).throw(AssertionError("auth read")),
    )
    result = invoke("invite", "accept", "--new-user", "--yes", input=TOKEN)
    assert result.exit_code == 3
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"]
        == "interactive_invitation_auth_required"
    )
    assert not mock_team[1]
    assert_private(result)


@pytest.mark.parametrize(
    "selected",
    [JWT, auth.ProjectKeyCredential(api_key="sk_proj_" + "ab" * 32, source="keyring")],
)
def test_new_user_rejects_selected_identity(mock_team, monkeypatch, selected):
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "resolve_credential", lambda: selected)
    result = invoke("invite", "accept", "--new-user", "--yes")
    assert result.exit_code == 3
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"]
        == "invitation_identity_ambiguous"
    )
    assert not mock_team[1]


@pytest.mark.parametrize(
    "bad",
    [
        {**TOKENS, "extra": "PRIVATE"},
        {"access_token": "PRIVATE"},
        {**TOKENS, "token_type": "PRIVATE"},
        {**TOKENS, "refresh_token": ""},
    ],
)
def test_accept_malformed_success_no_store_or_replay(mock_team, bad):
    mock_team[0].extend([ok(PREVIEW), ok(bad)])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 1
    assert "may already have completed" in result.output
    assert len(mock_team[1]) == 2 and not mock_team[2]
    assert "PRIVATE" not in result.output
    assert_private(result)


def test_accept_storage_failure_truthful_no_replay(mock_team, monkeypatch):
    def broken(*args):
        raise auth.CredentialStorageError("PRIVATE storage body")

    monkeypatch.setattr(invite, "store_jwt_pair", broken)
    mock_team[0].extend([ok(PREVIEW), ok(TOKENS)])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 3
    assert "acceptance completed" in result.output
    assert "PRIVATE" not in result.output
    assert len(mock_team[1]) == 2
    assert_private(result)


@pytest.mark.parametrize(
    "retry_after,delay", [("100000", 10), ("-5", 0), ("bad", 0), ("1", 1)]
)
def test_accept_capacity_once_bounded(mock_team, retry_after, delay):
    busy = httpx.Response(
        503,
        json={"detail": {"code": "auth_capacity_exceeded", "message": "PRIVATE"}},
        headers={"Retry-After": retry_after},
    )
    mock_team[0].extend([ok(PREVIEW), busy, ok(TOKENS)])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 0, result.output
    assert len(mock_team[1]) == 3 and mock_team[3] == [delay]
    assert len(mock_team[2]) == 1
    assert_private(result)


def test_capacity_exhaustion_is_transient_not_auth(mock_team):
    def busy():
        return httpx.Response(
            503,
            json={"detail": {"code": "auth_capacity_exceeded", "message": "PRIVATE"}},
            headers={"Retry-After": "0"},
        )

    mock_team[0].extend([ok(PREVIEW), busy(), busy()])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 1
    assert len(mock_team[1]) == 3 and len(mock_team[3]) == 1
    assert "PRIVATE" not in result.output and not mock_team[2]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503, json={"detail": TOKEN}, headers={"Retry-After": "1"}),
        httpx.ReadTimeout(TOKEN),
    ],
)
def test_accept_unknown_outcome_never_replays_or_leaks(mock_team, failure):
    mock_team[0].extend([ok(PREVIEW), failure])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 1
    assert len(mock_team[1]) == 2 and not mock_team[3]
    assert "may already have completed" in result.output
    assert_private(result)


@pytest.mark.parametrize(
    "args,payloads,name,input_value",
    [
        (["team", "members", "list"], [[MEMBER]], "team-members", None),
        (["team", "invites", "list"], [[INVITATION]], "team-invites", None),
        (
            ["team", "members", "set-role", "user_two", "--role", "admin", "--yes"],
            [ACCOUNT, [MEMBER], {**MEMBER, "role": "admin"}],
            "team-role",
            None,
        ),
        (
            ["team", "members", "remove", "user_two", "--yes"],
            [ACCOUNT, IDENTITY, [MEMBER], None],
            "team-remove",
            None,
        ),
        (
            ["team", "leave", "--yes"],
            [ACCOUNT, IDENTITY, [{**MEMBER, "user_id": "user_one"}], None],
            "team-leave",
            None,
        ),
        (
            ["team", "invites", "send", "invited@example.invalid", "--yes"],
            [ACCOUNT, INVITATION],
            "team-invite-send",
            None,
        ),
        (
            ["team", "invites", "resend", "inv_one", "--yes"],
            [ACCOUNT, [INVITATION], None],
            "team-invite-resend",
            None,
        ),
        (
            ["team", "invites", "revoke", "inv_one", "--yes"],
            [ACCOUNT, [INVITATION], None],
            "team-invite-revoke",
            None,
        ),
        (
            ["account", "rename", "New Team", "--yes"],
            [ACCOUNT, {**ACCOUNT, "display_name": "New Team"}],
            "account-rename",
            None,
        ),
        (["invite", "show"], [PREVIEW], "invite-show", TOKEN),
        (["invite", "accept", "--yes"], [PREVIEW, TOKENS], "invite-accept", TOKEN),
    ],
)
@pytest.mark.parametrize("json_output", [True, False])
def test_team_command_golden(mock_team, args, payloads, name, input_value, json_output):
    from pathlib import Path
    from golden.harness import assert_matches_golden

    mock_team[0].extend(
        httpx.Response(204) if payload is None else ok(payload) for payload in payloads
    )
    result = runner.invoke(
        app,
        (["--json"] if json_output else ["--no-color"]) + args,
        input=input_value,
        terminal_width=120,
    )
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout,
        Path(__file__).parent
        / "golden"
        / (name + (".json" if json_output else "-table.txt")),
    )
    assert_private(result)


@pytest.mark.parametrize("args", [["invite", "show"], ["invite", "accept", "--yes"]])
def test_invitation_debug_diagnostics_never_log_secret_route(mock_team, caplog, args):
    import logging

    caplog.set_level(logging.DEBUG)
    mock_team[0].extend([ok(PREVIEW), ok(TOKENS)])
    result = invoke(*args, input=TOKEN)
    assert result.exit_code == 0, result.output
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(
        [(record.msg, record.args, record.exc_info) for record in caplog.records]
    )


@pytest.mark.parametrize(
    "args",
    [
        ["invite", "show", TOKEN],
        ["invite", "accept", TOKEN],
        ["invite", TOKEN],
        ["invite", "accept", "--password", "private-password-canary"],
    ],
)
def test_invitation_rejected_argv_never_echoes_secrets(mock_team, args):
    result = invoke(*args)
    assert result.exit_code == 2
    assert_private(result)


@pytest.mark.parametrize(
    "field", ["account_name", "inviter_email", "role", "expires_at"]
)
@pytest.mark.parametrize("change", ["missing", "unknown", "wrong_type"])
def test_preview_fail_closed_required_nullable_fields(mock_team, field, change):
    body = PREVIEW.copy()
    if change == "missing":
        body.pop(field)
    elif change == "unknown":
        body["unexpected"] = "PRIVATE"
    else:
        body[field] = ["PRIVATE"]
    mock_team[0].append(ok(body))
    result = invoke("invite", "show", input=TOKEN)
    assert result.exit_code == 1
    assert "PRIVATE" not in result.output
    assert_private(result)


@pytest.mark.parametrize("action", ["resend", "revoke"])
@pytest.mark.parametrize("status,content", [(200, b""), (204, b"{}")])
def test_invitation_actions_require_exact_empty_204(mock_team, action, status, content):
    mock_team[0].extend(
        [ok(ACCOUNT), ok([INVITATION]), httpx.Response(status, content=content)]
    )
    result = invoke("team", "invites", action, "inv_one", "--yes")
    assert result.exit_code == 1
    assert len(mock_team[1]) == 3


@pytest.mark.parametrize(
    "command",
    [
        ["team", "invites", "send", "a@example.invalid"],
        ["team", "invites", "resend", "inv_one"],
    ],
)
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(
            503,
            json={"detail": {"code": "PRIVATE", "message": "PRIVATE"}},
            headers={"Retry-After": "9"},
        ),
        httpx.ReadTimeout("PRIVATE"),
    ],
)
def test_email_write_failure_no_replay_unknown_outcome(mock_team, command, failure):
    mock_team[0].append(ok(ACCOUNT))
    if command[2] == "resend":
        mock_team[0].append(ok([INVITATION]))
    mock_team[0].append(failure)
    result = invoke(*command, "--yes")
    assert result.exit_code == 1
    assert "may already have completed" in result.output
    assert "PRIVATE" not in result.output
    assert mock_team[3] == []
    assert len([c for c in mock_team[1] if c.method == "POST"]) == 1


@pytest.mark.parametrize("answer", ["n\n", "", "\x03"])
def test_invitation_accept_decline_eof_interrupt_never_accepts(
    mock_team, monkeypatch, answer
):
    monkeypatch.setattr(_team, "is_interactive", lambda: True)
    mock_team[0].append(ok(PREVIEW))
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "_prompt", lambda *args, **kwargs: TOKEN)
    result = invoke("invite", "accept", input=answer)
    assert result.exit_code == 2
    assert len(mock_team[1]) == 1 and not mock_team[2]
    assert all(c.method == "GET" for c in mock_team[1])
    assert_private(result)


@pytest.mark.parametrize("secret_length", [7, 129])
def test_new_user_password_bounds_fixed_errors_no_accept(
    mock_team, monkeypatch, secret_length
):
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "resolve_credential", lambda: None)
    answers = iter([TOKEN, "invited@example.invalid", "Name", "x" * secret_length])
    monkeypatch.setattr(invite, "_prompt", lambda *args, **kwargs: next(answers))
    mock_team[0].append(ok(PREVIEW))
    result = invoke("invite", "accept", "--new-user", "--yes")
    assert result.exit_code == 2
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"]
        == "invalid_invitation_credentials"
    )
    assert len(mock_team[1]) == 1 and not mock_team[2]


def test_accept_token_type_default_matches_core(mock_team):
    mock_team[0].extend(
        [ok(PREVIEW), ok({k: v for k, v in TOKENS.items() if k != "token_type"})]
    )
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 0
    assert len(mock_team[2]) == 1
    assert_private(result)


@pytest.mark.parametrize(
    "status,detail,expected",
    [
        (403, "forbidden", "team_access_denied"),
        (409, "user_already_has_account", "user_already_has_account"),
        (409, "user_already_exists", "user_already_exists"),
        (410, "PRIVATE", "invitation_gone"),
        (
            422,
            [{"input": "private-password-canary", "msg": "PRIVATE"}],
            "team_request_failed",
        ),
    ],
)
def test_accept_fixed_conflict_authorization_validation_errors(
    mock_team, status, detail, expected
):
    mock_team[0].extend([ok(PREVIEW), httpx.Response(status, json={"detail": detail})])
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == (3 if status == 403 else 1)
    assert json.loads(result.stderr.splitlines()[-1])["code"] == expected
    assert len(mock_team[1]) == 2 and not mock_team[2]
    assert "PRIVATE" not in result.output
    assert_private(result)


@pytest.mark.parametrize(
    "path", ["../bad", "bad/name", "bad%2fpath", "bad?query", "bad#fragment", "a\x00b"]
)
@pytest.mark.parametrize(
    "command", [["team", "members", "remove"], ["team", "invites", "resend"]]
)
def test_team_path_ids_invalid_before_auth_or_http(mock_team, command, path):
    result = invoke(*command, path, "--yes")
    assert result.exit_code == 2
    assert not mock_team[1]


def test_list_preserves_core_order_and_open_invite_statuses(mock_team):
    payloads = [
        {**INVITATION, "id": "inv_newest", "delivery_status": "future_delivery_state"},
        {**INVITATION, "id": "inv_old", "expires_at": "2025-01-01T00:00:00Z"},
    ]
    mock_team[0].append(ok(payloads))
    result = invoke("team", "invites", "list")
    assert result.exit_code == 0
    assert [i["id"] for i in json.loads(result.stdout)["invitations"]] == [
        "inv_newest",
        "inv_old",
    ]
    assert (
        json.loads(result.stdout)["invitations"][0]["delivery_status"]
        == "future_delivery_state"
    )


@pytest.mark.parametrize("name", ["", "a" * 121])
def test_rename_name_bounds_before_http(mock_team, name):
    result = invoke("account", "rename", name, "--yes")
    assert result.exit_code == 2
    assert not mock_team[1]


def test_team_effects_strip_terminal_controls_and_markup_is_literal(mock_team):
    mock_team[0].extend(
        [
            ok({**ACCOUNT, "display_name": "[red]Team\x1b[2J\nOne"}),
            ok([MEMBER]),
            ok({**MEMBER, "role": "admin"}),
        ]
    )
    result = invoke(
        "team", "members", "set-role", "user_two", "--role", "admin", "--yes"
    )
    assert result.exit_code == 0
    assert "\x1b" not in result.stderr and "[red]" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/accounts/me/invitations",
        "/api/v1/accounts/me/invitations/inv_one/resend",
        "/api/v1/invitations/synthetic_invitation_secret/other",
        "/api/v1/invitations/other/accept/more",
    ],
)
def test_invitation_capacity_retry_is_exact_route_only(path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            503,
            json={"detail": {"code": "auth_capacity_exceeded", "message": "PRIVATE"}},
            headers={"Retry-After": "0"},
        )

    with SolwynClient(
        api_url="https://api.example.invalid",
        credential=None,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        with pytest.raises(ApiError) as captured:
            client.request_unauthenticated("POST", path, json={})
    assert len(calls) == 1
    assert getattr(captured.value, "status_code", None) == 503


def test_accept_401_refresh_keeps_selected_user_and_empty_body(mock_team, monkeypatch):
    refresh_stores = []

    def store_refresh(access, refresh):
        refresh_stores.append((access, refresh))
        return auth.JwtCredential(access_token=access, refresh_token=refresh)

    monkeypatch.setattr(auth, "store_jwt_pair", store_refresh)
    rotated = {
        "access_token": "synthetic-rotated-access",
        "refresh_token": "synthetic-rotated-refresh",
        "token_type": "bearer",
    }
    mock_team[0].extend(
        [
            ok(PREVIEW),
            httpx.Response(401, json={"detail": "Invalid or expired token"}),
            ok(rotated),
            ok(TOKENS),
        ]
    )
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 0, result.output
    calls = mock_team[1]
    assert [c.method for c in calls] == ["GET", "POST", "POST", "POST"]
    assert calls[2].url.path == "/api/v1/auth/refresh"
    assert json.loads(calls[1].content) == json.loads(calls[3].content) == {}
    assert calls[3].headers["Authorization"] == "Bearer synthetic-rotated-access"
    assert len(refresh_stores) == 1 and len(mock_team[2]) == 1
    assert_private(result)


def test_accept_refresh_failure_never_falls_back_or_creates_user(mock_team):
    mock_team[0].extend(
        [
            ok(PREVIEW),
            httpx.Response(401, json={"detail": "PRIVATE"}),
            httpx.Response(401, json={"detail": "PRIVATE"}),
        ]
    )
    result = invoke("invite", "accept", "--yes", input=TOKEN)
    assert result.exit_code == 3
    assert len(mock_team[1]) == 3
    assert len([c for c in mock_team[1] if c.url.path.endswith("/accept")]) == 1
    assert not mock_team[2] and "PRIVATE" not in result.output
    assert_private(result)


def test_new_user_actual_hidden_prompts_keep_stdout_one_result(mock_team, monkeypatch):
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "resolve_credential", lambda: None)
    mock_team[0].extend([ok(PREVIEW), ok(TOKENS)])
    result = invoke(
        "invite",
        "accept",
        "--new-user",
        "--yes",
        input=TOKEN
        + "\ninvited@example.invalid\nInvited Name\nprivate-password-canary\n",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["accepted"] is True
    assert len(result.stdout.splitlines()) == 1
    assert "Invitation token" in result.stderr and "Password" in result.stderr
    assert_private(result)


def test_new_user_capacity_retry_unauthenticated_once(mock_team, monkeypatch):
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(invite, "resolve_credential", lambda: None)
    answers = iter(
        [TOKEN, "invited@example.invalid", "Invited Name", "private-password-canary"]
    )
    monkeypatch.setattr(invite, "_prompt", lambda *args, **kwargs: next(answers))
    mock_team[0].extend(
        [
            ok(PREVIEW),
            httpx.Response(
                503,
                json={
                    "detail": {"code": "auth_capacity_exceeded", "message": "PRIVATE"}
                },
                headers={"Retry-After": "0"},
            ),
            ok(TOKENS),
        ]
    )
    result = invoke("invite", "accept", "--new-user", "--yes")
    assert result.exit_code == 0, result.output
    assert len(mock_team[1]) == 3 and mock_team[3] == [0]
    assert mock_team[1][1].content == mock_team[1][2].content
    assert all("Authorization" not in c.headers for c in mock_team[1])
    assert len(mock_team[2]) == 1
    assert_private(result)


def test_invitation_models_redact_password_and_token_pair_repr():
    request = _team.AcceptInvitationRequest(
        email="invited@example.invalid",
        display_name="Name",
        password="private-password-canary",
    )
    tokens = _team.InvitationTokenResponse.model_validate(TOKENS)
    for value in (
        repr(request),
        request.model_dump_json(),
        repr(tokens),
        tokens.model_dump_json(),
    ):
        for secret in (
            "private-password-canary",
            TOKENS["access_token"],
            TOKENS["refresh_token"],
        ):
            assert secret not in value
