"""Account audit command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import audit as audit_module
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "a1" * 32
ACCESS_TOKEN = "audit-access-token"
REFRESH_TOKEN = "audit-refresh-token"

AUDIT_RESPONSE = {
    "events": [
        {
            "id": "audit_02",
            "account_id": "acct_alpha",
            "actor_user_id": "user_operator",
            "action": "agent_run.stopped",
            "target_type": "agent_run",
            "target_id": "run_0123456789abcdef0123456789abcdef",
            "detail": {"reason": "manual_kill"},
            "created_at": "2026-08-30T16:45:00Z",
        },
        {
            "id": "audit_01",
            "account_id": "acct_alpha",
            "actor_user_id": "user_owner",
            "action": "project.updated",
            "target_type": "project",
            "target_id": "proj_alpha",
            "created_at": "2026-08-29T12:00:00Z",
        },
    ],
    "next_cursor": "eyJjcmVhdGVkX2F0IjoiMjAyNi0wOC0yOVQxMjowMDowMFoifQ",
    "has_more": True,
}


class FakeClient:
    instances: list["FakeClient"] = []
    responses: list[httpx.Response | BaseException] = []

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url
        self.credential = credential
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(audit_module, "SolwynClient", FakeClient, raising=False)
    yield


def _jwt() -> auth.JwtCredential:
    return auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )


def _key(source: str) -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def test_root_registers_audit_without_deferred_team_crud() -> None:
    root = get_command(app)

    assert "audit" in root.commands
    assert "team" not in root.commands


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_audit_sends_exact_filters_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(
        audit_module, "resolve_credential", lambda: credential, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=AUDIT_RESPONSE)]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *options,
            "audit",
            "--range",
            "90d",
            "--action",
            "project.updated",
            "--cursor",
            "cursor/start",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/audit-events",
            {
                "params": {
                    "range": "90d",
                    "action": "project.updated",
                    "cursor": "cursor/start",
                }
            },
        )
    ]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"audit-{output_format}.{suffix}",
    )
    if output_format == "table":
        assert result.stderr == (
            "More audit events are available.\n"
            "Next cursor (JSON escaped): "
            '"eyJjcmVhdGVkX2F0IjoiMjAyNi0wOC0yOVQxMjowMDowMFoifQ"\n'
            "Run `solwyn audit --all` to fetch all remaining events.\n"
        )
    else:
        assert result.stderr == ""


def test_audit_table_escapes_opaque_cursor_in_pagination_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "resolve_credential", _jwt, raising=False)
    unsafe_cursor = "cursor\n\x1b]8;;https://example.invalid\x07terminal-link"
    FakeClient.responses = [
        httpx.Response(
            200,
            json={"events": [], "next_cursor": unsafe_cursor, "has_more": True},
        )
    ]

    result = runner.invoke(app, ["--no-color", "audit"])

    assert result.exit_code == 0, result.stderr
    assert unsafe_cursor not in result.stderr
    assert "\\n\\u001b]8;;https://example.invalid\\u0007terminal-link" in result.stderr
    assert "solwyn audit --all" in result.stderr


def test_audit_defaults_to_30d_and_omits_absent_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "resolve_credential", _jwt, raising=False)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={"events": [], "next_cursor": None, "has_more": False},
        )
    ]

    result = runner.invoke(app, ["--json", "audit"])

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/audit-events",
            {"params": {"range": "30d"}},
        )
    ]


def test_audit_all_follows_opaque_cursors_and_combines_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_module, "resolve_credential", _jwt, raising=False)
    first = {
        "events": [AUDIT_RESPONSE["events"][0]],
        "next_cursor": "cursor/next",
        "has_more": True,
    }
    second = {
        "events": [AUDIT_RESPONSE["events"][1]],
        "next_cursor": None,
        "has_more": False,
    }
    FakeClient.responses = [
        httpx.Response(200, json=first),
        httpx.Response(200, json=second),
    ]

    result = runner.invoke(
        app,
        ["--json", "audit", "--action", "project.updated", "--all"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/audit-events",
            {"params": {"range": "30d", "action": "project.updated"}},
        ),
        (
            "GET",
            "/api/v1/account/audit-events",
            {
                "params": {
                    "range": "30d",
                    "action": "project.updated",
                    "cursor": "cursor/next",
                }
            },
        ),
    ]
    payload = json.loads(result.stdout)
    assert [event["id"] for event in payload["events"]] == ["audit_02", "audit_01"]
    assert payload["next_cursor"] is None
    assert payload["has_more"] is False


@pytest.mark.parametrize(
    "response",
    [
        {"events": [], "next_cursor": None, "has_more": True},
        {"events": [], "next_cursor": "same", "has_more": True},
    ],
)
def test_audit_all_rejects_broken_or_repeated_pagination(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    monkeypatch.setattr(audit_module, "resolve_credential", _jwt, raising=False)
    FakeClient.responses = [httpx.Response(200, json=response)]
    args = ["--json", "audit", "--all"]
    if response["next_cursor"] == "same":
        args.extend(["--cursor", "same"])

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_audit_response"
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("source", ["environment", "keyring"])
def test_audit_rejects_project_keys_before_network(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setattr(
        audit_module, "resolve_credential", lambda: _key(source), raising=False
    )

    result = runner.invoke(app, ["--json", "audit"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "solwyn login" in json.loads(result.stderr)["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["--range", "1d"],
        ["--action", ""],
        ["--action", "x" * 101],
    ],
)
def test_audit_validates_query_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        audit_module,
        "resolve_credential",
        lambda: pytest.fail("must validate before auth"),
        raising=False,
    )

    result = runner.invoke(app, ["--json", "audit", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "payload",
    [
        {**AUDIT_RESPONSE, "server_extra": True},
        {
            **AUDIT_RESPONSE,
            "events": [{**AUDIT_RESPONSE["events"][0], "event_extra": "unexpected"}],
        },
        {
            **AUDIT_RESPONSE,
            "events": [
                {
                    key: value
                    for key, value in AUDIT_RESPONSE["events"][0].items()
                    if key != "actor_user_id"
                }
            ],
        },
    ],
)
def test_audit_strictly_rejects_response_drift_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(audit_module, "resolve_credential", _jwt, raising=False)
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(app, ["--json", "audit"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_audit_response"
    assert ACCESS_TOKEN not in result.stderr + repr(result.exception)
    assert REFRESH_TOKEN not in result.stderr + repr(result.exception)
