"""Account overview command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from golden.harness import assert_matches_golden
from login_fixtures import login_credential
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.commands import overview as overview_module
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "e4" * 32
ACCESS_TOKEN = "overview-access-token"
REFRESH_TOKEN = "overview-refresh-token"
OVERVIEW_RESPONSE = {
    "total_spend": 42.5,
    "total_requests": 5100,
    "project_count": 3,
    "trend_vs_last_period": 0.125,
    "period_start": "2026-09-01",
    "period_end": "2026-09-30",
    "savings": {
        "saved_cost": 4.25,
        "saved_cost_upper": None,
        "saved_input_tokens": 12000,
        "blocked_call_count": 6,
    },
    "quota_exceeded": True,
    "included_requests": 5000,
    "current_requests": 5100,
    "window_start": "2026-09-01T00:00:00Z",
    "window_end": "2026-10-01T00:00:00Z",
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
    monkeypatch.setattr(overview_module, "SolwynClient", FakeClient, raising=False)
    yield


def test_overview_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "overview" in result.stdout


def test_overview_model_preserves_core_date_and_datetime_formats() -> None:
    properties = overview_module.OverviewResponse.model_json_schema()["properties"]

    assert properties["period_start"]["format"] == "date"
    assert properties["period_end"]["format"] == "date"
    assert properties["window_start"]["anyOf"][0]["format"] == "date-time"
    assert properties["window_end"]["anyOf"][0]["format"] == "date-time"


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_overview_free_quota_and_nullable_savings_match_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(
        overview_module, "resolve_credential", lambda **_: credential, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=OVERVIEW_RESPONSE)]

    result = runner.invoke(
        app,
        ["--format", output_format, "--no-color", "overview"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [("GET", "/api/v1/overview", {})]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"overview-{output_format}.{suffix}",
    )
    assert result.stderr == ""
    rendered = result.stdout + repr(result.exception)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in rendered


def test_overview_project_key_stops_before_http_with_login_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        overview_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        raising=False,
    )

    result = runner.invoke(app, ["--json", "overview"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "solwyn login" in error["hint"]
    assert FakeClient.instances == []
    assert VALID_KEY not in result.stderr


def test_overview_paid_response_omits_inapplicable_nullable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        overview_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    paid = {
        **OVERVIEW_RESPONSE,
        "savings": None,
        "quota_exceeded": False,
        "included_requests": None,
        "current_requests": None,
        "window_start": None,
        "window_end": None,
    }
    FakeClient.responses = [httpx.Response(200, json=paid)]

    result = runner.invoke(app, ["--json", "overview"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "savings" not in payload
    assert "included_requests" not in payload
    assert "current_requests" not in payload
    assert "window_start" not in payload
    assert "window_end" not in payload
    assert_matches_golden(result.stdout, GOLDEN_DIR / "overview-paid-json.json")


def test_overview_invalid_response_is_fixed_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        overview_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json={"total_spend": ACCESS_TOKEN})]

    result = runner.invoke(app, ["--json", "overview"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "schema": "solwyn.error.v1",
        "code": "invalid_overview_response",
        "message": "The overview response was invalid.",
        "hint": None,
        "detail": None,
    }
    assert ACCESS_TOKEN not in result.stderr + repr(result.exception)
