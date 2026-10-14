"""Customer mutations require an acknowledged, sanitized effects preview."""

import importlib
import json

import click
import httpx
import pytest
from typer.testing import CliRunner

from help_options import assert_public_bool_flag
from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.main import app
from test_keys_command import ROTATE_RESPONSE as KEY_ROTATION
from test_runs_command import RUN, STORAGE_ID, _list_response
from test_webhooks_command import ROTATE_RESPONSE as WEBHOOK_ROTATION

runner = CliRunner()
CASES = [
    (
        "runs",
        ["stop", STORAGE_ID],
        "POST",
        f"/api/v1/projects/proj%2Fone/agent-runs/{STORAGE_ID}/stop",
        "future checks and lease renewals",
        "solwyn.runs.stop.v1",
    ),
    (
        "tags",
        ["retire", "env"],
        "DELETE",
        "/api/v1/account/tags",
        "historical values",
        "solwyn.tags.v1",
    ),
    (
        "keys",
        ["revoke", "--key-id", "key/old"],
        "DELETE",
        "/api/v1/projects/proj%2Fone/key/key%2Fold",
        "without a replacement",
        "solwyn.keys-revoke.v1",
    ),
    (
        "webhooks",
        ["delete", "wh/one"],
        "DELETE",
        "/api/v1/projects/proj%2Fone/webhooks/wh%2Fone",
        "Deactivate",
        "solwyn.webhooks-delete.v1",
    ),
    (
        "channels",
        ["delete", "ch/one"],
        "DELETE",
        "/api/v1/account/notification-channels/ch%2Fone",
        "routing references",
        "solwyn.channels.delete.v1",
    ),
    (
        "keys",
        ["rotate", "--grace-seconds", "0"],
        "POST",
        "/api/v1/projects/proj%2Fone/key/rotate",
        "immediately",
        "solwyn.keys-rotate.v1",
    ),
    (
        "webhooks",
        ["rotate", "wh/one"],
        "POST",
        "/api/v1/projects/proj%2Fone/webhooks/wh%2Fone/rotate",
        "immediately",
        "solwyn.webhooks-rotate.v1",
    ),
]


def argv(case, *extra):
    group, args, *_ = case
    return ["--json", "--project", "proj/one", group, *args, *extra]


def payload(case):
    group, args, *_ = case
    if group == "runs":
        return {
            "project_id": "proj/one",
            "run_id": STORAGE_ID,
            "run_name": "agent",
            "stopped_at": "2026-10-14T12:00:00Z",
            "already_stopped": False,
            "leases_revoked": 2,
            "enforcement_deadline": None,
        }
    if args[0] == "rotate":
        return KEY_ROTATION if group == "keys" else WEBHOOK_ROTATION
    return None


@pytest.fixture
def transport(monkeypatch):
    calls, replies, sleeps = [], [], []
    modules = [
        importlib.import_module("solwyn_cli.commands." + name)
        for name in ("runs", "tags", "keys", "webhooks", "_notifications_common")
    ]

    def handle(request):
        calls.append(request)
        assert replies, "unexpected HTTP request"
        return replies.pop(0)

    def client(**kwargs):
        return SolwynClient(
            **kwargs, transport=httpx.MockTransport(handle), sleeper=sleeps.append
        )

    for module in modules:
        monkeypatch.setattr(module, "SolwynClient", client)
        monkeypatch.setattr(
            module,
            "resolve_credential",
            lambda: auth.JwtCredential(
                access_token="synthetic-access", refresh_token="synthetic-refresh"
            ),
        )
    return calls, replies, sleeps, modules


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
def test_non_tty_refuses_before_credentials_or_http(transport, monkeypatch, case):
    calls, _, _, modules = transport
    for module in modules:
        monkeypatch.setattr(
            module,
            "resolve_credential",
            lambda: pytest.fail("credentials must not be resolved"),
        )
    result = runner.invoke(app, argv(case))
    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "confirmation_required"
    assert case[4] in error["message"]
    assert calls == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
def test_yes_previews_then_performs_exact_mutation_once(transport, case):
    calls, replies, _, _ = transport
    data = payload(case)
    replies.append(
        httpx.Response(204) if data is None else httpx.Response(200, json=data)
    )
    result = runner.invoke(app, argv(case, "--yes"))
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert (calls[0].method, calls[0].url.raw_path.decode().split("?")[0]) == (
        case[2],
        case[3],
    )
    assert json.loads(result.stdout)["schema"] == case[5]
    assert len(result.stdout.splitlines()) == 1
    assert case[4] in result.stderr
    assert (
        "proj/one" in result.stderr
        if case[0] not in ("tags", "channels")
        else "authenticated account" in result.stderr
    )
    if case[0] == "tags":
        assert dict(calls[0].url.params) == {"key": "env"}
    elif case[0] == "keys" and case[1][0] == "rotate":
        assert json.loads(calls[0].content) == {"grace_seconds": 0, "scope": None}
    else:
        assert calls[0].content == b""
    assert "synthetic-access" not in result.output
    assert "synthetic-refresh" not in result.output


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
@pytest.mark.parametrize(
    "answer,expected", [("y\n", 0), ("n\n", 2), ("", 2), ("\x03", 2)]
)
def test_interactive_acceptance_decline_eof_and_interrupt(
    transport, monkeypatch, case, answer, expected
):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(confirmation, "is_interactive", lambda: True)
    calls, replies, _, _ = transport
    data = payload(case)
    replies.append(
        httpx.Response(204) if data is None else httpx.Response(200, json=data)
    )
    result = runner.invoke(app, argv(case), input=answer)
    assert result.exit_code == expected, result.output
    assert case[4] in result.stderr
    assert "Continue" in result.stderr
    assert "Continue" not in result.stdout
    assert "[y/N]: y" not in result.stdout
    assert len(calls) == (1 if expected == 0 else 0)
    if expected:
        assert result.stdout == ""
        assert (
            json.loads(result.stderr.splitlines()[-1])["code"]
            == "confirmation_declined"
        )
    else:
        assert json.loads(result.stdout)["schema"] == case[5]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
@pytest.mark.parametrize("color", [False, True], ids=["no-color", "color"])
def test_help_is_auth_and_http_free(transport, monkeypatch, case, color):
    calls, _, _, modules = transport
    for module in modules:
        monkeypatch.setattr(
            module,
            "resolve_credential",
            lambda: pytest.fail("help resolved credentials"),
        )
    # Typer caches its force-terminal decision at import, before these cases.
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", color)
    monkeypatch.setenv("TERM", "xterm-256color" if color else "dumb")
    if color:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
    else:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, argv(case, "--help"), color=color)
    assert result.exit_code == 0
    assert ("\x1b[" in result.stdout) is color
    assert "--yes" in click.unstyle(result.stdout)
    assert_public_bool_flag([case[0], case[1][0]], "--yes")
    assert calls == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
def test_invalid_result_never_emits_success_or_retries(transport, case):
    calls, replies, _, _ = transport
    replies.append(
        httpx.Response(204, content=b'{"unknown":"private"}')
        if payload(case) is None
        else httpx.Response(200, json={"unknown": "private"})
    )
    result = runner.invoke(app, argv(case, "--yes"))
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert len(calls) == 1
    assert "private" not in result.output


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
@pytest.mark.parametrize("status,expected", [(403, 3), (404, 5), (503, 1)])
def test_permission_missing_and_busy_responses_preserve_exits(
    transport, case, status, expected
):
    calls, replies, sleeps, _ = transport
    replies.append(
        httpx.Response(
            status,
            headers={"Retry-After": "1"},
            json={
                "detail": "Forbidden"
                if status == 403
                else "Not found"
                if status == 404
                else "Unavailable"
            },
        )
    )
    result = runner.invoke(app, argv(case, "--yes"))
    assert result.exit_code == expected, result.output
    assert result.stdout == ""
    assert len(calls) == 1
    assert sleeps == []


def test_raw_run_is_resolved_before_confirmation_and_decline_never_stops(
    transport, monkeypatch
):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(confirmation, "is_interactive", lambda: True)
    calls, replies, _, _ = transport
    replies.append(
        httpx.Response(
            200, json=_list_response(runs=[{**RUN, "project_id": "proj/one"}])
        )
    )
    case = ("runs", ["stop", RUN["name"]], *CASES[0][2:])
    result = runner.invoke(app, argv(case), input="n\n")
    assert result.exit_code == 2, result.output
    assert [r.method for r in calls] == ["GET"]
    assert STORAGE_ID in result.stderr
    assert RUN["name"] in result.stderr


@pytest.mark.parametrize(
    "group,args",
    [("tags", ["retire", "env\x1b[31m"]), ("channels", ["delete", "ch\x1b[31m"])],
)
def test_preview_and_confirmation_echo_sanitize_terminal_controls(
    transport, monkeypatch, group, args
):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(confirmation, "is_interactive", lambda: True)
    result = runner.invoke(app, argv((group, args)), input="n\n")
    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "\\x1b[31m" in result.stderr


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
def test_actual_keyboard_interrupt_during_prompt_never_submits(
    transport, monkeypatch, case
):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(confirmation, "is_interactive", lambda: True)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(confirmation.typer, "confirm", interrupted)
    calls, _, _, _ = transport
    result = runner.invoke(app, argv(case))
    assert result.exit_code == 2
    assert result.stdout == ""
    assert case[4] in result.stderr
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "confirmation_declined"
    assert calls == []


def test_invalid_confirmation_input_is_sanitized_on_stderr(transport, monkeypatch):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(confirmation, "is_interactive", lambda: True)
    calls, _, _, _ = transport
    result = runner.invoke(app, argv(CASES[1]), input="\x1b[31m\nn\n")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "\x1b" not in result.stderr
    assert "\ufffd[31m" in result.stderr
    assert calls == []


@pytest.mark.parametrize("ambiguous", [False, True])
def test_stop_raw_name_not_found_or_ambiguous_never_confirms_or_mutates(
    transport, monkeypatch, ambiguous
):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    monkeypatch.setattr(
        confirmation,
        "confirm_effects",
        lambda *args, **kwargs: pytest.fail("unresolved run was confirmed"),
    )
    # The command imported this helper directly.
    runs = importlib.import_module("solwyn_cli.commands.runs")
    monkeypatch.setattr(runs, "confirm_effects", confirmation.confirm_effects)
    calls, replies, _, _ = transport
    entries = (
        [
            {**RUN, "project_id": "proj/one"},
            {**RUN, "project_id": "proj/one", "id": "run_" + "2b" * 16},
        ]
        if ambiguous
        else []
    )
    replies.append(httpx.Response(200, json=_list_response(runs=entries)))
    case = ("runs", ["stop", RUN["name"]])
    result = runner.invoke(app, argv(case, "--yes"))
    assert result.exit_code == (1 if ambiguous else 5)
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == (
        "ambiguous_agent_run" if ambiguous else "agent_run_not_found"
    )
    assert [request.method for request in calls] == ["GET"]
    if ambiguous:
        assert STORAGE_ID in error["hint"]
        assert "run_" + "2b" * 16 in error["hint"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0] + "-" + c[1][0])
def test_effects_are_written_before_destructive_http(transport, monkeypatch, case):
    confirmation = importlib.import_module("solwyn_cli.commands._confirmation")
    calls, replies, _, _ = transport
    displayed = []
    echo = confirmation.typer.echo

    def observe(message=None, **kwargs):
        if (
            message is not None
            and case[4] in message
            and message.startswith(
                (
                    "Stop run ",
                    "Retire tag ",
                    "Hard-revoke ",
                    "Rotate ",
                    "Deactivate ",
                    "Delete channel ",
                )
            )
        ):
            assert calls == []
            assert kwargs.get("err") is True
            displayed.append(message)
        return echo(message, **kwargs)

    monkeypatch.setattr(confirmation.typer, "echo", observe)
    data = payload(case)
    replies.append(
        httpx.Response(204) if data is None else httpx.Response(200, json=data)
    )
    result = runner.invoke(app, argv(case, "--yes"))
    assert result.exit_code == 0
    assert displayed
    assert len(calls) == 1


@pytest.mark.parametrize("grace", [None, "3600"])
def test_key_rotation_grace_still_requires_confirmation_before_credentials(
    transport, monkeypatch, grace
):
    calls, _, _, modules = transport
    keys = next(module for module in modules if module.__name__.endswith(".keys"))
    monkeypatch.setattr(
        keys, "resolve_credential", lambda: pytest.fail("credentials resolved")
    )
    args = ["rotate"] + (["--grace-seconds", grace] if grace else [])
    result = runner.invoke(app, argv(("keys", args)))
    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "confirmation_required"
    assert ("3600 seconds" if grace else "Core default grace period") in error[
        "message"
    ]
    assert calls == []
