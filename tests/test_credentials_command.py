"""CLI login management and approval status commands."""

import functools
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from golden.harness import assert_matches_golden
from key_fixtures import keys_entry
from login_fixtures import LOGIN_TOKEN, fake_keychain, login_entries
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import approvals as approvals_module
from solwyn_cli.commands import credentials as credentials_module
from solwyn_cli.main import app

runner = CliRunner()
VALID_KEY = "solwyn_sk_" + "ab" * 32
CURRENT_ID = "6c1f2a90-0000-4000-8000-000000000001"
OTHER_ID = "91be7d03-0000-4000-8000-000000000002"
APPROVAL_ID = "0b7d2c4e-0000-4000-8000-000000000003"
NOW = datetime(2026, 10, 30, 14, 20, tzinfo=UTC)


def _credential(item_id: str, label: str, *, current: bool, **extra: Any) -> dict:
    return {
        "id": item_id,
        "label": label,
        "client_id": "solwyn-cli",
        "client_name": "Solwyn CLI",
        "client_version": "0.4.0",
        "scope": "read contain" if current else "read",
        "token_prefix": "solwyn_cli_9a3be2f1",
        "created_at": "2026-10-30T14:03:11Z",
        "expires_at": "2026-11-29T14:03:11Z",
        "last_used_at": None if not current else "2026-10-30T14:05:00Z",
        "created_ip": "203.0.113.7",
        "current": current,
        **extra,
    }


LISTING = {
    "credentials": [
        _credential(CURRENT_ID, "dev-laptop", current=True),
        _credential(OTHER_ID, "ci-runner", current=False),
    ]
}


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values: dict[str, str] = login_entries(id=CURRENT_ID)
    store = fake_keychain(monkeypatch)
    store.update(values)
    values = store
    return values


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        module,
        "SolwynClient",
        functools.partial(SolwynClient, transport=httpx.MockTransport(record)),
    )
    return requests


def _credentials_server(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(200, json=LISTING)
    return httpx.Response(204)


def test_list_shows_active_logins_with_the_current_one_marked(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _serve(monkeypatch, credentials_module, _credentials_server)

    table = runner.invoke(app, ["--no-color", "credentials", "list"])
    machine = runner.invoke(app, ["--json", "credentials", "list"])

    assert table.exit_code == machine.exit_code == 0
    lines = table.stdout.splitlines()
    assert lines[0].split() == [
        "ID",
        "Label",
        "Scope",
        "Created",
        "Last",
        "used",
        "Expires",
    ]
    assert lines[1].split() == [
        "6c1f2a90",
        "dev-laptop",
        "read,contain",
        "2026-10-30",
        "2026-10-30",
        "2026-11-29",
        "current",
    ]
    assert lines[2].split() == [
        "91be7d03",
        "ci-runner",
        "read",
        "2026-10-30",
        "never",
        "2026-11-29",
    ]
    payload = json.loads(machine.stdout)
    assert payload["schema"] == "solwyn.credentials.list.v1"
    assert [item["id"] for item in payload["credentials"]] == [CURRENT_ID, OTHER_ID]
    assert payload["credentials"][0]["current"] is True
    assert requests[0].url.path == "/api/v1/cli/credentials"
    assert requests[0].headers["Authorization"] == f"Bearer {LOGIN_TOKEN}"
    assert LOGIN_TOKEN not in table.stdout + machine.stdout


def test_list_with_only_a_project_key_needs_a_login(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.clear()
    requests = _serve(monkeypatch, credentials_module, _credentials_server)

    result = runner.invoke(
        app, ["--json", "credentials", "list"], env={"SOLWYN_API_KEY": VALID_KEY}
    )

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "authentication_required"
    assert requests == []


def test_revoke_by_prefix_deletes_that_login_and_keeps_the_local_one(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _serve(monkeypatch, credentials_module, _credentials_server)

    result = runner.invoke(app, ["--json", "credentials", "revoke", "91be7d03"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solwyn.credentials.revoke.v1",
        "id": OTHER_ID,
        "label": "ci-runner",
        "revoked": True,
        "current": False,
        "cleared_local_login": False,
    }
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/v1/cli/credentials"),
        ("DELETE", f"/api/v1/cli/credentials/{OTHER_ID}"),
    ]
    assert auth.LOGIN_ENTRY in keychain


def test_revoke_current_clears_the_keychain_login(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry()
    requests = _serve(monkeypatch, credentials_module, _credentials_server)

    result = runner.invoke(app, ["--json", "credentials", "revoke", "--current"])

    assert result.exit_code == 0
    assert requests[-1].url.path == f"/api/v1/cli/credentials/{CURRENT_ID}"
    assert json.loads(result.stdout)["cleared_local_login"] is True
    # Revoking the login keeps the stored API keys.
    assert keychain == {auth.API_KEYS_ENTRY: keys_entry()}


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (["credentials", "revoke"], 2),
        (["credentials", "revoke", "91be7d03", "--current"], 2),
        (["credentials", "revoke", "91be7d0"], 2),
        (["credentials", "revoke", "ffffffff"], 5),
    ],
)
def test_revoke_selection_errors(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    exit_code: int,
) -> None:
    requests = _serve(monkeypatch, credentials_module, _credentials_server)

    result = runner.invoke(app, ["--json", *arguments])

    assert result.exit_code == exit_code
    assert all(request.method == "GET" for request in requests)


def test_revoke_with_an_ambiguous_prefix_is_a_usage_error(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    twins = {
        "credentials": [
            _credential("abcdefgh-1", "one", current=False),
            _credential("abcdefgh-2", "two", current=False),
        ]
    }
    _serve(
        monkeypatch,
        credentials_module,
        lambda request: httpx.Response(200, json=twins),
    )

    result = runner.invoke(app, ["--json", "credentials", "revoke", "abcdefgh"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "cli_credential_ambiguous"


def test_revoke_not_found_from_the_server_exits_five(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=LISTING)
        return httpx.Response(
            404,
            json={"detail": {"code": "cli_credential_not_found", "message": "Gone."}},
        )

    _serve(monkeypatch, credentials_module, handler)

    result = runner.invoke(app, ["--json", "credentials", "revoke", "91be7d03"])

    assert result.exit_code == 5
    assert json.loads(result.stderr)["code"] == "cli_credential_not_found"


def _approval(status: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "id": APPROVAL_ID,
        "status": status,
        "action": "budget.update",
        "title": "Change the budget for project checkout",
        "reasons": ["raises_cap"],
        "changes": [
            {
                "field": "limit_usd",
                "label": "Budget limit",
                "before": "1000.00",
                "after": "1000000.00",
                "effect": "raises_cap",
            }
        ],
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }
    body.update(overrides)
    return body


@pytest.fixture
def approval_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    current = [NOW]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(approvals_module, "sleep", sleep)
    monkeypatch.setattr(approvals_module, "now", lambda: current[0])
    return sleeps


@pytest.mark.parametrize("status", ["pending", "approved", "consumed"])
def test_approval_status_shows_the_approval(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
    status: str,
) -> None:
    requests = _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(200, json=_approval(status)),
    )

    result = runner.invoke(app, ["--json", "approvals", "status", APPROVAL_ID])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solwyn.approval.v1"
    assert payload["status"] == status
    assert payload["changes"][0]["after"] == "1000000.00"
    assert requests[0].url.path == f"/api/v1/cli/approvals/{APPROVAL_ID}"
    assert approval_clock == []


@pytest.mark.parametrize(
    ("approval", "code"),
    [
        (_approval("denied"), "approval_denied"),
        (_approval("expired"), "approval_expired"),
        (
            _approval("pending", expires_at=(NOW - timedelta(seconds=1)).isoformat()),
            "approval_expired",
        ),
    ],
)
def test_denied_or_expired_approval_exits_six(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
    approval: dict[str, Any],
    code: str,
) -> None:
    _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(200, json=approval),
    )

    result = runner.invoke(app, ["--json", "approvals", "status", APPROVAL_ID])

    assert result.exit_code == 6
    assert json.loads(result.stderr)["code"] == code


def test_approval_status_waits_for_a_decision(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
) -> None:
    answers = iter(["pending", "pending", "approved"])
    _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(200, json=_approval(next(answers))),
    )

    result = runner.invoke(
        app, ["--json", "approvals", "status", APPROVAL_ID, "--wait", "60"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "approved"
    assert approval_clock == [3.0, 3.0]


def test_approval_status_wait_timeout_exits_six_pending(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
) -> None:
    _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(200, json=_approval("pending")),
    )

    result = runner.invoke(
        app, ["--json", "approvals", "status", APPROVAL_ID, "--wait", "7"]
    )

    assert result.exit_code == 6
    assert json.loads(result.stderr)["code"] == "approval_pending"
    assert approval_clock == [3.0, 3.0]


def test_another_users_approval_is_not_found(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
) -> None:
    _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(
            404, json={"detail": {"code": "approval_not_found", "message": "No."}}
        ),
    )

    result = runner.invoke(app, ["--json", "approvals", "status", APPROVAL_ID])

    assert result.exit_code == 5
    assert json.loads(result.stderr)["code"] == "approval_not_found"


GOLDEN_DIR = Path(__file__).parent / "golden"


def test_credentials_and_approval_outputs_match_goldens(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    approval_clock: list[float],
) -> None:
    _serve(monkeypatch, credentials_module, _credentials_server)
    _serve(
        monkeypatch,
        approvals_module,
        lambda request: httpx.Response(200, json=_approval("pending")),
    )

    listing = runner.invoke(app, ["--json", "credentials", "list"])
    listing_table = runner.invoke(app, ["--no-color", "credentials", "list"])
    revoke = runner.invoke(app, ["--json", "credentials", "revoke", "91be7d03"])
    approval = runner.invoke(app, ["--json", "approvals", "status", APPROVAL_ID])
    approval_table = runner.invoke(
        app, ["--no-color", "approvals", "status", APPROVAL_ID]
    )

    assert_matches_golden(listing.stdout, GOLDEN_DIR / "credentials-list.json")
    assert_matches_golden(
        listing_table.stdout, GOLDEN_DIR / "credentials-list-table.txt"
    )
    assert_matches_golden(revoke.stdout, GOLDEN_DIR / "credentials-revoke.json")
    assert_matches_golden(approval.stdout, GOLDEN_DIR / "approval-status.json")
    assert_matches_golden(
        approval_table.stdout, GOLDEN_DIR / "approval-status-table.txt"
    )
