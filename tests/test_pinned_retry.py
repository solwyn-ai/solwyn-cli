"""Approval retries re-run a pinned command; nothing is ever persisted."""

import functools
import json
import shlex
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from login_fixtures import (
    APPROVAL_ID,
    approval_body,
    approval_invalid,
    approval_required,
    login_credential,
)
from solwyn_cli import approvals, auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import _notifications_common as common
from solwyn_cli.commands import budget_rules, plan
from solwyn_cli.main import app
from test_notifications_commands import CHANNEL


runner = CliRunner()
LOGIN = login_credential()
PROJECT = "proj_0123456789abcdef01234567"
SECRET = "https://hooks.slack.com/services/T000/B000/synthetic-secret"


class Server:
    def __init__(self) -> None:
        self.queue: list[httpx.Response] = []
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self.queue.pop(0)

    @property
    def changes(self) -> list[httpx.Request]:
        return [r for r in self.sent if "/cli/approvals/" not in r.url.path]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server()
    factory = functools.partial(
        SolwynClient, transport=httpx.MockTransport(fake), sleeper=lambda _: None
    )
    for module in (plan, budget_rules, common):
        monkeypatch.setattr(module, "SolwynClient", factory)
        monkeypatch.setattr(module, "resolve_credential", lambda **_: LOGIN)
    monkeypatch.setattr(plan, "_is_interactive", lambda: False, raising=False)
    return fake


@pytest.fixture
def no_storage(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail on any keychain write; reads return nothing."""
    writes: list[str] = []
    monkeypatch.setattr(auth.keyring, "get_password", lambda service, name: None)
    monkeypatch.setattr(
        auth.keyring,
        "set_password",
        lambda service, name, value: writes.append(name),
    )
    monkeypatch.setattr(auth.keyring, "delete_password", lambda service, name: None)
    return writes


def _time_out(server: Server, arguments: list[str], **kwargs: Any) -> dict[str, Any]:
    server.queue += [
        approval_required(),
        httpx.Response(200, json=approval_body("pending")),
    ]
    result = runner.invoke(
        app, ["--json", "--approval-wait", "0", *arguments], **kwargs
    )
    assert result.exit_code == 6, result.output
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "approval_pending"
    return error


def _rerun(error: dict[str, Any], **kwargs: Any) -> Any:
    argv = error["detail"]["retry_argv"]
    assert argv[0] == "solwyn"
    assert shlex.split(error["detail"]["retry_command"]) == argv
    return runner.invoke(app, argv[1:], **kwargs)


UPGRADE = ["plan", "upgrade", "--tier", "team", "--interval", "year", "--yes"]
UPGRADE += ["--no-browser"]
CHECKOUT = httpx.Response(
    200, json={"checkout_url": "https://checkout.stripe.com/c/pay/synthetic"}
)


def test_plan_upgrade_retry_pins_the_request_id_and_is_byte_identical(
    server: Server, no_storage: list[str]
) -> None:
    error = _time_out(server, UPGRADE)
    first = server.changes[0]
    client_request_id = json.loads(first.content)["client_request_id"]

    assert (
        f"--client-request-id {client_request_id}" in error["detail"]["retry_command"]
    )
    assert f"--approval {APPROVAL_ID}" in error["detail"]["retry_command"]
    assert error["detail"]["retry_command"] in error["hint"]
    server.queue.append(CHECKOUT)

    retried = _rerun(error)

    assert retried.exit_code == 0, retried.output
    retry = server.changes[-1]
    assert retry.headers["Solwyn-Approval"] == APPROVAL_ID
    assert (retry.method, retry.url.raw_path, retry.content) == (
        first.method,
        first.url.raw_path,
        first.content,
    )
    assert no_storage == []


def test_changed_arguments_build_different_bytes_and_ask_for_a_new_approval(
    server: Server, no_storage: list[str]
) -> None:
    error = _time_out(server, UPGRADE)
    first = server.changes[0]
    argv = error["detail"]["retry_argv"]
    changed = [("scale" if value == "team" else value) for value in argv]
    server.queue.append(approval_invalid("mismatch"))

    result = runner.invoke(app, changed[1:])

    assert server.changes[-1].content != first.content
    assert result.exit_code == 6
    rejection = json.loads(result.stderr.splitlines()[-1])
    assert rejection["code"] == "approval_invalid"
    assert rejection["message"] == "The request changed since it was approved."
    assert rejection["hint"] == "Run the command again to request a new approval."
    assert no_storage == []


def test_relative_snooze_is_pinned_to_an_absolute_time(
    server: Server, no_storage: list[str]
) -> None:
    arguments = ["--project", PROJECT, "budget", "threshold", "snooze", "80"]
    error = _time_out(server, [*arguments, "--until", "+2h"])
    first = server.changes[0]
    pinned = json.loads(first.content)["snoozed_until"]

    assert "+2h" not in error["detail"]["retry_command"]
    server.queue.append(
        httpx.Response(200, json={"snoozed_until": pinned.replace("Z", "+00:00")})
    )

    retried = _rerun(error)

    assert retried.exit_code == 0, retried.output
    assert server.changes[-1].content == first.content
    assert no_storage == []


def test_secret_from_stdin_is_never_pinned_and_the_note_says_how_to_supply_it(
    server: Server, no_storage: list[str]
) -> None:
    arguments = ["channels", "create", "--type", "slack", "--name", "Ops"]
    error = _time_out(server, [*arguments, "--secret-stdin"], input=SECRET)
    first = server.changes[0]

    assert SECRET not in json.dumps(error)
    assert "--secret-stdin" in error["detail"]["retry_argv"]
    assert "Supply the channel secret again" in error["hint"]
    server.queue.append(httpx.Response(201, json=CHANNEL))

    retried = _rerun(error, input=SECRET)

    assert server.changes[-1].content == first.content
    assert retried.exit_code == 0, retried.output
    assert no_storage == []


def test_the_stored_request_mechanism_is_gone() -> None:
    for name in ("remember_change", "recall_change", "forget_change"):
        assert not hasattr(approvals, name)


def test_human_hint_keeps_the_retry_command_on_one_line(
    server: Server, no_storage: list[str]
) -> None:
    server.queue += [
        approval_required(),
        httpx.Response(200, json=approval_body("pending")),
    ]
    result = runner.invoke(app, ["--no-color", "--approval-wait", "0", *UPGRADE])

    assert result.exit_code == 6
    hint = next(line for line in result.stderr.splitlines() if line.startswith("Hint:"))
    command = hint.removeprefix("Hint: After approving, run: ")
    argv = shlex.split(command)
    assert argv[:3] == ["solwyn", "--approval", APPROVAL_ID]
    assert "--approval-wait" not in argv
    assert "--client-request-id" in argv
