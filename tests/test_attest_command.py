"""Spend-control attestation command contract tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from golden.harness import assert_matches_golden
from key_fixtures import same_credential
from login_fixtures import login_credential
from typer.testing import CliRunner

from solwyn_cli import auth, runtime
from solwyn_cli.commands import attest as attest_module
from solwyn_cli.config import Config
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "d4" * 32


class FakeClient:
    """Stream-only command client with queued results."""

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

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: Any):
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        try:
            yield result
        finally:
            result.close()


def _key(**_: object) -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


def _jwt() -> auth.LoginCredential:
    return login_credential("attestation-access-token")


def _window_headers() -> dict[str, str]:
    return {
        "X-Solwyn-Requested-From": "2026-08-01T00:00:00+00:00",
        "X-Solwyn-Requested-To": "2026-08-31T00:00:00+00:00",
        "X-Solwyn-Effective-From": "2026-08-02T00:00:00+00:00",
        "X-Solwyn-Effective-To": "2026-08-31T00:00:00+00:00",
        "X-Solwyn-Retention-Clamped": "true",
    }


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(attest_module, "SolwynClient", FakeClient, raising=False)
    yield


def test_attest_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "attest" in result.stdout


@pytest.mark.parametrize("credential", [_key(), _jwt()])
def test_project_attestation_streams_exact_json_and_reports_window_headers(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", lambda **_: credential)
    expected = (GOLDEN_DIR / "attest-project-json.json").read_bytes()
    FakeClient.responses = [
        httpx.Response(200, content=expected, headers=_window_headers())
    ]

    result = runner.invoke(
        app,
        ["--project", "proj_be76331b95dfc399cd776d2f", "attest", "--format", "json"],
    )

    assert result.exit_code == 0, result.exception
    assert_matches_golden(result.stdout, GOLDEN_DIR / "attest-project-json.json")
    assert result.stdout_bytes == expected
    assert (
        result.stderr
        == "\n".join(f"{name}: {value}" for name, value in _window_headers().items())
        + "\n"
    )
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/projects/proj_be76331b95dfc399cd776d2f/spend-control-attestation",
            {"params": {"range": "30d", "format": "json"}},
        )
    ]


def test_account_attestation_requires_jwt_and_streams_csv_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(attest_module, "resolve_credential", lambda **_: credential)
    expected = (GOLDEN_DIR / "attest-account-csv.txt").read_bytes()
    FakeClient.responses = [httpx.Response(200, content=expected)]

    result = runner.invoke(app, ["attest", "--account", "--format", "csv"])

    assert result.exit_code == 0, result.exception
    assert_matches_golden(result.stdout, GOLDEN_DIR / "attest-account-csv.txt")
    assert result.stdout_bytes == expected
    assert same_credential(FakeClient.instances[0].credential, credential)
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/spend-control-attestation",
            {"params": {"range": "30d", "format": "csv"}},
        )
    ]


def test_custom_window_replaces_range_in_attestation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, content=b"{}")]

    result = runner.invoke(
        app,
        [
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "attest",
            "--range",
            "365d",
            "--from",
            "2026-08-01T00:00:00+00:00",
            "--to",
            "2026-08-02T00:00:00+00:00",
        ],
    )

    assert result.exit_code == 0, result.exception
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "from": "2026-08-01T00:00:00+00:00",
        "to": "2026-08-02T00:00:00+00:00",
        "format": "json",
    }


@pytest.mark.parametrize(
    "options",
    [
        ["--range", "all"],
        ["--format", "xml"],
        ["--from", "2026-08-01T00:00:00+00:00"],
        [
            "--from",
            "2026-08-02T00:00:00+00:00",
            "--to",
            "2026-08-01T00:00:00+00:00",
        ],
        [
            "--from",
            "2026-08-01T00:00:00",
            "--to",
            "2026-08-02T00:00:00",
        ],
    ],
)
def test_attest_rejects_invalid_export_options_before_request(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", _key)

    result = runner.invoke(
        app, ["-p", "proj_be76331b95dfc399cd776d2f", "attest", *options]
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []


def test_project_attestation_requires_a_project_before_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(runtime, "load_config", lambda: Config())
    monkeypatch.setattr(
        attest_module, "resolve_credential", lambda **_: calls.append(None)
    )

    result = runner.invoke(
        app,
        ["--json", "attest"],
        env={"SOLWYN_PROJECT_ID": ""},
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_required"
    assert calls == []
    assert FakeClient.instances == []


def test_account_attestation_refuses_project_key_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", _key)

    result = runner.invoke(app, ["--json", "attest", "--account"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "solwyn login" in error["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize("export_format", ["json", "csv"])
def test_free_tier_402_is_preserved_for_both_attestation_formats(
    monkeypatch: pytest.MonkeyPatch,
    export_format: str,
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", _key)
    detail = {
        "code": "upgrade_required",
        "feature": "spend_control_attestation",
        "current_tier": "free",
        "required_tier": "production",
        "message": "Spend-control attestations require the Production tier or above",
        "allowed_values": None,
        "cta_path": "/billing",
    }
    FakeClient.responses = [
        error_from_response(httpx.Response(402, json={"detail": detail}))
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "attest",
            "--format",
            export_format,
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["message"] == detail["message"]
    assert error["detail"]["feature"] == "spend_control_attestation"
    assert error["detail"]["required_tier"] == "production"
    assert "/billing" in error["hint"]


def test_attestation_range_402_preserves_allowed_values_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attest_module, "resolve_credential", _key)
    detail = {
        "code": "upgrade_required",
        "feature": "spend_control_attestation.range",
        "current_tier": "production",
        "required_tier": "team",
        "message": "Range '90d' is not available on your tier",
        "allowed_values": ["7d", "30d"],
        "cta_path": "/billing",
    }
    FakeClient.responses = [
        error_from_response(httpx.Response(402, json={"detail": detail}))
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "attest", "--range", "90d"],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["detail"]["feature"] == "spend_control_attestation.range"
    assert error["detail"]["allowed_values"] == ["7d", "30d"]
    assert "Allowed values: 7d, 30d" in error["hint"]
