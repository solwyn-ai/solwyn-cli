"""Customer team/account contracts; all HTTP and credentials are synthetic."""

from copy import deepcopy
import json
import httpx
from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import APPROVAL_USED_HINT, ApiError
from solwyn_cli.commands import _team, invite

import pytest
from typer.testing import CliRunner
from solwyn_cli.main import app
from login_fixtures import (
    APPROVAL_ID,
    APPROVAL_URL,
    approval_body,
    approval_invalid,
    approval_required,
    fake_keychain,
    login_credential,
)

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


JWT = login_credential("synthetic-access")
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
    monkeypatch.setattr(_team, "resolve_credential", lambda **_: JWT)
    monkeypatch.setattr(_team, "is_interactive", lambda: False)
    monkeypatch.setattr(invite, "_stdin_is_tty", lambda: False)
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
    ],
)
def test_non_tty_write_refuses_before_credentials_http_or_token(
    mock_team, monkeypatch, command
):
    def forbidden(**_: object):
        raise AssertionError("credential read before confirmation preflight")

    monkeypatch.setattr(_team, "resolve_credential", forbidden)
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
    ],
)
def test_selected_project_key_never_bypassed(mock_team, monkeypatch, command):
    key = auth.ProjectKeyCredential(
        api_key="sk_proj_" + "ab" * 32, source="environment"
    )
    monkeypatch.setattr(_team, "resolve_credential", lambda **_: key)
    result = invoke(*command, input=TOKEN + "\n")
    assert result.exit_code == 3
    assert not mock_team[1]
    assert_private(result)


def test_public_preview_never_resolves_or_sends_auth(mock_team, monkeypatch):
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda **_: (_ for _ in ()).throw(AssertionError("auth read")),
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


@pytest.mark.parametrize("args", [["invite", "show"]])
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


@pytest.mark.parametrize(
    "command,payloads",
    [
        (["team", "members", "list"], [[{**MEMBER, "unknown": "PRIVATE"}]]),
        (["team", "invites", "list"], [[{**INVITATION, "unknown": "PRIVATE"}]]),
        (
            ["account", "rename", "New Team", "--yes"],
            [{**ACCOUNT, "unknown": "PRIVATE"}],
        ),
        (["team", "leave", "--yes"], [ACCOUNT, {**IDENTITY, "unknown": "PRIVATE"}]),
        (
            ["team", "members", "set-role", "user_two", "--role", "admin", "--yes"],
            [ACCOUNT, [{**MEMBER, "unknown": "PRIVATE"}]],
        ),
        (
            ["team", "invites", "resend", "inv_one", "--yes"],
            [ACCOUNT, [{**INVITATION, "unknown": "PRIVATE"}]],
        ),
    ],
)
def test_malformed_reads_and_prewrite_never_claim_submission(
    mock_team, command, payloads
):
    mock_team[0].extend(ok(p) for p in payloads)
    result = invoke(*command)
    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_team_response"
    assert "No write was submitted" in error["hint"]
    assert "may already have completed" not in result.stderr
    assert all(c.method == "GET" for c in mock_team[1])
    assert "PRIVATE" not in result.output


@pytest.mark.parametrize(
    "command,payloads",
    [
        (
            ["account", "rename", "New Team", "--yes"],
            [ACCOUNT, {**ACCOUNT, "unknown": "PRIVATE"}],
        ),
        (
            ["team", "members", "set-role", "user_two", "--role", "admin", "--yes"],
            [ACCOUNT, [MEMBER], {**MEMBER, "unknown": "PRIVATE"}],
        ),
        (
            ["team", "invites", "send", "invited@example.invalid", "--yes"],
            [ACCOUNT, {**INVITATION, "unknown": "PRIVATE"}],
        ),
    ],
)
def test_malformed_postwrite_keeps_unknown_outcome_guidance(
    mock_team, command, payloads
):
    mock_team[0].extend(ok(p) for p in payloads)
    result = invoke(*command)
    assert result.exit_code == 1
    assert "The write may already have completed" in result.stderr
    assert len([c for c in mock_team[1] if c.method != "GET"]) == 1
    assert "PRIVATE" not in result.output


@pytest.mark.parametrize("command", [["invite", "show"]])
def test_preview_error_has_private_invitation_wording(mock_team, command):
    mock_team[0].append(httpx.Response(410, json={"detail": "PRIVATE"}))
    result = invoke(*command, input=TOKEN)
    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "invitation_gone"
    assert error["message"] == "The invitation preview request failed."
    assert all(
        c.method == "GET" and "Authorization" not in c.headers for c in mock_team[1]
    )
    assert "PRIVATE" not in result.output
    assert_private(result)


@pytest.mark.parametrize("status", ["sent", "send_failed"])
def test_send_json_always_has_fixed_unproven_delivery_hint(mock_team, status):
    mock_team[0].extend([ok(ACCOUNT), ok({**INVITATION, "delivery_status": status})])
    result = invoke("team", "invites", "send", "invited@example.invalid", "--yes")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["delivery_status"] == status
    assert (
        data["hint"]
        == "This response does not prove that a new invitation email was sent. Inspect solwyn team invites list for delivery_status."
    )


def test_member_nullable_name_table_uses_none(mock_team):
    mock_team[0].append(ok([MEMBER]))
    result = runner.invoke(app, ["--no-color", "team", "members", "list"])
    assert result.exit_code == 0
    assert "none" in result.stdout and "None" not in result.stdout


@pytest.mark.parametrize("command", [["invite", "show"]])
def test_malformed_invitation_preview_does_not_claim_acceptance(mock_team, command):
    mock_team[0].append(httpx.Response(200, content=b"not JSON"))
    result = invoke(*command, input=TOKEN)
    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_invitation_response"
    assert "No write was submitted" in error["hint"]
    assert len(mock_team[1]) == 1 and mock_team[1][0].method == "GET"
    assert not mock_team[2]
    assert_private(result)


@pytest.mark.parametrize(
    "command,payloads",
    [
        (
            ["team", "leave", "--yes"],
            [ok(ACCOUNT), ok(IDENTITY), ok([{**MEMBER, "user_id": "user_one"}])],
        ),
        (
            ["team", "invites", "resend", "inv_one", "--yes"],
            [ok(ACCOUNT), ok([INVITATION])],
        ),
        (
            ["team", "invites", "revoke", "inv_one", "--yes"],
            [ok(ACCOUNT), ok([INVITATION])],
        ),
    ],
)
def test_malformed_empty_success_keeps_unknown_write_guidance(
    mock_team, command, payloads
):
    mock_team[0].extend([*payloads, httpx.Response(204, content=b"unexpected")])
    result = invoke(*command)
    assert result.exit_code == 1
    assert "The write may already have completed" in result.stderr
    assert "No write was submitted" not in result.stderr
    assert len([c for c in mock_team[1] if c.method != "GET"]) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["invite", "accept"],
        ["invite", "accept", "--yes"],
        ["invite", "accept", "--new-user", "--yes"],
    ],
)
def test_invite_accept_is_retired_without_reading_token_or_credentials(
    mock_team, monkeypatch, args
):
    monkeypatch.setattr(
        auth,
        "resolve_credential",
        lambda **_: (_ for _ in ()).throw(AssertionError("auth read")),
    )
    result = invoke(*args, input=TOKEN + "\n")
    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "invite_accept_retired"
    assert error["message"] == (
        "Accept the invitation from the link in your email, then run solwyn login."
    )
    assert not mock_team[1]
    assert_private(result)


# Review item 6: approval outcomes pass through the team/account adapter.


def _rename(*options):
    return runner.invoke(
        app, ["--json", *options, "account", "rename", "New Team", "--yes"]
    )


def test_rename_mismatched_approval_keeps_its_code_and_exits_six(
    mock_team, monkeypatch
):
    fake_keychain(monkeypatch)
    mock_team[0].extend([ok(ACCOUNT), approval_invalid("mismatch")])
    result = _rename("--approval", APPROVAL_ID)
    assert result.exit_code == 6, result.output
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "approval_invalid"
    assert error["message"] == "The request changed since it was approved."


def test_rename_approval_limit_keeps_its_code_and_exits_six(mock_team, monkeypatch):
    fake_keychain(monkeypatch)
    mock_team[0].extend(
        [
            ok(ACCOUNT),
            httpx.Response(
                429,
                json={
                    "detail": {
                        "code": "approval_limit_reached",
                        "message": "Too many pending approvals.",
                    }
                },
            ),
        ]
    )
    result = _rename()
    assert result.exit_code == 6, result.output
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"] == "approval_limit_reached"
    )


def test_rename_pending_approval_keeps_the_continuation_and_exits_six(
    mock_team, monkeypatch
):
    fake_keychain(monkeypatch)
    mock_team[0].extend(
        [ok(ACCOUNT), approval_required(), ok(approval_body("pending"))]
    )
    result = _rename("--approval-wait", "0")
    assert result.exit_code == 6, result.output
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "approval_pending"
    assert error["detail"]["retry_argv"][:3] == ["solwyn", "--approval", APPROVAL_ID]
    assert error["detail"]["retry_command"] in error["hint"]
    assert error["detail"]["approval_id"] == APPROVAL_ID
    assert error["detail"]["approval_url"] == APPROVAL_URL


# Review round 2, item 6: a spent approval stays visible through the adapter.


def test_rename_handler_failure_after_explicit_approval_says_it_was_spent(
    mock_team, monkeypatch
):
    fake_keychain(monkeypatch)
    mock_team[0].extend(
        [
            ok(ACCOUNT),
            httpx.Response(
                422, json={"detail": {"code": "validation_error", "message": "Bad."}}
            ),
        ]
    )
    result = _rename("--approval", APPROVAL_ID)
    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "team_request_failed"
    assert error["hint"].endswith(APPROVAL_USED_HINT)


def test_rename_handler_failure_after_automatic_approval_says_it_was_spent(
    mock_team, monkeypatch
):
    fake_keychain(monkeypatch)
    mock_team[0].extend(
        [
            ok(ACCOUNT),
            approval_required(),
            ok(approval_body("approved")),
            httpx.Response(
                422, json={"detail": {"code": "validation_error", "message": "Bad."}}
            ),
        ]
    )
    result = _rename()
    assert result.exit_code == 1, result.output
    assert mock_team[1][-1].headers["Solwyn-Approval"] == APPROVAL_ID
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["hint"].endswith(APPROVAL_USED_HINT)
