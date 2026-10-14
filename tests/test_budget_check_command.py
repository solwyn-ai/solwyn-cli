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
    monkeypatch.setattr(
        budget_module, "uuid4", lambda: "12345678-1234-1234-1234-123456789abc"
    )
    yield


def _credential(source: str = "environment") -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _queue(**overrides: object) -> None:
    FakeClient.responses = [httpx.Response(200, json={**ALLOW_RESPONSE, **overrides})]
    if overrides.get("allowed") is False:
        FakeClient.responses.append(httpx.Response(202, json={"ingested": 1}))


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
                    "uncounted_calls": 0,
                    "uncounted_tokens": 0,
                    "price_hints_version": "1",
                    "estimated_input_tokens": 1200,
                    "model": "gpt-5",
                    "provider": "openai",
                    "fallback_providers": ["anthropic", "google"],
                    "fallback_models": ["claude-sonnet-4", "gemini-2.5-pro"],
                }
            },
        )
    ]


def test_budget_check_sends_agent_run_id_only_when_the_flag_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in field is on the wire when asked for, and absent otherwise."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS, "--agent-run-id", "run_42"])

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/budgets/check",
            {
                "json": {
                    "uncounted_calls": 0,
                    "uncounted_tokens": 0,
                    "price_hints_version": "1",
                    "estimated_input_tokens": 1200,
                    "model": "gpt-5",
                    "provider": "openai",
                    "fallback_providers": [],
                    "fallback_models": [],
                    "agent_run_id": "run_42",
                    "run_directive_version": "1",
                }
            },
        )
    ]


def test_budget_check_omits_agent_run_id_rather_than_posting_an_explicit_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset run id leaves the wire body byte-identical to a pre-flag check."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 0
    (_, _, kwargs) = FakeClient.instances[0].requests[0]
    assert "agent_run_id" not in kwargs["json"]
    assert kwargs["json"] == {
        "uncounted_calls": 0,
        "uncounted_tokens": 0,
        "price_hints_version": "1",
        "estimated_input_tokens": 1200,
        "model": "gpt-5",
        "provider": "openai",
        "fallback_providers": [],
        "fallback_models": [],
    }


def test_budget_check_accepts_null_omitting_directive_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping any optional response key must not turn an allow into an error."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "allowed": True,
                "remaining_budget": 81.25,
                "mode": "hard_deny",
                "budget_limit": 100.0,
                "current_usage": 18.75,
                "project_id": "proj_from_key",
            },
        )
    ]

    result = runner.invoke(
        app,
        ["--json", *BASE_ARGS, "--agent-run-id", "raw-run-42"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_control"] is None


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_check_run_stop_denial_emits_termination_directive_and_reason(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "allowed": False,
                "remaining_budget": 0.0,
                "mode": "hard_deny",
                "budget_limit": 100.0,
                "current_usage": 18.75,
                "denied_by_period": "run_stopped",
                "project_id": "proj_from_key",
                "run_control": {
                    "version": "1",
                    "action": "terminate",
                    "agent_run_id": "raw-run-42",
                    "reason": "manual_kill",
                },
            },
        )
    ]
    FakeClient.responses.append(httpx.Response(202, json={"ingested": 1}))
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*output_options, *BASE_ARGS, "--agent-run-id", "raw-run-42"],
    )

    assert result.exit_code == 4
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-check-run-stopped-{output_format}.{suffix}",
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    "run_control",
    [
        None,
        {
            "version": "1",
            "action": "terminate_run",
            "agent_run_id": "raw-run-42",
            "reason": "manual_kill",
        },
        {
            "version": "1",
            "action": "terminate",
            "agent_run_id": "raw-run-42",
            "reason": "operator_request",
        },
    ],
)
def test_budget_check_rejects_missing_or_invalid_opted_in_stop_directive(
    monkeypatch: pytest.MonkeyPatch,
    run_control: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "allowed": False,
                "remaining_budget": 0.0,
                "mode": "hard_deny",
                "budget_limit": 100.0,
                "current_usage": 18.75,
                "denied_by_period": "run_stopped",
                "project_id": "proj_from_key",
                **({} if run_control is None else {"run_control": run_control}),
            },
        )
    ]

    result = runner.invoke(
        app,
        ["--json", *BASE_ARGS, "--agent-run-id", "raw-run-42"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_check_response"


def test_budget_check_omits_estimated_media_for_a_text_only_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No media flag leaves the wire body byte-identical to a pre-flag check."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 0
    (_, _, kwargs) = FakeClient.instances[0].requests[0]
    assert "estimated_media" not in kwargs["json"]


def test_budget_check_posts_every_media_quantity_when_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(
        app,
        [
            "--json",
            *BASE_ARGS,
            "--image-count",
            "2",
            "--generation-count",
            "1",
            "--video-seconds",
            "8.5",
            "--audio-seconds",
            "12",
            "--input-characters",
            "480",
            "--resolution",
            "1024x1024",
            "--quality",
            "hd",
            "--is-estimated",
        ],
    )

    assert result.exit_code == 0
    (_, _, kwargs) = FakeClient.instances[0].requests[0]
    assert kwargs["json"]["estimated_media"] == {
        "image_count": 2,
        "generation_count": 1,
        "video_seconds": 8.5,
        "audio_seconds": 12.0,
        "input_characters": 480,
        "resolution": "1024x1024",
        "quality": "hd",
        "is_estimated": True,
    }


@pytest.mark.parametrize(
    ("extra_args", "expected_media"),
    [
        (["--video-seconds", "8"], {"video_seconds": 8.0, "is_estimated": False}),
        (["--image-count", "0"], {"image_count": 0, "is_estimated": False}),
        (["--is-estimated"], {"is_estimated": True}),
    ],
)
def test_budget_check_sends_only_the_media_quantities_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected_media: dict[str, object],
) -> None:
    """An unobservable quantity is omitted rather than sent as a zero."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS, *extra_args])

    assert result.exit_code == 0
    (_, _, kwargs) = FakeClient.instances[0].requests[0]
    assert kwargs["json"]["estimated_media"] == expected_media


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--image-count", "-1"],
        ["--generation-count", "-1"],
        ["--video-seconds", "-0.5"],
        ["--audio-seconds", "-0.5"],
        ["--input-characters", "-1"],
        ["--video-seconds", "nan"],
        ["--resolution", "r" * 33],
        ["--quality", "q" * 33],
    ],
)
def test_budget_check_rejects_invalid_media_values_before_auth_and_network(
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


def test_budget_check_rejects_an_over_long_agent_run_id_before_auth_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS, "--agent-run-id", "r" * 257])

    assert result.exit_code == 2
    assert resolution_calls == []
    assert FakeClient.instances == []
    assert json.loads(result.stderr)["code"] == "invalid_budget_check_request"


def test_budget_check_accepts_an_agent_run_id_at_the_exact_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue()

    result = runner.invoke(app, ["--json", *BASE_ARGS, "--agent-run-id", "r" * 256])

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[0][2]["json"]["agent_run_id"] == "r" * 256


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


def _queue_error(status_code: int, detail: object) -> None:
    FakeClient.responses = [
        error_from_response(httpx.Response(status_code, json={"detail": detail}))
    ]


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_check_read_only_key_403_exits_auth_with_an_actionable_hint(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue_error(
        403,
        {"code": "read_only_key", "message": "This project key is read-only."},
    )
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*output_options, *BASE_ARGS])

    assert result.exit_code == 3
    assert result.stdout == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr,
        GOLDEN_DIR / f"budget-check-read-only-key-{output_format}.{suffix}",
    )


def test_budget_check_plain_403_exits_auth_without_a_read_only_key_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 that is not about key scope must not claim the key is read-only."""
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue_error(403, "Forbidden.")

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "http_error"
    assert error["hint"] is None
    assert "read-only" not in result.stderr


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


@pytest.mark.parametrize("hints", [{}, None])
def test_price_column_is_absent_without_hints(hints, monkeypatch):
    monkeypatch.setattr(budget_module, "resolve_credential", _credential, raising=False)
    _queue(price_hints=hints)
    result = runner.invoke(app, BASE_ARGS)
    assert result.exit_code == 0
    assert "rel. price" not in result.stdout
