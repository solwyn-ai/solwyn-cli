"""Costs command contract tests."""

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth, runtime
from solwyn_cli.commands import costs as costs_module
from solwyn_cli.config import Config
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "c3" * 32
ACCESS_TOKEN = "costs-access-token"
REFRESH_TOKEN = "costs-refresh-token"

GROUPS = {
    "day": [
        {
            "group_key": "2026-07-09",
            "provider": None,
            "total_cost": 1.25,
            "total_input_tokens": 1200,
            "total_output_tokens": 340,
            "call_count": 4,
            "unpriced_call_count": 1,
            "local_call_count": 0,
        }
    ],
    "model": [
        {
            "group_key": "gpt-5-mini",
            "provider": "openai",
            "total_cost": 2.5,
            "total_input_tokens": 2400,
            "total_output_tokens": 680,
            "call_count": 8,
            "unpriced_call_count": 0,
            "local_call_count": 2,
        }
    ],
    "provider": [
        {
            "group_key": "anthropic",
            "provider": "anthropic",
            "total_cost": 3.75,
            "total_input_tokens": 3600,
            "total_output_tokens": 1020,
            "call_count": 12,
            "unpriced_call_count": 0,
            "local_call_count": 0,
        }
    ],
}


def _response(
    *,
    project_id: str = "proj_alpha",
    time_range: str = "7d",
    group_by: str = "model",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "range": time_range,
        "group_by": group_by,
        "groups": copy.deepcopy(GROUPS[group_by]),
        "truncated": False,
        "savings": None,
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
    monkeypatch.setattr(costs_module, "SolwynClient", FakeClient, raising=False)
    yield


def _queue_costs_response(**kwargs: str) -> None:
    FakeClient.responses = [httpx.Response(200, json=_response(**kwargs))]


def _credential() -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")


def test_costs_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "costs" in result.stdout


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
def test_costs_supports_bare_key_and_jwt_with_one_read_only_get(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda: credential, raising=False
    )
    _queue_costs_response()

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "costs"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.api_url == "https://api.solwyn.ai"
    assert client.requests == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs",
            {"params": {"range": "7d", "group_by": "model"}},
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    for secret in (VALID_KEY, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in rendered


@pytest.mark.parametrize("time_range", ["7d", "30d", "90d", "365d"])
@pytest.mark.parametrize("group_by", ["day", "model", "provider"])
def test_costs_sends_every_frozen_range_and_group_value(
    monkeypatch: pytest.MonkeyPatch,
    time_range: str,
    group_by: str,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)
    _queue_costs_response(time_range=time_range, group_by=group_by)

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_alpha",
            "costs",
            "--range",
            time_range,
            "--group-by",
            group_by,
        ],
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[0][2] == {
        "params": {"range": time_range, "group_by": group_by}
    }


@pytest.mark.parametrize("group_by", ["day", "model", "provider"])
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_costs_grouping_shapes_match_goldens(
    monkeypatch: pytest.MonkeyPatch,
    group_by: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)
    _queue_costs_response(group_by=group_by)
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *output_options,
            "-p",
            "proj_alpha",
            "costs",
            "--group-by",
            group_by,
        ],
    )

    assert result.exit_code == 0
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"costs-{group_by}-{output_format}.{suffix}",
    )
    assert result.stderr == ""
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize(
    ("options", "environment_project", "configured_project", "expected_project"),
    [
        (["--project", "proj_flag"], "proj_environment", "proj_config", "proj_flag"),
        ([], "proj_environment", "proj_config", "proj_environment"),
        ([], "", "proj_config", "proj_config"),
    ],
)
def test_costs_project_precedence_is_flag_environment_then_config(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    environment_project: str,
    configured_project: str,
    expected_project: str,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(default_project=configured_project),
    )
    _queue_costs_response(project_id=expected_project)

    result = runner.invoke(
        app,
        ["--json", *options, "costs"],
        env={"SOLWYN_PROJECT_ID": environment_project},
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[0][1] == (
        f"/api/v1/projects/{expected_project}/costs"
    )


def test_costs_missing_project_is_safe_usage_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(runtime, "load_config", lambda: Config(default_project=None))
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(
        app,
        ["--json", "costs"],
        env={"SOLWYN_PROJECT_ID": ""},
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "project_required"
    assert "--project" in error["hint"]
    assert "SOLWYN_PROJECT_ID" in error["hint"]
    assert "config" in error["hint"]
    assert resolution_calls == []
    assert FakeClient.instances == []


def test_costs_missing_credentials_is_auth_error_without_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", lambda: None, raising=False)

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "costs"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "option",
    [
        ["--range", "1d"],
        ["--group-by", "service"],
    ],
)
def test_costs_rejects_non_frozen_options_before_request(option: list[str]) -> None:
    result = runner.invoke(app, ["-p", "proj_alpha", "costs", *option])

    assert result.exit_code == 2
    assert FakeClient.instances == []


def test_costs_relays_production_gated_402_for_free_project_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential()
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda: credential, raising=False
    )
    detail = {
        "code": "upgrade_required",
        "message": "The 365d range requires a Founder plan.",
        "cta_path": "/settings/billing?source=cli-costs",
    }
    response = httpx.Response(
        402,
        json={"detail": detail},
        request=httpx.Request(
            "GET", "https://api.solwyn.ai/api/v1/projects/proj_alpha/costs"
        ),
    )
    FakeClient.responses = [error_from_response(response)]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_alpha",
            "costs",
            "--range",
            "365d",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == detail["code"]
    assert error["message"] == detail["message"]
    assert error["detail"] == detail
    assert error["hint"] is not None
    assert detail["cta_path"] in error["hint"]
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs",
            {"params": {"range": "365d", "group_by": "model"}},
        )
    ]
    assert VALID_KEY not in result.stderr


def test_costs_rejects_invalid_response_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda: credential, raising=False
    )
    invalid = _response()
    invalid["groups"][0].pop("call_count")
    FakeClient.responses = [httpx.Response(200, json=invalid)]

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "costs"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_costs_response"
    rendered = result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


def test_costs_accepts_snapshot_optional_group_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)
    response = _response()
    group = dict(response["groups"][0])
    for optional_field in ("provider", "unpriced_call_count", "local_call_count"):
        group.pop(optional_field)
    response["groups"] = [group]
    FakeClient.responses = [httpx.Response(200, json=response)]

    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "costs"])

    assert result.exit_code == 0
    rendered_group = json.loads(result.stdout)["groups"][0]
    assert rendered_group["provider"] is None
    assert rendered_group["unpriced_call_count"] == 0
    assert rendered_group["local_call_count"] == 0


def test_costs_goldens_never_contain_synthetic_credentials() -> None:
    for golden in GOLDEN_DIR.glob("costs-*"):
        rendered = golden.read_text(encoding="utf-8")
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered
        assert REFRESH_TOKEN not in rendered


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_truncated_costs_note_stays_on_stderr(output_format, monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)
    response = _response()
    response["truncated"] = True
    response["groups"][0]["agent_run_name"] = "Research job"
    FakeClient.responses = [httpx.Response(200, json=response)]
    result = runner.invoke(
        app,
        [
            "--project",
            "proj_alpha",
            *(["--json"] if output_format == "json" else []),
            "costs",
        ],
    )
    assert result.exit_code == 0
    assert result.stderr == "showing top 100 groups\n"
    assert "showing top" not in result.stdout
    if output_format == "json":
        assert (
            json.loads(result.stdout)["groups"][0]["agent_run_name"] == "Research job"
        )
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR
        / f"costs-truncated-{output_format}.{'json' if output_format == 'json' else 'txt'}",
    )


def test_costs_rejects_missing_truncation_flag(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    response = _response()
    response.pop("truncated", None)
    FakeClient.responses = [httpx.Response(200, json=response)]
    result = runner.invoke(app, ["--json", "-p", "proj_alpha", "costs"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_costs_response"
