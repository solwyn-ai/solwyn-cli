"""Budget check guardrail command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "e5" * 32
JWT_ACCESS = "budget-check-access-token"
JWT_REFRESH = "budget-check-refresh-token"
BASE_ARGS = [
    "budget",
    "check",
    "--estimated-input-tokens",
    "1200",
    "--model",
    "gpt-5",
    "--provider",
    "openai",
]
ALLOW_RESPONSE = {
    "allowed": True,
    "remaining_budget": 81.25,
    "reservation_id": "res_01JZEXAMPLE",
    "mode": "hard_deny",
    "budget_limit": 100.0,
    "current_usage": 18.75,
    "denied_by_period": None,
    "project_id": "proj_from_key",
    "price_hints": {"openai": 1.0, "anthropic": 1.15},
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


def _credential(source: str = "environment") -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _queue(**overrides: object) -> None:
    FakeClient.responses = [httpx.Response(200, json={**ALLOW_RESPONSE, **overrides})]


def test_budget_group_registers_check_and_confirm_without_regressing_commands() -> None:
    result = runner.invoke(app, ["budget", "--help"])

    assert result.exit_code == 0
    assert "check" in result.stdout
    assert "status" in result.stdout
    assert "confirm" in result.stdout
    assert "set" in result.stdout


@pytest.mark.parametrize("source", ["environment", "keyring"])
def test_budget_check_posts_exact_call_shaped_request_with_project_key(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    credential = _credential(source)
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue()

    result = runner.invoke(
        app,
        [
            "--json",
            *BASE_ARGS,
            "--fallback-provider",
            "anthropic",
            "--fallback-model",
            "claude-sonnet-4",
            "--fallback-provider",
            "google",
            "--fallback-model",
            "gemini-2.5-pro",
        ],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [
        (
            "POST",
            "/api/v1/budgets/check",
            {
                "json": {
                    "estimated_input_tokens": 1200,
                    "model": "gpt-5",
                    "provider": "openai",
                    "fallback_providers": ["anthropic", "google"],
                    "fallback_models": ["claude-sonnet-4", "gemini-2.5-pro"],
                }
            },
        )
    ]


def test_budget_check_rejects_jwt_locally_with_project_key_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=JWT_ACCESS,
        refresh_token=JWT_REFRESH,
    )
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: credential, raising=False
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert FakeClient.instances == []
    error = json.loads(result.stderr)
    assert error["code"] == "project_key_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert JWT_ACCESS not in result.stderr
    assert JWT_REFRESH not in result.stderr


def test_budget_check_rejects_missing_auth_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: None, raising=False
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 3
    assert FakeClient.instances == []
    assert "SOLWYN_API_KEY" in json.loads(result.stderr)["hint"]


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--fallback-provider", "anthropic"],
        ["--fallback-model", "claude-sonnet-4"],
        [
            *sum(
                (
                    ["--fallback-provider", "openai", "--fallback-model", f"m{i}"]
                    for i in range(9)
                ),
                [],
            )
        ],
        ["--model", "m" * 2049],
        ["--fallback-provider", "openai", "--fallback-model", "m" * 2049],
    ],
)
def test_budget_check_rejects_invalid_fallback_or_model_before_auth_and_network(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS, *extra_args])

    assert result.exit_code == 2
    assert resolution_calls == []
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["budget", "check", "--model", "gpt-5", "--provider", "openai"],
        [
            "budget",
            "check",
            "--estimated-input-tokens",
            "-1",
            "--model",
            "gpt-5",
            "--provider",
            "openai",
        ],
        [
            "budget",
            "check",
            "--estimated-input-tokens",
            "1",
            "--model",
            "gpt-5",
            "--provider",
            "not-a-provider",
        ],
    ],
)
def test_budget_check_required_and_enum_options_fail_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
        raising=False,
    )

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    ("case", "response_overrides", "expected_exit", "expected_stderr"),
    [
        ("allow", {}, 0, ""),
        (
            "deny",
            {
                "allowed": False,
                "reservation_id": None,
                "denied_by_period": "monthly",
            },
            4,
            "",
        ),
        (
            "fail-open",
            {"reservation_id": None},
            0,
            "fail-open, unreserved — skip confirm\n",
        ),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_check_outcomes_match_goldens_and_stream_contract(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    response_overrides: dict[str, object],
    expected_exit: int,
    expected_stderr: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue(**response_overrides)
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*output_options, *BASE_ARGS])

    assert result.exit_code == expected_exit
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-check-{case}-{output_format}.{suffix}",
    )
    assert result.stderr == expected_stderr
    if case == "allow":
        assert "res_01JZEXAMPLE" in result.stdout
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["schema"] == "solwyn.budget-check.v1"
        assert "reservation_id" in payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed": False, "reservation_id": "res_impossible"},
        {
            "allowed": False,
            "reservation_id": None,
            "denied_by_period": "monthly",
            "mode": "alert_only",
        },
        {"allowed": True, "denied_by_period": "monthly"},
        {"remaining_budget": "NaN"},
        {"budget_limit": "Infinity"},
        {"current_usage": "-Infinity"},
        {"price_hints": {"openai": "NaN"}},
        {"reservation_id": ""},
        {"mode": "unknown"},
        {"denied_by_period": "yearly"},
    ],
)
def test_budget_check_rejects_invalid_or_inconsistent_success_response(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue(**overrides)

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_check_response"


def test_budget_check_preserves_structured_api_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                422,
                json={
                    "detail": {
                        "code": "unknown_model",
                        "message": "Model is not priced.",
                    }
                },
            )
        )
    ]

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "unknown_model"


def test_budget_check_never_leaks_credentials_to_output_exception_or_goldens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    rendered = result.stdout + result.stderr + repr(result.exception)
    assert VALID_KEY not in rendered
    for golden in GOLDEN_DIR.glob("budget-check-*"):
        assert VALID_KEY not in golden.read_text(encoding="utf-8")
