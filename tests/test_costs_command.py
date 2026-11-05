"""Costs command contract tests."""

import copy
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
from test_entitlement import BILLING
from typer.testing import CliRunner

from solwyn_cli import auth, runtime
from solwyn_cli.commands import costs as costs_module
from solwyn_cli.commands._cost_models import AccountCostDailyPoint
from solwyn_cli.commands._entitlement import TierCapabilities
from solwyn_cli.config import Config
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "c3" * 32
ACCESS_TOKEN = "costs-access-token"
REFRESH_TOKEN = "costs-refresh-token"

WINDOW = {
    "requested_from": "2026-07-09T00:00:00Z",
    "requested_to": "2026-07-16T00:00:00Z",
    "effective_from": "2026-07-09T00:00:00Z",
    "effective_to": "2026-07-16T00:00:00Z",
    "retention_clamped": False,
}

ACCOUNT_RESPONSE = {
    "range": "7d",
    "group_by": "project",
    "groups": [
        {
            "group_key": "proj_be76331b95dfc399cd776d2f",
            "provider": None,
            "modality": None,
            "agent_run_name": None,
            "total_cost": 2.5,
            "total_input_tokens": 2400,
            "total_output_tokens": 680,
            "call_count": 8,
            "unpriced_call_count": 0,
            "local_call_count": 0,
            "project_name": "Alpha",
            "current_period_cost": 3.0,
            "daily": [
                {"day": "2026-07-09", "cost": 0.5},
                {"day": "2026-07-10", "cost": 2.0},
            ],
        }
    ],
    "truncated": False,
    "analytics_withheld": False,
    "savings": None,
    "window": WINDOW,
}

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
    project_id: str = "proj_be76331b95dfc399cd776d2f",
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
        "window": WINDOW,
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

    @property
    def current_credential(self):
        return self.credential

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: Any):
        response = self.request(method, path, **kwargs)
        try:
            yield response
        finally:
            response.close()

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
    monkeypatch.setattr(costs_module, "get_capabilities", lambda **kwargs: None)
    monkeypatch.setattr(costs_module, "SolwynClient", FakeClient, raising=False)
    yield


def _queue_costs_response(**kwargs: str) -> None:
    FakeClient.responses = [httpx.Response(200, json=_response(**kwargs))]


def _credential(**_: object) -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


def test_costs_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "costs" in result.stdout


def test_account_costs_defaults_to_project_rollup_without_a_project_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda **_: credential, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=ACCOUNT_RESPONSE)]

    result = runner.invoke(app, ["--json", "costs", "--account"])

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/costs",
            {"params": {"range": "7d", "group_by": "project"}},
        )
    ]
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solwyn.account_costs.v1"
    assert payload["groups"][0]["current_period_cost"] == 3.0


def test_account_daily_point_preserves_core_date_schema() -> None:
    schema = AccountCostDailyPoint.model_json_schema()

    assert schema["properties"]["day"]["format"] == "date"


@pytest.mark.parametrize(
    ("daily", "expected"),
    [
        ([], "—"),
        (
            [
                {"day": "2026-07-09", "cost": 0.0},
                {"day": "2026-07-10", "cost": 0.0},
            ],
            "▁▁",
        ),
    ],
)
def test_account_cost_sparkline_distinguishes_empty_and_all_zero_series(
    monkeypatch: pytest.MonkeyPatch,
    daily: list[dict[str, object]],
    expected: str,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    payload = copy.deepcopy(ACCOUNT_RESPONSE)
    payload["groups"][0]["daily"] = daily
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(app, ["--no-color", "costs", "--account"])

    assert result.exit_code == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_account_cost_project_rollup_and_sparkline_match_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json=ACCOUNT_RESPONSE)]

    result = runner.invoke(
        app,
        ["--format", output_format, "--no-color", "costs", "--account"],
    )

    assert result.exit_code == 0, result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"costs-account-{output_format}.{suffix}",
    )


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_account_costs_withheld_rows_never_render_analytic_zeros(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    withheld = copy.deepcopy(ACCOUNT_RESPONSE)
    withheld["analytics_withheld"] = True
    withheld["groups"][0].update(
        total_cost=0,
        total_input_tokens=0,
        total_output_tokens=0,
        call_count=0,
        daily=[],
    )
    withheld["savings"] = {
        "saved_cost": 1.25,
        "saved_cost_upper": None,
        "saved_input_tokens": 1000,
        "blocked_call_count": 2,
    }
    FakeClient.responses = [httpx.Response(200, json=withheld)]

    result = runner.invoke(
        app,
        ["--format", output_format, "--no-color", "costs", "--account"],
    )

    assert result.exit_code == 0, result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"costs-account-withheld-{output_format}.{suffix}",
    )
    assert "0.000000" not in result.stdout
    if output_format == "json":
        row = json.loads(result.stdout)["groups"][0]
        for field in (
            "total_cost",
            "total_input_tokens",
            "total_output_tokens",
            "call_count",
            "unpriced_call_count",
            "local_call_count",
            "daily",
        ):
            assert field not in row


def test_account_costs_forward_only_supported_filters_and_custom_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    payload = copy.deepcopy(ACCOUNT_RESPONSE)
    payload["group_by"] = "provider"
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(
        app,
        [
            "--json",
            "costs",
            "--account",
            "--group-by",
            "provider",
            "--from",
            "2026-07-01T00:00:00Z",
            "--to",
            "2026-07-02T00:00:00Z",
            "--model",
            "gpt-5",
            "--provider",
            "openai",
            "--modality",
            "text",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
        "model": "gpt-5",
        "provider": "openai",
        "modality": "text",
        "group_by": "provider",
    }


def test_account_costs_project_key_stops_before_http_with_login_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(costs_module, "resolve_credential", _credential, raising=False)

    result = runner.invoke(app, ["--json", "costs", "--account"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "solwyn login" in error["hint"]
    assert FakeClient.instances == []
    assert VALID_KEY not in result.stderr


@pytest.mark.parametrize(
    "option",
    [
        ["--status", "success"],
        ["--service-tier", "flex"],
        ["--provider-region", "us-east-1"],
        ["--agent-run", "run"],
        ["--failover-reason", "primary_error"],
        ["--model-fallback"],
        ["--provider-fallback"],
        ["--tag", "customer=example"],
    ],
)
def test_account_costs_reject_project_only_filters_before_http(
    monkeypatch: pytest.MonkeyPatch,
    option: list[str],
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )

    result = runner.invoke(app, ["--json", "costs", "--account", *option])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "invalid_cost_query"
    assert FakeClient.instances == []


def test_account_costs_print_core_window_headers_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    headers = {
        "X-Solwyn-Requested-From": "2026-07-01T00:00:00Z",
        "X-Solwyn-Requested-To": "2026-07-31T00:00:00Z",
        "X-Solwyn-Effective-From": "2026-07-02T00:00:00Z",
        "X-Solwyn-Effective-To": "2026-07-31T00:00:00Z",
        "X-Solwyn-Retention-Clamped": "true",
    }
    FakeClient.responses = [httpx.Response(200, json=ACCOUNT_RESPONSE, headers=headers)]

    result = runner.invoke(app, ["--json", "costs", "--account"])

    assert result.exit_code == 0, result.stderr
    for name, value in headers.items():
        assert f"{name}: {value}" in result.stderr


def test_account_bad_group_by_is_left_to_core_422_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    FakeClient.responses = [
        error_from_response(
            httpx.Response(422, json={"detail": "Invalid account group_by"})
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "costs", "--account", "--group-by", "tag:"],
    )

    assert result.exit_code == 2
    assert "Invalid account group_by" in result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/account/costs",
            {"params": {"range": "7d", "group_by": "tag:"}},
        )
    ]


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code", "expected_hint"),
    [
        (
            402,
            {
                "code": "quota_exceeded",
                "message": "Monthly request quota reached (5000/5000)",
                "cta_path": "/billing",
            },
            "quota_exceeded",
            "Review usage and plan limits in the dashboard: /billing",
        ),
        (
            403,
            "This endpoint requires JWT authentication",
            "http_error",
            "Run solwyn login to authenticate with your user account.",
        ),
    ],
)
def test_account_costs_keep_core_402_and_403_messages_safe(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    detail: dict[str, object] | str,
    expected_code: str,
    expected_hint: str,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    FakeClient.responses = [
        error_from_response(httpx.Response(status_code, json={"detail": detail}))
    ]

    result = runner.invoke(app, ["--json", "costs", "--account", "--group-by", "day"])

    assert result.exit_code == (1 if status_code == 402 else 3)
    error = json.loads(result.stderr)
    assert error["code"] == expected_code
    assert error["hint"] == expected_hint
    assert "checkout" not in result.stderr.lower()
    assert ACCESS_TOKEN not in result.stderr


@pytest.mark.parametrize(
    "credential",
    [
        auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        login_credential(ACCESS_TOKEN),
    ],
)
def test_costs_supports_bare_key_and_jwt_with_one_read_only_get(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda **_: credential, raising=False
    )
    _queue_costs_response()

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "costs"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert same_credential(client.credential, credential)
    assert client.api_url == "https://api.solwyn.ai"
    assert client.requests == [
        (
            "GET",
            "/api/v1/projects/proj_be76331b95dfc399cd776d2f/costs",
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
            "proj_be76331b95dfc399cd776d2f",
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
            "proj_be76331b95dfc399cd776d2f",
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
        (
            ["--project", "proj_112f3a99b283a4e1788dedd8"],
            "proj_c6e454d960df4845f9d69eb1",
            "proj_dfba7aade0868074c2861c98",
            "proj_112f3a99b283a4e1788dedd8",
        ),
        (
            [],
            "proj_c6e454d960df4845f9d69eb1",
            "proj_dfba7aade0868074c2861c98",
            "proj_c6e454d960df4845f9d69eb1",
        ),
        ([], "", "proj_dfba7aade0868074c2861c98", "proj_dfba7aade0868074c2861c98"),
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
        lambda **_: resolution_calls.append(None),
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
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda **_: None, raising=False
    )

    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs"]
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "option",
    [
        ["--group-by", "tag:"],
        ["--group-by", "tag:" + "x" * 65],
    ],
)
def test_costs_rejects_malformed_tag_group_before_request(option: list[str]) -> None:
    result = runner.invoke(
        app, ["-p", "proj_be76331b95dfc399cd776d2f", "costs", *option]
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []


def test_costs_relays_production_gated_402_for_free_project_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential()
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda **_: credential, raising=False
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
            "GET",
            "https://api.solwyn.ai/api/v1/projects/proj_be76331b95dfc399cd776d2f/costs",
        ),
    )
    FakeClient.responses = [error_from_response(response)]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
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
            "/api/v1/projects/proj_be76331b95dfc399cd776d2f/costs",
            {"params": {"range": "365d", "group_by": "model"}},
        )
    ]
    assert VALID_KEY not in result.stderr


def test_costs_rejects_invalid_response_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = login_credential(ACCESS_TOKEN)
    monkeypatch.setattr(
        costs_module, "resolve_credential", lambda **_: credential, raising=False
    )
    invalid = _response()
    invalid["groups"][0].pop("call_count")
    FakeClient.responses = [httpx.Response(200, json=invalid)]

    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs"]
    )

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

    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs"]
    )

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
            "proj_be76331b95dfc399cd776d2f",
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
    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs"]
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_costs_response"


@pytest.mark.parametrize(
    "dimension",
    [
        "status",
        "service_tier",
        "provider_region",
        "failover_reason",
        "agent_run",
        "tag:customer",
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_expanded_dimensions_match_goldens(monkeypatch, dimension, output_format):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    payload = _response()
    payload["group_by"] = dimension
    payload["groups"][0]["group_key"] = "example"
    if dimension == "agent_run":
        payload["groups"][0]["agent_run_name"] = "Research job"
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = runner.invoke(
        app,
        [
            "--format",
            output_format,
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "--group-by",
            dimension,
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["params"]["group_by"] == dimension
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"costs-{dimension.replace(':', '-')}-{output_format}.{suffix}",
    )


EVENT = {
    "id": "event_1",
    "project_id": "proj_be76331b95dfc399cd776d2f",
    "agent_run_id": None,
    "provider": "openai",
    "model": "gpt-5-mini",
    "input_tokens": 100,
    "output_tokens": 0,
    "cost": 0.0,
    "denied_cost": 0.25,
    "deny_source": "server",
    "deny_reason": "budget_exceeded",
    "estimated_cost": None,
    "status": "budget_denied",
    "is_model_fallback": False,
    # Core always returns the failover pair, and the trace fields when known.
    "is_provider_fallback": False,
    "failover_reason": None,
    "latency_ms": 0,
    "sdk_instance_id": "instance_1",
    "trace_id": None,
    "span_id": None,
    "trace_sampled": None,
    "trace_url": None,
    "created_at": "2026-07-10T00:00:00Z",
    "tags": {"customer": "example"},
}
TOTAL = {
    "project_id": "proj_be76331b95dfc399cd776d2f",
    "range": "7d",
    "total_cost": 2.5,
    "total_input_tokens": 2400,
    "total_output_tokens": 680,
    "call_count": 8,
    "unpriced_call_count": 0,
    "local_call_count": 2,
    "savings": None,
    "window": WINDOW,
}


@pytest.mark.parametrize(
    "command,payload",
    [
        (
            "events",
            {
                "items": [EVENT],
                "next_cursor": "next_page",
                "has_more": True,
                "window": WINDOW,
            },
        ),
        ("total", TOTAL),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_events_and_total_goldens(monkeypatch, command, payload, output_format):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = runner.invoke(
        app,
        [
            "--format",
            output_format,
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            command,
        ],
    )
    assert result.exit_code == 0, result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"costs-{command}-{output_format}.{suffix}"
    )
    if command == "events" and output_format == "table":
        assert_matches_golden(
            result.stderr, GOLDEN_DIR / "costs-events-continuation-stderr.txt"
        )


def test_all_filters_and_custom_window_reach_core(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    FakeClient.responses = [httpx.Response(200, json=TOTAL)]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "total",
            "--from",
            "2026-07-01T00:00:00Z",
            "--to",
            "2026-07-02T00:00:00Z",
            "--model",
            "m",
            "--provider",
            "p",
            "--modality",
            "text",
            "--status",
            "success",
            "--service-tier",
            "flex",
            "--provider-region",
            "us-east-1",
            "--agent-run",
            "raw",
            "--failover-reason",
            "primary_error",
            "--no-model-fallback",
            "--provider-fallback",
            "--tag",
            "x=a=b",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
        "model": "m",
        "provider": "p",
        "modality": "text",
        "status": "success",
        "service_tier": "flex",
        "provider_region": "us-east-1",
        "agent_run": "raw",
        "failover_reason": "primary_error",
        "is_model_fallback": False,
        "is_provider_fallback": True,
        "tag.x": "a=b",
    }


@pytest.mark.parametrize("status_code", [400, 422])
def test_core_invalid_query_is_usage_error(monkeypatch, status_code):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(status_code, json={"detail": "Invalid group_by from Core"})
        )
    ]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "--group-by",
            "invalid",
        ],
    )
    assert result.exit_code == 2
    assert "Invalid group_by from Core" in result.stderr


def test_retention_clamp_and_nullable_savings(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    payload = copy.deepcopy(TOTAL)
    payload["window"]["retention_clamped"] = True
    payload["savings"] = {
        "saved_cost": 1.0,
        "saved_cost_upper": None,
        "saved_input_tokens": 100,
        "blocked_call_count": 2,
    }
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs", "total"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["savings"]["saved_cost_upper"] is None
    assert "Retention clamped" in result.stderr
    assert "upper —" in result.stderr


@pytest.mark.parametrize("command", ["events", "total", "export"])
@pytest.mark.parametrize(
    "option",
    [
        ["--from", "2026-07-01T00:00:00Z"],
        ["--from", "2026-07-01", "--to", "2026-07-02"],
    ],
)
def test_all_cost_subcommands_reject_invalid_windows_before_auth(command, option):
    result = runner.invoke(
        app, ["-p", "proj_be76331b95dfc399cd776d2f", "costs", command, *option]
    )
    assert result.exit_code == 2
    assert not FakeClient.instances


@pytest.mark.parametrize("limit", ["0", "101"])
def test_events_limit_bounds(limit):
    result = runner.invoke(
        app,
        ["-p", "proj_be76331b95dfc399cd776d2f", "costs", "events", "--limit", limit],
    )
    assert result.exit_code == 2
    assert not FakeClient.instances


def test_events_cursor_and_probe_limit_reach_core(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "items": [],
                "next_cursor": None,
                "has_more": False,
                "window": WINDOW,
            },
        )
    ]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "events",
            "--limit",
            "1",
            "--cursor",
            "page",
        ],
    )
    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "range": "7d",
        "limit": 1,
        "cursor": "page",
    }


def test_current_capabilities_supply_choices_without_static_dimension_list(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    monkeypatch.setattr(
        costs_module,
        "get_capabilities",
        lambda **kwargs: TierCapabilities.model_validate(BILLING["capabilities"]),
    )
    payload = _response()
    payload["group_by"] = "future_dimension"
    FakeClient.responses = [httpx.Response(200, json=payload)]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "--group-by",
            "future_dimension",
        ],
    )
    assert result.exit_code == 0
    assert not result.stderr
    assert json.loads(result.stdout)["group_by"] == "future_dimension"


def test_disallowed_capabilities_keep_core_400_message_and_show_current_choices(
    monkeypatch,
):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    monkeypatch.setattr(
        costs_module,
        "get_capabilities",
        lambda **kwargs: TierCapabilities.model_validate(BILLING["capabilities"]),
    )
    FakeClient.responses = [
        error_from_response(
            httpx.Response(400, json={"detail": "Invalid group_by from Core"})
        )
    ]
    result = runner.invoke(
        app, ["-p", "proj_be76331b95dfc399cd776d2f", "costs", "--group-by", "bad"]
    )
    assert result.exit_code == 2
    assert "future_dimension" in result.stderr
    assert "Invalid group_by from Core" in result.stderr


def test_account_project_grouping_never_gets_a_false_capability_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        costs_module,
        "resolve_credential",
        lambda **_: login_credential(ACCESS_TOKEN),
        raising=False,
    )
    monkeypatch.setattr(
        costs_module,
        "get_capabilities",
        lambda **kwargs: TierCapabilities.model_validate(BILLING["capabilities"]),
    )
    FakeClient.responses = [
        error_from_response(httpx.Response(422, json={"detail": "Invalid window"}))
    ]

    result = runner.invoke(app, ["--json", "costs", "--account"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["message"] == "Invalid window"
    assert error["hint"] is None
    assert "Current plan groupings" not in result.stderr


def test_parent_filters_are_not_silently_ignored_for_subcommands():
    result = runner.invoke(
        app, ["-p", "proj_be76331b95dfc399cd776d2f", "costs", "--model", "m", "total"]
    )
    assert result.exit_code == 2
    assert "after the subcommand" in result.stderr
    assert not FakeClient.instances


@pytest.mark.parametrize(
    "command,feature,tier",
    [
        ("events", "cost_events", "production"),
        ("export", "csv_export", "production"),
        ("export", "csv_export.raw", "team"),
    ],
)
def test_cost_feature_upgrade_ladder_preserves_core_message(
    monkeypatch, command, feature, tier
):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    detail = {
        "code": "upgrade_required",
        "feature": feature,
        "current_tier": "free" if tier == "production" else "production",
        "required_tier": tier,
        "message": f"{feature} requires {tier}",
        "allowed_values": None,
        "cta_path": "/billing",
    }
    FakeClient.responses = [
        error_from_response(httpx.Response(402, json={"detail": detail}))
    ]
    result = runner.invoke(
        app, ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs", command]
    )
    assert result.exit_code == 1
    assert not result.stdout
    error = json.loads(result.stderr)
    assert error["message"] == f"{feature} requires {tier}"
    assert "/billing" in error["hint"]


def test_ndjson_export_is_not_wrapped_or_decoded(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    content = '{"model":"模型"}\n{"model":"second"}\n'.encode()
    FakeClient.responses = [httpx.Response(200, content=content)]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "export",
            "--format",
            "json",
            "--group-by",
            "model",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout_bytes == content
    assert FakeClient.instances[0].requests[0][2]["params"] == {
        "range": "30d",
        "format": "json",
        "group_by": "model",
    }


def test_entitlement_hints_keep_json_error_envelope_parseable(monkeypatch):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    monkeypatch.setattr(
        costs_module,
        "get_capabilities",
        lambda **kwargs: TierCapabilities.model_validate(BILLING["capabilities"]),
    )
    FakeClient.responses = [
        error_from_response(
            httpx.Response(400, json={"detail": "Invalid group_by from Core"})
        )
    ]
    result = runner.invoke(
        app,
        ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "costs", "--group-by", "bad"],
    )
    assert result.exit_code == 2
    assert "future_dimension" in json.loads(result.stderr)["hint"]


@pytest.mark.parametrize("output_format", ["table", "json"])
@pytest.mark.parametrize("has_more", [True, False])
def test_events_continuation_notice_stays_on_table_stderr(
    monkeypatch, output_format, has_more
):
    monkeypatch.setattr(costs_module, "resolve_credential", _credential)
    cursor = 'next-page-"quoted"' if has_more else None
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "items": [EVENT],
                "next_cursor": cursor,
                "has_more": has_more,
                "window": WINDOW,
            },
        )
    ]
    result = runner.invoke(
        app,
        [
            "--format",
            output_format,
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "costs",
            "events",
        ],
    )
    assert result.exit_code == 0
    assert "More events" not in result.stdout
    if output_format == "table" and has_more:
        assert "More events are available" in result.stderr
        assert "--cursor" in result.stderr
        assert json.dumps(cursor) in result.stderr
    else:
        assert result.stderr == ""
    if output_format == "json":
        assert json.loads(result.stdout)["next_cursor"] == cursor
