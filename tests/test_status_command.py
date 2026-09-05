"""Status command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli import runtime
from solwyn_cli.commands import status as status_module
from solwyn_cli.config import Config
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "b2" * 32
ACCESS_TOKEN = "status-access-token"
REFRESH_TOKEN = "status-refresh-token"

BUDGET_RESPONSE = {
    "project_id": "proj_alpha",
    "budget_limit": 100.0,
    "current_usage": 37.5,
    "remaining_budget": 62.5,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "period_date": "2026-07",
    "outage_overspend_ceiling_usd": 0.0,
    "utilization_pct": 37.5,
    "thresholds": [],
    "sub_period": None,
    "sub_limit": None,
    "constraints": {
        "provider_allowlist": None,
        "model_allowlist": None,
        "blocked_providers": [],
        "blocked_models": [],
    },
}
PROVIDERS_RESPONSE = [
    {
        "provider": "openai",
        "total_calls": 100,
        "success_count": 98,
        "error_count": 2,
        "failover_count": 1,
        "model_failover_count": 1,
        "provider_failover_count": 0,
        "success_rate": 98.0,
        "avg_latency_ms": 245.5,
        "circuit_state": "closed",
        "circuit_source": "reported",
    },
    {
        "provider": "anthropic",
        "total_calls": 50,
        "success_count": 40,
        "error_count": 10,
        "failover_count": 3,
        "model_failover_count": 1,
        "provider_failover_count": 2,
        "success_rate": 80.0,
        "avg_latency_ms": 410.0,
        "circuit_state": "open",
        "circuit_source": "inferred",
    },
]

FORECAST_RESPONSE = {
    "current_spend": 12.5,
    "forecast_eom": 75.0,
    "burn_rate_per_day": 2.5,
    "ci_low": 60.0,
    "ci_high": 90.0,
    "period_start": "2026-09-01T00:00:00+00:00",
    "period_end": "2026-09-30T23:59:59.999999+00:00",
    "days_elapsed": 5,
    "days_remaining": 25,
    "insufficient_data": False,
}

FAILOVERS_RESPONSE = [
    {
        "requested_provider": "openai",
        "requested_model": "gpt-5",
        "served_provider": "anthropic",
        "served_model": "claude-sonnet-4",
        "failover_reason": "circuit_open",
        "failover_error_class": None,
        "event_count": 4,
        "total_cost_usd": 1.234567,
        "first_seen": "2026-09-01T10:00:00Z",
        "last_seen": "2026-09-05T12:30:00Z",
        "is_unpriced": False,
    }
]


class FakeClient:
    """Minimal closeable command client with queued request results."""

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
    monkeypatch.setattr(status_module, "SolwynClient", FakeClient, raising=False)
    yield


def _queue_status_responses(
    budget: dict[str, Any] | None = None,
    providers: list[dict[str, Any]] | None = None,
) -> None:
    FakeClient.responses = [
        httpx.Response(200, json=budget or BUDGET_RESPONSE),
        httpx.Response(200, json=providers or PROVIDERS_RESPONSE),
    ]


def test_status_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "status" in result.stdout


@pytest.mark.parametrize(
    "credential",
    [
        auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
        auth.JwtCredential(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
        ),
    ],
)
def test_status_supports_bare_key_and_jwt_with_two_read_only_gets(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_status_responses()

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "status"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.api_url == "https://api.solwyn.ai"
    assert client.requests == [
        ("GET", "/api/v1/projects/proj_alpha/budget", {}),
        ("GET", "/api/v1/projects/proj_alpha/providers", {}),
    ]
    assert all(method == "GET" for method, _, _ in client.requests)
    assert_matches_golden(result.stdout, GOLDEN_DIR / "status.json")
    payload = json.loads(result.stdout)
    assert "forecast" not in payload
    assert "failovers" not in payload
    assert result.stderr == ""
    rendered = result.stdout + result.stderr + repr(result.exception)
    for secret in (VALID_KEY, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in rendered


def test_status_non_tty_table_and_no_color_match_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_status_responses()

    result = runner.invoke(
        app,
        ["--no-color", "--project", "proj_alpha", "status"],
    )

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "status-table.txt")
    assert result.stderr == ""
    assert "\x1b" not in result.stdout


def test_status_honors_environment_json_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="keyring")
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_status_responses()

    result = runner.invoke(
        app,
        ["--project", "proj_alpha", "status"],
        env={"SOLWYN_FORMAT": "json"},
    )

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "status.json")


def test_status_optional_forecast_and_failover_shortcuts_add_reads_and_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=PROVIDERS_RESPONSE),
        httpx.Response(200, json=FORECAST_RESPONSE),
        httpx.Response(200, json=FAILOVERS_RESPONSE),
    ]

    result = runner.invoke(
        app,
        ["--no-color", "-p", "proj_alpha", "status", "--forecast", "--failovers"],
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_alpha/budget", {}),
        ("GET", "/api/v1/projects/proj_alpha/providers", {}),
        (
            "GET",
            "/api/v1/projects/proj_alpha/forecast",
            {"params": {"period": "mtd"}},
        ),
        (
            "GET",
            "/api/v1/projects/proj_alpha/providers/failovers",
            {"params": {"range": "7d"}},
        ),
    ]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "status-flags-table.txt")
    assert result.stderr == "Showing top 50 groups of an unknown total.\n"


def test_status_optional_sections_are_schema_bearing_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_module,
        "resolve_credential",
        lambda: auth.JwtCredential(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
        ),
        raising=False,
    )
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=PROVIDERS_RESPONSE),
        httpx.Response(200, json=FORECAST_RESPONSE),
        httpx.Response(200, json=FAILOVERS_RESPONSE),
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_alpha", "status", "--forecast", "--failovers"],
    )

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "status-flags.json")
    assert result.stderr == "Showing top 50 groups of an unknown total.\n"


@pytest.mark.parametrize(
    ("options", "environment_project", "configured_project", "expected_project"),
    [
        (["--project", "proj_flag"], "proj_environment", "proj_config", "proj_flag"),
        ([], "proj_environment", "proj_config", "proj_environment"),
        ([], "", "proj_config", "proj_config"),
    ],
)
def test_status_project_precedence_is_flag_environment_then_config(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    environment_project: str,
    configured_project: str,
    expected_project: str,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(default_project=configured_project),
    )
    _queue_status_responses(budget={**BUDGET_RESPONSE, "project_id": expected_project})

    result = runner.invoke(
        app,
        ["--json", *options, "status"],
        env={"SOLWYN_PROJECT_ID": environment_project},
    )

    assert result.exit_code == 0
    paths = [path for _, path, _ in FakeClient.instances[0].requests]
    assert paths == [
        f"/api/v1/projects/{expected_project}/budget",
        f"/api/v1/projects/{expected_project}/providers",
    ]
    assert json.loads(result.stdout)["project_id"] == expected_project


def test_status_missing_project_is_safe_usage_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(runtime, "load_config", lambda: Config(default_project=None))
    monkeypatch.setattr(
        status_module,
        "resolve_credential",
        lambda: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(
        app,
        ["--json", "status"],
        env={"SOLWYN_PROJECT_ID": ""},
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "project_required"
    assert "--project" in error["hint"]
    assert "SOLWYN_PROJECT_ID" in error["hint"]
    assert "config" in error["hint"]
    assert resolution_calls == []
    assert FakeClient.instances == []


def test_status_missing_credentials_is_auth_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: None, raising=False
    )

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "status"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert FakeClient.instances == []


def test_status_preserves_structured_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    FakeClient.responses = [
        ApiError(
            status_code=404,
            code="project_not_found",
            message="Project not found.",
            hint=None,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    ]

    result = runner.invoke(app, ["--json", "-p", "proj_missing", "status"])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert len(FakeClient.instances[0].requests) == 1


def test_status_rejects_invalid_response_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )
    monkeypatch.setattr(
        status_module, "resolve_credential", lambda: credential, raising=False
    )
    invalid_budget = dict(BUDGET_RESPONSE)
    invalid_budget.pop("remaining_budget")
    _queue_status_responses(budget=invalid_budget)

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "status"])

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_status_response"
    rendered = result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


def test_status_goldens_never_contain_synthetic_credentials() -> None:
    for golden in GOLDEN_DIR.glob("status*"):
        rendered = golden.read_text(encoding="utf-8")
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered
        assert REFRESH_TOKEN not in rendered
