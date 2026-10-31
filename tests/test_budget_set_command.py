"""Budget configuration mutation contract tests."""

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
from login_fixtures import login_credential


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
JWT_ACCESS = "budget-set-access-token"
JWT_REFRESH = "budget-set-refresh-token"
VALID_KEY = "sk_proj_" + "a7" * 32
ROUTING = {
    "will_send": False,
    "email": None,
    "channels": [],
    "source": "project_default",
    "muted": {"reason": "email_off", "severity_floor": None},
}
THRESHOLDS = [
    {
        "type": "budget_threshold",
        "rule_key": "budget:80",
        "percent": 80,
        "channel_ids": ["channel-budget"],
        "routing": ROUTING,
        "snoozed_until": "2026-07-11T12:00:00Z",
        "paused": True,
    },
    {
        "type": "forecast_overrun",
        "rule_key": "forecast:125",
        "spend_threshold_usd": 125.5,
        "period": "month",
        "channel_ids": ["channel-forecast"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "runaway_run",
        "mode": "alert_only",
        "rule_key": "runaway:50",
        "spend_threshold_usd": 50.0,
        "call_threshold": 100,
        "window_minutes": 60,
        "channel_ids": ["channel-runaway"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "daily_anomaly",
        "rule_key": "daily:2.5:14",
        "multiplier": 2.5,
        "lookback_days": 14,
        "channel_ids": [],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "unit_cost_anomaly",
        "rule_key": "unit:3:21",
        "multiplier": 3.0,
        "lookback_days": 21,
        "channel_ids": ["channel-unit"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": True,
    },
    {
        "type": "scoped_budget",
        "current_usage": None,
        "match_value": None,
        "rule_key": "scoped:model:gpt-4o",
        "scope": "model",
        "match": "gpt-4o",
        "limit": 25.5,
        "mode": "hard_deny",
        "channel_ids": ["channel-scoped"],
        "routing": ROUTING,
        "snoozed_until": None,
        "paused": False,
    },
]
BUDGET_RESPONSE = {
    "project_id": "proj_alpha",
    "budget_limit": 100.0,
    "current_usage": 25.0,
    "remaining_budget": 75.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "period_date": "2026-07",
    "outage_overspend_ceiling_usd": 0.0,
    "utilization_pct": 25.0,
    "thresholds": THRESHOLDS,
    "sub_period": "daily",
    "sub_limit": 20.0,
    "constraints": {
        "allowed_modes": ["alert_only", "hard_deny"],
        "allowed_periods": ["daily", "weekly", "monthly"],
        "sub_period_enabled": True,
        "max_thresholds": 50,
    },
}
EXPECTED_THRESHOLD_CONFIGS = [
    {
        "type": "budget_threshold",
        "percent": 80,
        "channel_ids": ["channel-budget"],
        "snoozed_until": "2026-07-11T12:00:00Z",
        "paused": True,
    },
    {
        "type": "forecast_overrun",
        "spend_threshold_usd": 125.5,
        "period": "month",
        "channel_ids": ["channel-forecast"],
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "runaway_run",
        "mode": "alert_only",
        "spend_threshold_usd": 50.0,
        "call_threshold": 100,
        "window_minutes": 60,
        "channel_ids": ["channel-runaway"],
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "daily_anomaly",
        "multiplier": 2.5,
        "lookback_days": 14,
        "channel_ids": [],
        "snoozed_until": None,
        "paused": False,
    },
    {
        "type": "unit_cost_anomaly",
        "multiplier": 3.0,
        "lookback_days": 21,
        "channel_ids": ["channel-unit"],
        "snoozed_until": None,
        "paused": True,
    },
    {
        "type": "scoped_budget",
        "match_value": None,
        "scope": "model",
        "match": "gpt-4o",
        "limit": 25.5,
        "mode": "hard_deny",
        "channel_ids": ["channel-scoped"],
        "snoozed_until": None,
        "paused": False,
    },
]


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
    monkeypatch.setattr(budget_module, "SolwynClient", FakeClient)
    yield


def _jwt(**_: object) -> auth.LoginCredential:
    return login_credential(JWT_ACCESS)


def _response(**overrides: object) -> dict[str, object]:
    return {**BUDGET_RESPONSE, **overrides}


def test_budget_group_registers_set_without_regressing_existing_commands() -> None:
    result = runner.invoke(app, ["budget", "--help"])

    assert result.exit_code == 0
    for command in ("status", "check", "confirm", "set"):
        assert command in result.stdout


def test_budget_set_partial_limit_preserves_every_untouched_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _jwt()
    project_id = "team/alpha beta"
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [
        httpx.Response(200, json=_response(project_id=project_id)),
        httpx.Response(
            200,
            json=_response(
                project_id=project_id,
                budget_limit=250.0,
                remaining_budget=225.0,
                utilization_pct=10.0,
            ),
        ),
    ]

    result = runner.invoke(
        app,
        ["--json", "--project", project_id, "budget", "set", "--limit", "250"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [
        ("GET", "/api/v1/projects/team%2Falpha%20beta/budget", {}),
        (
            "PUT",
            "/api/v1/projects/team%2Falpha%20beta/budget",
            {
                "json": {
                    "limit_usd": 250.0,
                    "period": "monthly",
                    "mode": "hard_deny",
                    "sub_period": "daily",
                    "sub_limit_usd": 20.0,
                    "thresholds": EXPECTED_THRESHOLD_CONFIGS,
                }
            },
        ),
    ]
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solwyn.budget-set.v1"
    assert payload["budget_limit"] == 250.0
    assert payload["thresholds"] == EXPECTED_THRESHOLD_CONFIGS
    assert "rule_key" not in result.stdout
    assert "routing" not in result.stdout
    rendered = result.stdout + result.stderr + repr(result.exception)
    for secret in (JWT_ACCESS, JWT_REFRESH):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("flag", "value", "changed_field", "changed_value"),
    [
        ("--period", "weekly", "period", "weekly"),
        ("--mode", "alert_only", "mode", "alert_only"),
    ],
)
def test_budget_set_period_or_mode_only_changes_its_matching_field(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
    changed_field: str,
    changed_value: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    put_overrides = {
        "budget_period" if changed_field == "period" else "budget_mode": changed_value
    }
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=_response(**put_overrides)),
    ]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "budget", "set", flag, value],
    )

    assert result.exit_code == 0
    body = FakeClient.instances[0].requests[1][2]["json"]
    expected = {
        "limit_usd": 100.0,
        "period": "monthly",
        "mode": "hard_deny",
        "sub_period": "daily",
        "sub_limit_usd": 20.0,
        "thresholds": EXPECTED_THRESHOLD_CONFIGS,
    }
    expected[changed_field] = changed_value
    assert body == expected


def test_budget_set_no_changes_is_usage_error_before_auth_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        budget_module,
        "resolve_credential",
        lambda **_: pytest.fail("must not resolve auth"),
    )

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "budget", "set"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []
    error = json.loads(result.stderr)
    assert error["code"] == "budget_changes_required"
    assert "--limit" in error["hint"]
    assert "--period" in error["hint"]
    assert "--mode" in error["hint"]


@pytest.mark.parametrize(
    ("source", "expected_hint"),
    [
        ("environment", "Run solwyn login, then retry budget set."),
        ("keyring", "Run solwyn login, then retry budget set."),
    ],
)
def test_budget_set_refuses_each_project_key_source_without_requests(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_hint: str,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: credential)

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_alpha",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert FakeClient.instances == []
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert expected_hint in error["hint"]
    assert VALID_KEY not in result.stderr + repr(result.exception)


def test_budget_set_requires_login_and_project_before_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "load_config", lambda: Config(default_project=None))
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: None)

    missing_project = runner.invoke(
        app,
        ["--json", "budget", "set", "--limit", "125"],
        env={},
    )
    missing_auth = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "budget", "set", "--limit", "125"],
        env={},
    )

    assert missing_project.exit_code == 2
    assert json.loads(missing_project.stderr)["code"] == "project_required"
    assert missing_auth.exit_code == 3
    assert json.loads(missing_auth.stderr)["code"] == "authentication_required"
    assert FakeClient.instances == []


def test_budget_set_get_failure_is_preserved_and_never_puts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                404,
                json={
                    "detail": {
                        "code": "project_not_found",
                        "message": "Project was not found.",
                    }
                },
            )
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "missing", "budget", "set", "--mode", "alert_only"],
    )

    assert result.exit_code == 5
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/missing/budget", {})
    ]


def test_budget_set_put_failure_is_preserved_after_one_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        error_from_response(
            httpx.Response(
                422,
                json={
                    "detail": {
                        "code": "unknown_notification_channel",
                        "message": "A notification channel was not found.",
                    }
                },
            )
        ),
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_alpha", "budget", "set", "--limit", "125"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "unknown_notification_channel"
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
        "PUT",
    ]


@pytest.mark.parametrize(
    "invalid_current",
    [
        _response(thresholds=[{**THRESHOLDS[0], "type": "future_rule"}]),
        _response(
            thresholds=[
                {**THRESHOLDS[1], "spend_threshold_usd": "Infinity"},
            ]
        ),
        _response(sub_period="yearly"),
        _response(sub_limit="not-a-number"),
        {key: value for key, value in BUDGET_RESPONSE.items() if key != "thresholds"},
    ],
)
def test_budget_set_rejects_malformed_current_state_without_put(
    monkeypatch: pytest.MonkeyPatch,
    invalid_current: dict[str, object],
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(200, json=invalid_current)]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_alpha", "budget", "set", "--limit", "125"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"
    assert [request[0] for request in FakeClient.instances[0].requests] == ["GET"]


@pytest.mark.parametrize(
    "invalid_put",
    [
        _response(project_id="proj_other"),
        _response(budget_limit="NaN"),
        _response(thresholds=[{**THRESHOLDS[1], "routing": {}}]),
    ],
)
def test_budget_set_rejects_malformed_or_mismatched_put_response(
    monkeypatch: pytest.MonkeyPatch,
    invalid_put: dict[str, object],
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=invalid_put),
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_alpha", "budget", "set", "--limit", "125"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
        "PUT",
    ]


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_budget_set_success_matches_stable_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(
            200,
            json=_response(
                budget_limit=125.0,
                remaining_budget=100.0,
                utilization_pct=20.0,
            ),
        ),
    ]
    output_options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *output_options,
            "-p",
            "proj_alpha",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-set-{output_format}.{suffix}",
    )


def test_budget_set_uses_global_project_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(default_project="proj_config"),
    )
    FakeClient.responses = [
        httpx.Response(200, json=_response(project_id="proj_environment")),
        httpx.Response(200, json=_response(project_id="proj_environment")),
    ]

    result = runner.invoke(
        app,
        ["--json", "budget", "set", "--mode", "alert_only"],
        env={"SOLWYN_PROJECT_ID": "proj_environment"},
    )

    assert result.exit_code == 0
    assert [request[1] for request in FakeClient.instances[0].requests] == [
        "/api/v1/projects/proj_environment/budget",
        "/api/v1/projects/proj_environment/budget",
    ]


def test_budget_set_goldens_never_contain_credentials_or_response_only_state() -> None:
    for golden in GOLDEN_DIR.glob("budget-set*"):
        rendered = golden.read_text(encoding="utf-8")
        for secret in (JWT_ACCESS, JWT_REFRESH, VALID_KEY, "rule_key", "routing"):
            assert secret not in rendered


@pytest.mark.parametrize(
    "reconciled",
    [
        _response(budget_limit=125.0, current_usage=31.0),
        _response(
            budget_limit=125.0,
            thresholds=[
                {
                    **rule,
                    "rule_key": "new-server-key",
                    **(
                        {"current_usage": 9.0}
                        if rule["type"] == "scoped_budget"
                        else {}
                    ),
                }
                for rule in THRESHOLDS
            ],
        ),
    ],
)
def test_budget_set_reconciles_post_commit_503_without_replaying_put(
    monkeypatch, reconciled
):
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        error_from_response(
            httpx.Response(
                503, json={"detail": "Budget backend temporarily unavailable; retry"}
            )
        ),
        httpx.Response(200, json=reconciled),
    ]
    result = runner.invoke(
        app, ["--json", "-p", "proj_alpha", "budget", "set", "--limit", "125"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["budget_limit"] == 125.0
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
        "PUT",
        "GET",
    ]


@pytest.mark.parametrize(
    "reconciled",
    [
        httpx.Response(200, json=_response()),
        httpx.Response(200, json=_response(budget_limit=125.0, budget_period="weekly")),
        httpx.Response(
            200, json=_response(budget_limit=125.0, budget_mode="alert_only")
        ),
        httpx.Response(200, json=_response(budget_limit=125.0, sub_limit=21.0)),
        httpx.Response(200, json=_response(budget_limit=125.0, sub_period="weekly")),
        *[
            httpx.Response(
                200,
                json=_response(
                    budget_limit=125.0,
                    thresholds=[
                        {**rule, **changes} if rule["type"] == rule_type else rule
                        for rule in THRESHOLDS
                    ],
                ),
            )
            for rule_type, changes in [
                ("runaway_run", {"mode": "hard_deny"}),
                ("scoped_budget", {"match": "different-model"}),
                ("budget_threshold", {"channel_ids": ["different-channel"]}),
                ("daily_anomaly", {"paused": True}),
            ]
        ],
        httpx.Response(
            200, json=_response(budget_limit=125.0, thresholds=THRESHOLDS[:-1])
        ),
        httpx.Response(200, json=_response(budget_limit=125.0, project_id="different")),
        httpx.Response(200, json={"invalid": True}),
        error_from_response(httpx.Response(503, json={"detail": "Still unavailable"})),
        auth.AuthError("Synthetic auth failure"),
    ],
)
def test_budget_set_503_unreconciled_is_explicitly_indeterminate(
    monkeypatch, reconciled
):
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        error_from_response(
            httpx.Response(
                503, json={"detail": "Budget backend temporarily unavailable; retry"}
            )
        ),
        reconciled,
    ]
    result = runner.invoke(
        app, ["--json", "-p", "proj_alpha", "budget", "set", "--limit", "125"]
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "budget_update_indeterminate"
    assert "may have been saved" in error["message"]
    assert "budget status" in error["hint"]
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
        "PUT",
        "GET",
    ]
