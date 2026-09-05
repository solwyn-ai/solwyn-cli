"""Agent-run command contract tests."""

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import runs as runs_module
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "a4" * 32
STORAGE_ID = "run_" + "1a" * 16
SECOND_STORAGE_ID = "run_" + "2b" * 16
JWT = auth.JwtCredential(access_token="runs-access", refresh_token="runs-refresh")

RUN = {
    "id": STORAGE_ID,
    "name": "checkout-agent",
    "parent_run_id": None,
    "project_id": "proj_alpha",
    "started_at": "2026-08-01T10:00:00Z",
    "last_seen_at": "2026-08-07T12:30:00Z",
    "total_calls": 42,
    "total_cost": 12.345678,
    "total_denied_cost": 1.25,
    "total_input_tokens": 12000,
    "total_output_tokens": 3400,
    "velocity_flags": ["rate_acceleration", "future_signal"],
    "stopped_at": None,
    "stopped_by_user_id": None,
    "stoppable": True,
}
WINDOW = {
    "requested_from": "2026-08-01T00:00:00Z",
    "requested_to": "2026-08-08T00:00:00Z",
    "effective_from": "2026-08-01T00:00:00Z",
    "effective_to": "2026-08-08T00:00:00Z",
    "retention_clamped": False,
}


def _list_response(
    *,
    runs: list[dict[str, Any]] | None = None,
    next_cursor: str | None = None,
    has_more: bool = False,
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "runs": [dict(RUN)] if runs is None else runs,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "window": dict(WINDOW) if window is None else window,
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
        response = self.__class__.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(runs_module, "SolwynClient", FakeClient, raising=False)
    yield


def _key(source: str = "environment") -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _run_args(*args: str, json_output: bool = False) -> list[str]:
    return [
        *(["--json"] if json_output else ["--no-color"]),
        "--project",
        "proj_alpha",
        "runs",
        *args,
    ]


def test_runs_group_registers_list_show_and_stop() -> None:
    result = runner.invoke(app, ["runs", "--help"])

    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "show" in result.stdout
    assert "stop" in result.stdout


@pytest.mark.parametrize(
    ("credential", "json_output", "golden"),
    [
        (_key(), False, "runs-list-key-table.txt"),
        (JWT, True, "runs-list-jwt-json.json"),
    ],
)
def test_runs_list_supports_key_and_jwt_with_exact_filters_and_goldens(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
    json_output: bool,
    golden: str,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [httpx.Response(200, json=_list_response())]

    result = runner.invoke(
        app,
        _run_args(
            "list",
            "--range",
            "30d",
            "--q",
            "checkout",
            "--parent",
            "parent/raw",
            "--sort",
            "total_cost",
            "--limit",
            "50",
            json_output=json_output,
        ),
    )

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / golden)
    assert result.stderr == ("window: 2026-08-01T00:00:00Z to 2026-08-08T00:00:00Z\n")
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/agent-runs",
            {
                "params": {
                    "range": "30d",
                    "q": "checkout",
                    "parent": "parent/raw",
                    "sort": "total_cost",
                    "limit": 50,
                }
            },
        )
    ]


def test_runs_list_reports_retention_clamp_without_corrupting_json_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    clamped = {
        **WINDOW,
        "requested_from": "2025-08-01T00:00:00Z",
        "effective_from": "2026-07-01T00:00:00Z",
        "retention_clamped": True,
    }
    FakeClient.responses = [
        httpx.Response(200, json=_list_response(window=clamped, runs=[]))
    ]

    result = runner.invoke(app, _run_args("list", "--range", "365d", json_output=True))

    assert result.exit_code == 0
    assert json.loads(result.stdout)["runs"] == []
    assert result.stderr == (
        "window: 2026-07-01T00:00:00Z to 2026-08-08T00:00:00Z\n"
        "retention clamp applied: requested 2025-08-01T00:00:00Z to "
        "2026-08-08T00:00:00Z\n"
    )


def test_runs_list_rejects_cursor_sort_mismatch_before_auth_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"id": STORAGE_ID, "sort": "total_cost", "value": 1.2}).encode()
        )
        .decode()
        .rstrip("=")
    )
    monkeypatch.setattr(
        runs_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
    )

    result = runner.invoke(
        app,
        _run_args(
            "list", "--cursor", cursor, "--sort", "total_calls", json_output=True
        ),
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "cursor_sort_mismatch"
    assert FakeClient.instances == []


def test_runs_list_rejects_unpaired_custom_window_before_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runs_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
    )

    result = runner.invoke(
        app,
        _run_args("list", "--from", "2026-08-01T00:00:00Z", json_output=True),
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "custom_window_pair_required"


def test_runs_list_preserves_range_gate_allowed_values_and_dashboard_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                402,
                json={
                    "detail": {
                        "code": "upgrade_required",
                        "feature": "agent_runs.range",
                        "current_tier": "free",
                        "required_tier": "production",
                        "message": "Range '30d' is not available on your tier",
                        "allowed_values": ["7d"],
                        "cta_path": "/billing",
                    }
                },
            )
        )
    ]

    result = runner.invoke(app, _run_args("list", "--range", "30d", json_output=True))

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["message"] == "Range '30d' is not available on your tier"
    assert error["hint"] == (
        "Allowed values: 7d. Review upgrade options in the dashboard: /billing"
    )


@pytest.mark.parametrize("json_output", [False, True])
def test_runs_show_storage_id_uses_detail_endpoint_and_goldens(
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=RUN)]

    result = runner.invoke(app, _run_args("show", STORAGE_ID, json_output=json_output))

    assert result.exit_code == 0
    suffix = "json.json" if json_output else "table.txt"
    assert_matches_golden(result.stdout, GOLDEN_DIR / f"runs-show-{suffix}")
    assert result.stderr == ""
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            f"/api/v1/projects/proj_alpha/agent-runs/{STORAGE_ID}",
            {},
        )
    ]


def test_runs_show_resolves_raw_name_across_pages_then_reads_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    first = {**RUN, "id": SECOND_STORAGE_ID, "name": "other"}
    FakeClient.responses = [
        httpx.Response(
            200,
            json=_list_response(runs=[first], next_cursor="page-2", has_more=True),
        ),
        httpx.Response(200, json=_list_response(runs=[RUN])),
        httpx.Response(200, json=RUN),
    ]

    result = runner.invoke(
        app,
        _run_args("show", "checkout-agent", json_output=True),
    )

    assert result.exit_code == 0
    requests = FakeClient.instances[0].requests
    assert requests[0][2]["params"] == {
        "range": "365d",
        "q": "checkout-agent",
        "sort": "last_seen_at",
        "limit": 100,
    }
    assert requests[1][2]["params"] == {
        "range": "365d",
        "q": "checkout-agent",
        "sort": "last_seen_at",
        "limit": 100,
        "cursor": "page-2",
    }
    assert requests[2][1].endswith(STORAGE_ID)


def test_runs_show_raw_name_falls_back_to_largest_server_allowed_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    gate = error_from_response(
        httpx.Response(
            402,
            json={
                "detail": {
                    "code": "upgrade_required",
                    "message": "Range unavailable",
                    "allowed_values": ["7d", "future", "90d", "30d"],
                    "cta_path": "/billing",
                }
            },
        )
    )
    FakeClient.responses = [
        gate,
        httpx.Response(200, json=_list_response()),
        httpx.Response(200, json=RUN),
    ]

    result = runner.invoke(app, _run_args("show", "checkout-agent", json_output=True))

    assert result.exit_code == 0
    assert [
        request[2]["params"]["range"]
        for request in FakeClient.instances[0].requests[:2]
    ] == ["365d", "90d"]


def test_runs_show_rejects_ambiguous_exact_names_with_candidate_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    duplicate = {**RUN, "id": SECOND_STORAGE_ID}
    FakeClient.responses = [
        httpx.Response(200, json=_list_response(runs=[RUN, duplicate]))
    ]

    result = runner.invoke(app, _run_args("show", "checkout-agent", json_output=True))

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "ambiguous_agent_run"
    assert STORAGE_ID in error["hint"]
    assert SECOND_STORAGE_ID in error["hint"]
    assert len(FakeClient.instances[0].requests) == 1


def test_runs_show_returns_not_found_when_raw_name_has_no_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", _key)
    other = {**RUN, "name": "checkout-agent-child"}
    FakeClient.responses = [httpx.Response(200, json=_list_response(runs=[other]))]

    result = runner.invoke(app, _run_args("show", "checkout-agent", json_output=True))

    assert result.exit_code == 5
    assert json.loads(result.stderr)["code"] == "agent_run_not_found"


@pytest.mark.parametrize("source", ["environment", "keyring"])
def test_runs_stop_refuses_project_keys_before_network(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", lambda: _key(source))

    result = runner.invoke(app, _run_args("stop", STORAGE_ID, json_output=True))

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "solwyn login" in error["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize("json_output", [False, True])
def test_runs_stop_resolves_raw_name_and_emits_honest_stop_result(
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", lambda: JWT)
    stop_response = {
        "project_id": "proj_alpha",
        "run_id": STORAGE_ID,
        "run_name": "checkout-agent",
        "stopped_at": "2026-08-08T12:00:00Z",
        "already_stopped": False,
        "leases_revoked": 2,
        "enforcement_deadline": "2026-08-08T12:00:30Z",
    }
    FakeClient.responses = [
        httpx.Response(200, json=_list_response()),
        httpx.Response(200, json=stop_response),
    ]

    result = runner.invoke(
        app,
        _run_args("stop", "checkout-agent", json_output=json_output),
    )

    assert result.exit_code == 0
    suffix = "json.json" if json_output else "table.txt"
    assert_matches_golden(result.stdout, GOLDEN_DIR / f"runs-stop-{suffix}")
    assert result.stderr == (
        "stops future checks and lease renewals; in-flight requests finish; "
        "fully effective by 2026-08-08T12:00:30Z\n"
    )
    assert FakeClient.instances[0].requests[-1] == (
        "POST",
        f"/api/v1/projects/proj_alpha/agent-runs/{STORAGE_ID}/stop",
        {"retry_rate_limit": False},
    )


def test_runs_stop_surfaces_not_stoppable_conflict_as_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runs_module, "resolve_credential", lambda: JWT)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "run_not_stoppable",
                        "message": "Auto-grouped agent runs cannot be stopped.",
                    }
                },
            )
        )
    ]

    result = runner.invoke(app, _run_args("stop", STORAGE_ID, json_output=True))

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "run_not_stoppable"
