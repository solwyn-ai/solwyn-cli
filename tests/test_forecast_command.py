"""Forecast command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import forecast as forecast_module
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "f4" * 32
ACCESS_TOKEN = "forecast-access-token"
REFRESH_TOKEN = "forecast-refresh-token"

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
    "insufficient_data": True,
}


class FakeClient:
    """Minimal closeable command client with queued responses."""

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
    monkeypatch.setattr(forecast_module, "SolwynClient", FakeClient, raising=False)
    yield


def test_forecast_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "forecast" in result.stdout


@pytest.mark.parametrize(
    "credential",
    [
        auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
        auth.JwtCredential(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN),
    ],
)
def test_forecast_supports_key_and_jwt_with_one_mtd_get(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        forecast_module, "resolve_credential", lambda: credential, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=FORECAST_RESPONSE)]

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "forecast"])

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/forecast",
            {"params": {"period": "mtd"}},
        )
    ]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "forecast.json")
    assert result.stderr == ""


def test_forecast_table_shows_numbers_even_when_data_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        forecast_module,
        "resolve_credential",
        lambda: auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json=FORECAST_RESPONSE)]

    result = runner.invoke(app, ["--no-color", "-p", "proj_alpha", "forecast"])

    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "forecast-table.txt")
    assert "$75.000000" in result.stdout
    assert "$2.500000 / day" in result.stdout
    assert "$60.000000 - $90.000000" in result.stdout
    assert "Insufficient data" in result.stdout
    assert "yes" in result.stdout
    assert result.stderr == ""


def test_forecast_rejects_missing_project_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(
        forecast_module,
        "resolve_credential",
        lambda: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(app, ["--json", "forecast"], env={"SOLWYN_PROJECT_ID": ""})

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_required"
    assert resolution_calls == []
    assert FakeClient.instances == []


def test_forecast_invalid_or_extended_payload_fails_closed_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )
    monkeypatch.setattr(
        forecast_module, "resolve_credential", lambda: credential, raising=False
    )
    FakeClient.responses = [
        httpx.Response(200, json={**FORECAST_RESPONSE, "future_field": "unsafe"})
    ]

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "forecast"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_forecast_response"
    rendered = result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
