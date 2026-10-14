"""Doctor command diagnostic contract tests."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli import config as config_module
from solwyn_cli.commands import doctor as doctor_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "a1" * 32
CORE_REF = "690d61c0de1f11c6653f7a8a17800356f5009a8c"


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

    def _respond(
        self, method: str, path: str, kwargs: dict[str, Any]
    ) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._respond(method, path, kwargs)

    def request_unauthenticated(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        return self._respond(method, path, kwargs)


@pytest.fixture(autouse=True)
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(doctor_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(doctor_module, "_today", lambda: date(2026, 9, 5))
    monkeypatch.setattr(
        doctor_module,
        "_load_snapshot",
        lambda: {
            "x-solwyn-snapshot-date": "2026-09-05",
            "x-solwyn-core-ref": CORE_REF,
        },
    )
    monkeypatch.setattr(
        doctor_module.auth_module,
        "_get_password",
        lambda entry: (
            True,
            "stored"
            if entry in {auth.ACCESS_TOKEN_ENTRY, auth.REFRESH_TOKEN_ENTRY}
            else None,
        ),
    )
    yield


def _ready_response(*, redis_status: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "ready",
            "service": "solwyn-api",
            "checks": [
                {"name": "database", "status": "ok", "detail": None},
                {
                    "name": "redis",
                    "status": redis_status,
                    "detail": "cache failover" if redis_status == "degraded" else None,
                },
                {"name": "stripe_catalog", "status": "skipped", "detail": None},
            ],
        },
    )


def test_doctor_jwt_json_reports_all_checks_and_redis_degraded_is_non_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('default_project = "proj_alpha"\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.JwtCredential(
        access_token="access-secret", refresh_token="refresh-secret"
    )
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        _ready_response(redis_status="degraded"),
        httpx.Response(200, json={"user_id": "user_123"}),
    ]

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "doctor-jwt.json")
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [
        (
            "GET",
            "/health/ready",
            {"accepted_statuses": frozenset({503})},
        ),
        ("GET", "/api/v1/account/me", {}),
    ]
    assert "access-secret" not in result.stdout + result.stderr
    assert "refresh-secret" not in result.stdout + result.stderr


def test_doctor_key_table_keeps_503_readiness_body_informative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('default_project = "proj_alpha"\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        httpx.Response(
            503,
            json={
                "status": "degraded",
                "service": "solwyn-api",
                "checks": [
                    {
                        "name": "database",
                        "status": "degraded",
                        "detail": "connection refused",
                    },
                    {"name": "redis", "status": "degraded", "detail": "failover"},
                ],
            },
        ),
        httpx.Response(200, json={"project_id": "proj_alpha"}),
    ]

    result = runner.invoke(app, ["--no-color", "doctor"])

    assert result.exit_code == 1
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "doctor-degraded-table.txt")
    assert FakeClient.instances[0].requests[0][2] == {
        "accepted_statuses": frozenset({503})
    }


def test_doctor_key_budget_503_is_reported_as_backend_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('default_project = "proj_alpha"\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="keyring")
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        _ready_response(),
        ApiError(
            status_code=503,
            code="http_error",
            message="Budget backend temporarily unavailable; retry",
            hint=None,
            detail="Budget backend temporarily unavailable; retry",
        ),
    ]

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["authenticated_probe"] == {
        "status": "degraded",
        "credential_type": "project_key",
        "endpoint": "/api/v1/projects/proj_alpha/budget",
        "detail": "Budget backend temporarily unavailable; retry",
    }
    assert result.stderr == ""


def test_doctor_key_without_default_project_skips_auth_http_and_fails_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [_ready_response()]

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["authenticated_probe"]["status"] == "skipped"
    assert "default project" in payload["authenticated_probe"]["detail"].lower()
    assert [request[1] for request in FakeClient.instances[0].requests] == [
        "/health/ready"
    ]


def test_doctor_reports_malformed_config_instead_of_failing_in_root_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: None)
    FakeClient.responses = [_ready_response()]

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["config"] == {
        "status": "error",
        "detail": "Configuration could not be loaded.",
    }
    assert payload["authenticated_probe"]["status"] == "skipped"
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert "unterminated-sensitive-value" not in rendered


def test_doctor_malformed_config_never_sends_stored_credential_to_fallback_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.JwtCredential(
        access_token="access-secret", refresh_token="refresh-secret"
    )
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [_ready_response()]

    result = runner.invoke(
        app,
        ["--json", "doctor"],
        env={"SOLWYN_API_URL": ""},
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["config"]["status"] == "error"
    assert payload["authenticated_probe"] == {
        "status": "skipped",
        "credential_type": "jwt",
        "endpoint": None,
        "detail": (
            "Authenticated probe skipped because the API endpoint could not be "
            "loaded; provide --api-url or SOLWYN_API_URL."
        ),
    }
    client = FakeClient.instances[0]
    assert client.credential is None
    assert [request[1] for request in client.requests] == ["/health/ready"]
    assert "access-secret" not in result.stdout + result.stderr
    assert "refresh-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("global_args", "environment", "expected_url"),
    [
        (["--api-url", "https://explicit.example"], {}, "https://explicit.example"),
        (
            [],
            {"SOLWYN_API_URL": "https://environment.example"},
            "https://environment.example",
        ),
    ],
)
def test_doctor_malformed_config_uses_validated_override_for_authenticated_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_args: list[str],
    environment: dict[str, str],
    expected_url: str,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    credential = auth.JwtCredential(
        access_token="access-secret", refresh_token="refresh-secret"
    )
    monkeypatch.setattr(doctor_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        _ready_response(),
        httpx.Response(200, json={"user_id": "user_123"}),
    ]

    result = runner.invoke(app, [*global_args, "--json", "doctor"], env=environment)

    assert result.exit_code == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["config"]["status"] == "error"
    assert payload["authenticated_probe"]["status"] == "ok"
    client = FakeClient.instances[0]
    assert client.api_url == expected_url
    assert client.credential is credential
    assert [request[1] for request in client.requests] == [
        "/health/ready",
        "/api/v1/account/me",
    ]


def test_snapshot_diagnostic_marks_old_or_unpinned_snapshot_degraded() -> None:
    result = doctor_module._snapshot_diagnostic(
        {
            "x-solwyn-snapshot-date": "2026-07-01",
            "x-solwyn-core-ref": None,
        },
        today=date(2026, 9, 5),
    )

    assert result.status == "degraded"
    assert result.synced_on == "2026-07-01"
    assert result.age_days == 66
    assert result.core_ref is None


def test_keychain_diagnostic_rejects_an_incomplete_jwt_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_module.auth_module,
        "_get_password",
        lambda entry: (True, "stored" if entry == auth.ACCESS_TOKEN_ENTRY else None),
    )

    result = doctor_module._keychain_diagnostic()

    assert result.status == "degraded"
    assert result.detail == "Keychain contains an incomplete JWT credential."


@pytest.mark.parametrize("name", ["redis", "stripe_catalog", "stripe_portal"])
@pytest.mark.parametrize("output_format", ["json", "table"])
def test_doctor_labels_degraded_advisory_checks_non_gating(
    name: str, output_format: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('default_project = "proj_alpha"\n')
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    monkeypatch.setattr(
        doctor_module,
        "resolve_credential",
        lambda: auth.JwtCredential(
            access_token="test-access", refresh_token="test-refresh"
        ),
    )
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "status": "ready",
                "service": "solwyn-api",
                "checks": [
                    {"name": "database", "status": "ok", "detail": None},
                    {"name": name, "status": "degraded", "detail": "advisory"},
                ],
            },
        ),
        httpx.Response(200, json={"user_id": "user_123"}),
    ]
    result = runner.invoke(
        app, ["--json" if output_format == "json" else "--no-color", "doctor"]
    )
    assert result.exit_code == 0, result.stderr
    if output_format == "json":
        checks = json.loads(result.stdout)["readiness"]["checks"]
        assert checks[0]["gating"] is True
        assert checks[1]["gating"] is False
    else:
        assert f"Readiness {name} — advisory (non-gating)" in result.stdout
