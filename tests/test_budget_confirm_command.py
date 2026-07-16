"""Budget confirmation guardrail command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli._vendor import wire as wire_module
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "f6" * 32
JWT_ACCESS = "budget-confirm-access-token"
JWT_REFRESH = "budget-confirm-refresh-token"
BASE_ARGS = [
    "budget",
    "confirm",
    "--reservation-id",
    "res_01JZEXAMPLE",
    "--call-id",
    "12345678-1234-1234-1234-123456789abc",
    "--model",
    "claude-sonnet-4",
    "--provider",
    "bedrock",
]
ALL_TOKEN_ARGS = [
    "--input-tokens",
    "11",
    "--output-tokens",
    "12",
    "--cached-input-tokens",
    "13",
    "--cache-creation-5m-tokens",
    "14",
    "--cache-creation-1h-tokens",
    "15",
    "--reasoning-tokens",
    "16",
    "--audio-input-tokens",
    "17",
    "--audio-output-tokens",
    "18",
    "--accepted-prediction-tokens",
    "19",
    "--rejected-prediction-tokens",
    "20",
    "--tool-use-input-tokens",
    "21",
    "--is-estimated",
]
TOKEN_COUNT_FLAGS = [
    "--input-tokens",
    "--output-tokens",
    "--cached-input-tokens",
    "--cache-creation-5m-tokens",
    "--cache-creation-1h-tokens",
    "--reasoning-tokens",
    "--audio-input-tokens",
    "--audio-output-tokens",
    "--accepted-prediction-tokens",
    "--rejected-prediction-tokens",
    "--tool-use-input-tokens",
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
    monkeypatch.setattr(budget_module, "SolwynClient", FakeClient, raising=False)
    yield


def _credential(source: str = "environment") -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _queue_success(status_code: int = 204) -> None:
    FakeClient.responses = [httpx.Response(status_code)]


def test_budget_group_registers_confirm_without_regressing_other_commands() -> None:
    result = runner.invoke(app, ["budget", "--help"])

    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "check" in result.stdout
    assert "confirm" in result.stdout
    assert "set" in result.stdout


def test_budget_confirm_uses_the_vendored_service_tier_domain() -> None:
    assert budget_module.ServiceTier is wire_module.ServiceTier


@pytest.mark.parametrize("source", ["environment", "keyring"])
def test_budget_confirm_posts_every_token_detail_with_project_key(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    credential = _credential(source)
    monkeypatch.setattr(
        budget_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_success()

    result = runner.invoke(
        app,
        [
            "--json",
            *BASE_ARGS,
            "--is-provider-fallback",
            "--provider-region",
            "us-east-1",
            "--service-tier",
            "optimized",
            *ALL_TOKEN_ARGS,
        ],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [
        (
            "POST",
            "/api/v1/budgets/confirm",
            {
                "json": {
                    "reservation_id": "res_01JZEXAMPLE",
                    "model": "claude-sonnet-4",
                    "provider": "bedrock",
                    "is_provider_fallback": True,
                    "call_id": "12345678-1234-1234-1234-123456789abc",
                    "token_details": {
                        "input_tokens": 11,
                        "output_tokens": 12,
                        "cached_input_tokens": 13,
                        "cache_creation_5m_tokens": 14,
                        "cache_creation_1h_tokens": 15,
                        "reasoning_tokens": 16,
                        "audio_input_tokens": 17,
                        "audio_output_tokens": 18,
                        "accepted_prediction_tokens": 19,
                        "rejected_prediction_tokens": 20,
                        "tool_use_input_tokens": 21,
                        "is_estimated": True,
                    },
                    "provider_region": "us-east-1",
                    "service_tier": "optimized",
                },
                "retry_rate_limit": False,
            },
        )
    ]


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_confirm_success_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue_success()
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*output_options, *BASE_ARGS])

    assert result.exit_code == 0
    assert result.stderr == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-confirm-success-{output_format}.{suffix}",
    )
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload == {
            "schema": "solwyn.budget-confirm.v1",
            "reservation_id": "res_01JZEXAMPLE",
            "call_id": "12345678-1234-1234-1234-123456789abc",
        }


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_confirm_missing_reservation_explains_fail_open_and_skipping(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
        raising=False,
    )
    args = [
        "budget",
        "confirm",
        "--call-id",
        "call-id",
        "--model",
        "gpt-5",
        "--provider",
        "openai",
    ]
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*output_options, *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr,
        GOLDEN_DIR / f"budget-confirm-missing-reservation-{output_format}.{suffix}",
    )
    assert "reservation_id=null" in result.stderr
    assert "fail-open" in result.stderr
    assert "skip confirm" in result.stderr


def test_budget_confirm_read_only_key_403_exits_auth_with_an_actionable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "read_only_key",
                        "message": "This project key is read-only.",
                    }
                },
            )
        )
    ]

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "read_only_key"
    assert error["hint"] == (
        "Set SOLWYN_API_KEY to a project key with write scope, or store one "
        "with solwyn login --with-key."
    )


def test_budget_confirm_plain_403_exits_auth_without_a_read_only_key_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        error_from_response(httpx.Response(403, json={"detail": "Forbidden."}))
    ]

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["hint"] is None


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_confirm_expired_or_consumed_404_is_actionable_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    first_error = error_from_response(
        httpx.Response(
            404,
            json={
                "detail": {
                    "code": "reservation_not_found",
                    "message": "Budget reservation was not found.",
                    "reservation_state": "expired_or_consumed",
                }
            },
        )
    )
    FakeClient.responses = [first_error, RuntimeError("must not retry")]
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*output_options, *BASE_ARGS])

    assert result.exit_code == 5
    assert result.stdout == ""
    assert len(FakeClient.instances) == 1
    assert len(FakeClient.instances[0].requests) == 1
    assert len(FakeClient.responses) == 1
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr,
        GOLDEN_DIR / f"budget-confirm-expired-{output_format}.{suffix}",
    )
    assert "fresh check" in result.stderr
    if output_format == "json":
        error = json.loads(result.stderr)
        assert error["code"] == "reservation_not_found"
        assert error["message"] == "Budget reservation was not found."
        assert error["detail"]["reservation_state"] == "expired_or_consumed"


@pytest.mark.parametrize(
    "credential",
    [
        auth.JwtCredential(access_token=JWT_ACCESS, refresh_token=JWT_REFRESH),
        None,
    ],
)
def test_budget_confirm_refuses_jwt_or_missing_auth_without_request(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.JwtCredential | None,
) -> None:
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
    assert "--with-key" in error["hint"]
    rendered = result.stderr + repr(result.exception)
    assert JWT_ACCESS not in rendered
    assert JWT_REFRESH not in rendered


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--reservation-id", ""],
        ["--model", "m" * 2049],
        ["--call-id", "c" * 37],
        ["--provider-region", "r" * 33],
        ["--call-id", ""],
        ["--model", ""],
        ["--provider-region", ""],
        ["--service-tier", "unknown"],
        ["--provider", "unknown"],
    ],
)
def test_budget_confirm_rejects_invalid_input_before_auth_and_network(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> None:
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
        raising=False,
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS, *extra_args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.parametrize("flag", TOKEN_COUNT_FLAGS)
@pytest.mark.parametrize("value", ["-1", "100000001"])
def test_budget_confirm_enforces_every_token_count_bound_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: pytest.fail("must not resolve auth"),
        raising=False,
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS, flag, value])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        [
            "budget",
            "confirm",
            "--reservation-id",
            "reservation",
            "--model",
            "gpt-5",
            "--provider",
            "openai",
        ],
        [
            "budget",
            "confirm",
            "--reservation-id",
            "reservation",
            "--call-id",
            "call-id",
            "--provider",
            "openai",
        ],
        [
            "budget",
            "confirm",
            "--reservation-id",
            "reservation",
            "--call-id",
            "call-id",
            "--model",
            "gpt-5",
        ],
    ],
)
def test_budget_confirm_requires_call_model_and_provider_before_auth(
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


@pytest.mark.parametrize("status_code", [200, 201, 202, 205])
def test_budget_confirm_accepts_only_204_success(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue_success(status_code)

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_confirm_response"


def test_budget_confirm_rejects_a_body_on_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [httpx.Response(204, content=b"unexpected")]

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_confirm_response"


def test_budget_confirm_preserves_non_404_api_errors(
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
    error = json.loads(result.stderr)
    assert error["code"] == "unknown_model"
    assert error["message"] == "Model is not priced."


def test_budget_confirm_never_leaks_credentials_to_output_exception_or_goldens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue_success()

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    rendered = result.stdout + result.stderr + repr(result.exception)
    assert VALID_KEY not in rendered
    for golden in GOLDEN_DIR.glob("budget-confirm-*"):
        assert VALID_KEY not in golden.read_text(encoding="utf-8")
