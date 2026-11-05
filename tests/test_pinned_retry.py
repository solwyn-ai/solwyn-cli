"""Approval retries re-run a pinned command; nothing is ever persisted."""

import functools
import json
import shlex
from typing import Any

import httpx
import pytest
from login_fixtures import (
    APPROVAL_ID,
    approval_body,
    approval_invalid,
    approval_required,
    login_credential,
)
from test_notifications_commands import CHANNEL
from typer.testing import CliRunner

from solwyn_cli import approvals, auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import _notifications_common as common
from solwyn_cli.commands import budget_rules, plan
from solwyn_cli.main import app

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

    class _RecordingStore:
        kind = "file"

        def describe(self) -> str:
            return "/test/credentials.json"

        def has_origin(self, origin: str) -> bool:
            return False

        def get(self, origin: str, entry: str) -> None:
            return None

        def set_many(self, origin: str, values: dict[str, str]) -> None:
            writes.extend(values)

        def delete(self, origin: str, entries: object) -> None:
            return None

    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(auth, "_file_store", _RecordingStore)
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


# Round 3, item 2: positionals that look like options survive the retry.


@pytest.fixture
def team_server(server: Server, monkeypatch: pytest.MonkeyPatch) -> Server:
    from solwyn_cli.commands import _team

    factory = functools.partial(
        SolwynClient, transport=httpx.MockTransport(server), sleeper=lambda _: None
    )
    monkeypatch.setattr(_team, "SolwynClient", factory)
    monkeypatch.setattr(_team, "resolve_credential", lambda **_: LOGIN)
    monkeypatch.setattr(_team, "is_interactive", lambda: False)
    return server


ACCOUNT = {
    "id": "acct_one",
    "display_name": "Team One",
    "subscription_tier": "team",
    "alert_email_override": None,
    "seat_limit": 5,
    "seats_used": 2,
    "your_role": "owner",
}


@pytest.mark.parametrize("name", ["--help", "--production", "-x"])
def test_option_like_positional_keeps_the_separator_on_retry(
    team_server: Server, no_storage: list[str], name: str
) -> None:
    team_server.queue.append(httpx.Response(200, json=ACCOUNT))
    error = _time_out(team_server, ["account", "rename", "--yes", "--", name])
    first = team_server.changes[-1]

    argv = error["detail"]["retry_argv"]
    assert argv[-2:] == ["--", name]
    assert json.loads(first.content) == {"display_name": name}
    team_server.queue += [
        httpx.Response(200, json=ACCOUNT),
        httpx.Response(200, json={**ACCOUNT, "display_name": name}),
    ]

    retried = _rerun(error)

    assert retried.exit_code == 0, retried.output
    retry = team_server.changes[-1]
    assert retry.method == "PATCH"
    assert retry.content == first.content
    assert retry.headers["Solwyn-Approval"] == APPROVAL_ID


def _dummy(param: Any) -> list[str]:
    choices = getattr(param.type, "choices", None)
    if choices:
        value = list(choices)[0]
        return [str(getattr(value, "value", value))]
    if param.type.name.startswith(("integer", "float")):
        return ["1"]
    return ["x"]


def test_every_approval_capable_positional_round_trips_through_the_retry() -> None:
    import typer.main
    from test_help_snapshots import _NO_APPROVAL_WRITES, _WRITE, _commands

    checked = []
    for path, command in _commands(typer.main.get_command(app)):
        endpoint = next(
            (
                line
                for line in (command.epilog or "").splitlines()
                if line.startswith("Endpoint:")
            ),
            "",
        )
        writes = [
            r for _, r in _WRITE.findall(endpoint) if not _NO_APPROVAL_WRITES.match(r)
        ]
        arguments = [
            p for p in command.params if getattr(p, "param_type_name", "") == "argument"
        ]
        if not writes or not arguments:
            continue
        options: list[str] = []
        for param in command.params:
            if param.required and getattr(param, "param_type_name", "") == "option":
                options += [param.opts[0], *_dummy(param)]
        values: list[str] = []
        for param in arguments:
            # Choice and numeric positionals are validated; free text may look
            # like an option, which is exactly what the separator must protect.
            constrained = getattr(param.type, "choices", None) or (
                param.type.name.startswith(("integer", "float"))
            )
            dashed = _dummy(param)[0] if constrained else "--looks-like-an-option"
            values += [dashed] * (2 if param.nargs == -1 else 1)
        context = command.make_context(path[-1], [*options, "--", *values])
        rendered = approvals.render_leaf(context, {})
        assert "--" in rendered, path
        again = command.make_context(path[-1], list(rendered))
        assert again.params == context.params, path
        checked.append(" ".join(path))
    assert "account rename" in checked
    assert len(checked) >= 10, checked


# Round 3, item 1: init --run-cap resumes with the created project.


def test_init_run_cap_waiting_for_approval_continues_with_budget_rule_add(
    no_storage: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import copy

    from test_budget_rules_command import ROUTING
    from test_budget_set_command import BUDGET_RESPONSE

    from solwyn_cli import config as config_module
    from solwyn_cli.commands import budget as budget_module
    from solwyn_cli.commands import init as init_module

    project = BUDGET_RESPONSE["project_id"]
    created = {
        "id": project,
        "name": "Created project",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "alert_only",
        "created_at": "2026-07-10T12:00:00Z",
        "key": "solwyn_sk_" + "cd" * 32,
        "key_prefix": ("solwyn_sk_" + "cd" * 32)[:26],
        "scope": "full",
        "dedicated_keys_only": False,
        "key_id": "6f1d0c9e-0000-4000-8000-0000000000cd",
        "created_via": "cli",
        "expires_at": "2027-07-10T12:00:00Z",
    }
    puts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/projects" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/api/v1/projects":
            return httpx.Response(201, json=created)
        if "/cli/approvals/" in path:
            return httpx.Response(200, json=approval_body("pending"))
        if request.method == "PUT":
            puts.append(request)
            if "Solwyn-Approval" not in request.headers:
                return approval_required()
            rule = {
                "type": "runaway_run",
                "rule_key": "run-cap",
                "channel_ids": [],
                "paused": False,
                "snoozed_until": None,
                "routing": ROUTING,
                "spend_threshold_usd": 5.0,
                "mode": "hard_deny",
                "call_threshold": None,
                "window_minutes": None,
            }
            body = copy.deepcopy(BUDGET_RESPONSE)
            body["thresholds"] = [*body["thresholds"], rule]
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=BUDGET_RESPONSE)

    factory = functools.partial(
        SolwynClient, transport=httpx.MockTransport(handle), sleeper=lambda _: None
    )
    for module in (init_module, budget_module, budget_rules):
        monkeypatch.setattr(module, "SolwynClient", factory)
    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: LOGIN)
    monkeypatch.setattr(budget_rules, "resolve_credential", lambda **_: LOGIN)
    stored: list[str] = []
    monkeypatch.setattr(
        init_module.auth,
        "store_api_key",
        lambda **fields: (
            stored.append(fields["project_id"])
            or auth.ApiKeyCredential(
                api_key=fields["key"], source="keyring", project=fields["project_id"]
            )
        ),
    )
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    # init creates a project only inside a git repo, named after it.
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.chdir(repository)

    waited = runner.invoke(
        app, ["--json", "--approval-wait", "0", "init", "--run-cap", "5"]
    )

    assert waited.exit_code == 6, waited.output
    error = json.loads(waited.stderr.splitlines()[-1])
    assert error["detail"]["retry_argv"] == [
        "solwyn",
        "--approval",
        APPROVAL_ID,
        "--json",
        "--project",
        project,
        "budget",
        "rule",
        "add",
        "--type",
        "runaway_run",
        "--spend",
        "5.0",
        "--mode",
        "hard_deny",
    ]
    assert f"Project {project} was created" in error["hint"]
    assert "same project name" not in error["hint"]
    assert stored == [project]

    retried = _rerun(error)

    assert retried.exit_code == 0, retried.output
    assert len(puts) == 2
    assert puts[1].headers["Solwyn-Approval"] == APPROVAL_ID
    assert puts[1].content == puts[0].content
    assert no_storage == []
