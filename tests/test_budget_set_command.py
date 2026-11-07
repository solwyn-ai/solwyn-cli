"""Budget configuration mutation contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from golden.harness import assert_matches_golden
from key_fixtures import same_credential
from login_fixtures import login_credential
from typer.testing import CliRunner

from solwyn_cli import auth, runtime
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.config import Config
from solwyn_cli.errors import error_from_response
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
JWT_ACCESS = "budget-set-access-token"
JWT_REFRESH = "budget-set-refresh-token"
VALID_KEY = "solwyn_sk_" + "a7" * 32
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
    "project_id": "proj_be76331b95dfc399cd776d2f",
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
        # The approval re-check hook is exercised by the approval tests below.
        kwargs.pop("before_resend", None)
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
    project_id = "proj_d25187dc137f35c88bc80ec8"
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [
        httpx.Response(200, json=_response(project_id=project_id)),
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
    assert same_credential(client.credential, credential)
    assert client.requests == [
        ("GET", "/api/v1/projects/proj_d25187dc137f35c88bc80ec8/budget", {}),
        ("GET", "/api/v1/projects/proj_d25187dc137f35c88bc80ec8/budget", {}),
        (
            "PUT",
            "/api/v1/projects/proj_d25187dc137f35c88bc80ec8/budget",
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
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=_response(**put_overrides)),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            flag,
            value,
        ],
    )

    assert result.exit_code == 0
    body = FakeClient.instances[0].requests[2][2]["json"]
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
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "budget", "set"],
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
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source=source)
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: credential)

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_be76331b95dfc399cd776d2f",
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
    monkeypatch.setattr(runtime, "load_config", lambda: Config())
    monkeypatch.setattr(budget_module, "resolve_credential", lambda **_: None)

    missing_project = runner.invoke(
        app,
        ["--json", "budget", "set", "--limit", "125"],
        env={},
    )
    missing_auth = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
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
        [
            "--json",
            "-p",
            "proj_5a013c49508291c6816ac388",
            "budget",
            "set",
            "--mode",
            "alert_only",
        ],
    )

    # project_not_found names a selector problem (Core spec 2.4): a usage error.
    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_5a013c49508291c6816ac388/budget", {})
    ]


def test_budget_set_put_failure_is_preserved_after_one_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
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
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "unknown_notification_channel"
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
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
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"
    assert [request[0] for request in FakeClient.instances[0].requests] == ["GET"]


@pytest.mark.parametrize(
    "invalid_put",
    [
        _response(project_id="proj_d0941e68da8f38151ff86a61"),
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
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=invalid_put),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
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
            "proj_be76331b95dfc399cd776d2f",
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
        lambda: Config(),
    )
    FakeClient.responses = [
        httpx.Response(200, json=_response(project_id="proj_c6e454d960df4845f9d69eb1")),
        httpx.Response(200, json=_response(project_id="proj_c6e454d960df4845f9d69eb1")),
        httpx.Response(200, json=_response(project_id="proj_c6e454d960df4845f9d69eb1")),
    ]

    result = runner.invoke(
        app,
        ["--json", "budget", "set", "--mode", "alert_only"],
        env={"SOLWYN_PROJECT": "proj_c6e454d960df4845f9d69eb1"},
    )

    assert result.exit_code == 0
    assert [request[1] for request in FakeClient.instances[0].requests] == [
        "/api/v1/projects/proj_c6e454d960df4845f9d69eb1/budget",
        "/api/v1/projects/proj_c6e454d960df4845f9d69eb1/budget",
        "/api/v1/projects/proj_c6e454d960df4845f9d69eb1/budget",
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
        httpx.Response(200, json=BUDGET_RESPONSE),
        error_from_response(
            httpx.Response(
                503, json={"detail": "Budget backend temporarily unavailable; retry"}
            )
        ),
        httpx.Response(200, json=reconciled),
    ]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["budget_limit"] == 125.0
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
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
        httpx.Response(200, json=BUDGET_RESPONSE),
        error_from_response(
            httpx.Response(
                503, json={"detail": "Budget backend temporarily unavailable; retry"}
            )
        ),
        reconciled,
    ]
    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "budget",
            "set",
            "--limit",
            "125",
        ],
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "budget_update_indeterminate"
    assert "may have been saved" in error["message"]
    assert "budget status" in error["hint"]
    assert [request[0] for request in FakeClient.instances[0].requests] == [
        "GET",
        "GET",
        "PUT",
        "GET",
    ]


# --- Audit F2: budget set re-reads before the PUT and after an approval -------

PROJECT = "proj_be76331b95dfc399cd776d2f"
BUDGET_PATH = f"/api/v1/projects/{PROJECT}/budget"
APPROVAL_ID = "0b7d2c4e-0000-4000-8000-000000000001"
GUARDRAIL = {
    "type": "runaway_run",
    "mode": "hard_deny",
    "rule_key": "runaway:5",
    "spend_threshold_usd": 5.0,
    "call_threshold": None,
    "window_minutes": None,
    "channel_ids": [],
    "routing": ROUTING,
    "snoozed_until": None,
    "paused": False,
}
EDITED = _response(thresholds=[*THRESHOLDS, GUARDRAIL])


def _set_limit() -> Any:
    return runner.invoke(
        app, ["--json", "-p", PROJECT, "budget", "set", "--limit", "500"]
    )


def test_budget_set_refuses_when_the_configuration_changes_before_the_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(budget_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=BUDGET_RESPONSE),
        httpx.Response(200, json=EDITED),
    ]

    result = _set_limit()

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "budget_configuration_changed"
    assert [r[0] for r in FakeClient.instances[0].requests] == ["GET", "GET"]


class _ApprovingCore:
    """Real client over a fake Core: the PUT needs approval, which is granted
    after ``edit`` (a dashboard change) has replaced the configuration."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, edit: dict | None) -> None:
        from solwyn_cli.client import SolwynClient as RealSolwynClient

        self.requests: list[httpx.Request] = []
        self.current: dict = BUDGET_RESPONSE
        self.edit = edit
        self.puts = 0

        def factory(*, api_url: str, credential: object) -> Any:
            return RealSolwynClient(
                api_url=api_url,
                credential=credential,
                transport=httpx.MockTransport(self.handle),
                sleeper=lambda _: None,
            )

        monkeypatch.setattr(budget_module, "SolwynClient", factory)
        monkeypatch.setattr(budget_module, "resolve_credential", _jwt)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/v1/cli/approvals/{APPROVAL_ID}":
            if self.edit is not None:
                self.current = self.edit  # edited while the CLI waited
            return httpx.Response(200, json=_approval_view())
        if path == BUDGET_PATH and request.method == "GET":
            return httpx.Response(200, json=self.current)
        if path == BUDGET_PATH and request.method == "PUT":
            self.puts += 1
            if "Solwyn-Approval" not in request.headers:
                return httpx.Response(
                    403,
                    json={
                        "detail": {
                            "code": "approval_required",
                            "message": "This change needs your approval.",
                            "approval_id": APPROVAL_ID,
                            "approval_url": f"https://app.solwyn.ai/approve/{APPROVAL_ID}",
                        }
                    },
                )
            body = json.loads(request.content)
            return httpx.Response(200, json=_response(budget_limit=body["limit_usd"]))
        return httpx.Response(599, json={"detail": "unexpected"})


def _approval_view() -> dict:
    return {
        "id": APPROVAL_ID,
        "status": "approved",
        "action": "budget.update",
        "title": "Change the budget",
        "reasons": ["raises_cap"],
        "changes": [],
        "request": {"method": "PUT", "path": BUDGET_PATH, "query": ""},
        "credential": {"id": "c", "label": "dev-laptop"},
        "project": {"id": PROJECT, "name": "checkout"},
        "request_ip": "203.0.113.7",
        "created_at": "2026-10-30T14:20:00+00:00",
        "expires_at": "2099-10-30T14:30:00+00:00",
    }


def test_budget_set_never_resends_a_stale_body_after_an_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _ApprovingCore(monkeypatch, edit=EDITED)

    result = _set_limit()

    assert result.exit_code == 6, result.stderr
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "budget_changed_during_approval"
    # The approved resend was never sent: the dashboard's guardrail survives.
    assert core.puts == 1


def test_budget_set_resends_after_an_approval_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = _ApprovingCore(monkeypatch, edit=None)

    result = _set_limit()

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["budget_limit"] == 500.0
    assert core.puts == 2
