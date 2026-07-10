"""Budget status command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth, runtime
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.config import Config
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "d4" * 32
ACCESS_TOKEN = "budget-access-token"
REFRESH_TOKEN = "budget-refresh-token"
BUDGET_RESPONSE = {
    "project_id": "proj_alpha",
    "budget_limit": 100.0,
    "current_usage": 75.0,
    "remaining_budget": 25.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "period_date": "2026-07",
    "utilization_pct": 75.0,
    "thresholds": [],
    "constraints": {
        "effective_limit": 100.0,
        "effective_period": "monthly",
    },
}


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
    monkeypatch.setattr(budget_module, "SolwynClient", FakeClient, raising=False)
    yield


def _queue_budget_response(**overrides: object) -> None:
    FakeClient.responses = [httpx.Response(200, json={**BUDGET_RESPONSE, **overrides})]


def _key() -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")


def test_budget_group_registers_status_and_check_only() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["budget", "--help"])

    assert root.exit_code == 0
    assert group.exit_code == 0
    assert "budget" in root.stdout
    assert "status" in group.stdout
    assert "check" in group.stdout
    for mutation in ("confirm", "set"):
        assert mutation not in group.stdout


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
def test_budget_status_supports_both_auth_modes_with_one_get_and_no_reservation(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_budget_response()

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "budget", "status"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.api_url == "https://api.solwyn.ai"
    assert client.credential is credential
    assert client.requests == [("GET", "/api/v1/projects/proj_alpha/budget", {})]
    assert "/budgets/check" not in repr(client.requests)
    rendered = result.stdout + result.stderr + repr(result.exception)
    for secret in (VALID_KEY, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in rendered


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_status_matches_stable_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    _queue_budget_response()
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*output_options, "-p", "proj_alpha", "budget", "status"],
    )

    assert result.exit_code == 0
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-status-{output_format}.{suffix}",
    )
    assert result.stderr == ""
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
@pytest.mark.parametrize(
    ("utilization", "threshold", "expected_exit"),
    [(74.99, 75.0, 0), (75.0, 75.0, 4), (75.01, 75.0, 4)],
)
def test_max_utilization_is_inclusive_and_always_renders_normal_output(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    utilization: float,
    threshold: float,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    _queue_budget_response(utilization_pct=utilization)
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *output_options,
            "-p",
            "proj_alpha",
            "budget",
            "status",
            "--max-utilization",
            str(threshold),
        ],
    )

    assert result.exit_code == expected_exit
    assert result.stdout
    assert result.stderr == ""
    if output_format == "json":
        assert json.loads(result.stdout)["schema"] == "solwyn.budget-status.v1"
    else:
        assert "Utilization" in result.stdout


def test_budget_status_without_threshold_exits_zero_at_full_utilization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    _queue_budget_response(utilization_pct=100.0)

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "budget", "status"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["utilization_pct"] == 100.0


@pytest.mark.parametrize(
    "threshold", ["-0.01", "100.01", "not-a-number", "nan", "inf", "-inf"]
)
def test_invalid_max_utilization_is_rejected_before_auth_or_request(
    monkeypatch: pytest.MonkeyPatch,
    threshold: str,
) -> None:
    def fail_if_resolved() -> None:
        raise AssertionError("credentials must not be resolved")

    monkeypatch.setattr(
        budget_module, "resolve_credential", fail_if_resolved, raising=False
    )

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_alpha",
            "budget",
            "status",
            "--max-utilization",
            threshold,
        ],
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []


@pytest.mark.parametrize("threshold", ["0", "100"])
def test_boundary_max_utilization_values_are_valid(
    monkeypatch: pytest.MonkeyPatch,
    threshold: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    _queue_budget_response(utilization_pct=50.0)

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_alpha",
            "budget",
            "status",
            "--max-utilization",
            threshold,
        ],
    )

    assert result.exit_code == (4 if threshold == "0" else 0)
    assert json.loads(result.stdout)["schema"] == "solwyn.budget-status.v1"


@pytest.mark.parametrize(
    ("options", "environment_project", "configured_project", "expected_project"),
    [
        (["--project", "proj_flag"], "proj_environment", "proj_config", "proj_flag"),
        ([], "proj_environment", "proj_config", "proj_environment"),
        ([], "", "proj_config", "proj_config"),
    ],
)
def test_budget_status_uses_global_project_precedence(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    environment_project: str,
    configured_project: str,
    expected_project: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(default_project=configured_project),
    )
    _queue_budget_response(project_id=expected_project)

    result = runner.invoke(
        app,
        ["--json", *options, "budget", "status"],
        env={"SOLWYN_PROJECT_ID": environment_project},
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests == [
        ("GET", f"/api/v1/projects/{expected_project}/budget", {})
    ]


def test_budget_status_missing_project_is_usage_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "load_config", lambda: Config(default_project=None))
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)

    result = runner.invoke(app, ["--json", "budget", "status"], env={})

    assert result.exit_code == 2
    assert FakeClient.instances == []
    error = json.loads(result.stderr)
    assert error["code"] == "project_required"
    assert "--project" in error["hint"]


def test_budget_status_missing_credentials_is_auth_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: None, raising=False
    )

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "budget", "status"])

    assert result.exit_code == 3
    assert FakeClient.instances == []
    assert json.loads(result.stderr)["code"] == "authentication_required"


def test_budget_status_preserves_structured_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                404,
                json={"detail": {"code": "project_not_found", "message": "Missing."}},
            )
        )
    ]

    result = runner.invoke(app, ["--json", "-p", "proj_missing", "budget", "status"])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_not_found"


def test_budget_status_rejects_invalid_response_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: credential, raising=False
    )
    invalid = dict(BUDGET_RESPONSE)
    invalid.pop("remaining_budget")
    FakeClient.responses = [httpx.Response(200, json=invalid)]

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "budget", "status"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


@pytest.mark.parametrize(
    "response_override",
    [
        {"project_id": "proj_other"},
        {"utilization_pct": "NaN"},
        {"utilization_pct": "Infinity"},
    ],
)
def test_budget_status_rejects_untrusted_project_or_non_finite_numbers(
    monkeypatch: pytest.MonkeyPatch,
    response_override: dict[str, object],
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _key, raising=False)
    _queue_budget_response(**response_override)

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "budget", "status"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"


def test_budget_status_help_names_max_utilization_as_ci_deploy_gate() -> None:
    result = runner.invoke(app, ["budget", "status", "--help"])

    assert result.exit_code == 0
    assert "CI deploy gate" in result.stdout


def test_budget_goldens_never_contain_synthetic_credentials() -> None:
    for golden in GOLDEN_DIR.glob("budget-status*"):
        rendered = golden.read_text(encoding="utf-8")
        for secret in (VALID_KEY, ACCESS_TOKEN, REFRESH_TOKEN):
            assert secret not in rendered
