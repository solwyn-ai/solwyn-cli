"""Budget rule commands preserve configuration and expose enforcement state."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import budget_rules
from solwyn_cli.main import app
from solwyn_cli.errors import error_from_response
from test_budget_set_command import BUDGET_RESPONSE, ROUTING, FakeClient
from login_fixtures import login_credential

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
TYPES = [
    ("budget_threshold", ["--percent", "90"], {"percent": 90}),
    (
        "runaway_run",
        ["--spend", "10", "--mode", "hard_deny"],
        {
            "spend_threshold_usd": 10.0,
            "mode": "hard_deny",
            "call_threshold": None,
            "window_minutes": None,
        },
    ),
    (
        "scoped_budget",
        [
            "--scope",
            "tag",
            "--match",
            "env",
            "--value",
            "prod",
            "--limit",
            "5",
            "--mode",
            "hard_deny",
        ],
        {
            "scope": "tag",
            "match": "env",
            "match_value": "prod",
            "limit": 5.0,
            "mode": "hard_deny",
            "current_usage": None,
        },
    ),
    (
        "forecast_overrun",
        ["--spend", "200"],
        {"spend_threshold_usd": 200.0, "period": "month"},
    ),
    ("daily_anomaly", ["--multiplier", "4"], {"multiplier": 4.0, "lookback_days": 7}),
    (
        "unit_cost_anomaly",
        ["--multiplier", "5"],
        {"multiplier": 5.0, "lookback_days": 7},
    ),
]


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(budget_rules, "SolwynClient", FakeClient)
    monkeypatch.setattr(
        budget_rules,
        "resolve_credential",
        lambda **_: login_credential("access"),
    )


def invoke(args, fmt="json"):
    return runner.invoke(
        app, ["--format", fmt, "--project", "proj_alpha", "budget", *args]
    )


def response(**changes):
    return httpx.Response(200, json={**copy.deepcopy(BUDGET_RESPONSE), **changes})


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_list_golden(fmt):
    FakeClient.responses = [response()]
    result = invoke(["rules", "list"], fmt)
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-rules-list-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )
    assert "snoozed hard_deny still denies" in result.stderr


@pytest.mark.parametrize("kind,flags,fields", TYPES)
@pytest.mark.parametrize("fmt", ["json", "table"])
def test_add_golden(kind, flags, fields, fmt):
    rule = {
        "type": kind,
        "rule_key": "new-rule-key",
        "channel_ids": ["ch-one"],
        "paused": False,
        "snoozed_until": None,
        "routing": ROUTING,
        **fields,
    }
    FakeClient.responses = [
        response(),
        response(),
        response(thresholds=[*BUDGET_RESPONSE["thresholds"], rule]),
    ]
    result = invoke(["rule", "add", "--type", kind, *flags, "--channel", "ch-one"], fmt)
    assert result.exit_code == 0, result.output
    requests = FakeClient.instances[0].requests
    assert [r[0] for r in requests] == ["GET", "GET", "PUT"]
    body = requests[-1][2]["json"]
    assert body["sub_period"] == "daily" and body["sub_limit_usd"] == 20
    assert body["mode"] == "hard_deny" and body["limit_usd"] == 100
    assert all(
        not {"current_usage", "routing", "rule_key"} & r.keys()
        for r in body["thresholds"]
    )
    assert (
        "rule_key" in result.stdout
        if fmt == "json"
        else "new-rule-key" in result.stdout
    )
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR
        / f"budget-rule-add-{kind}-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )


def test_concurrent_edit_aborts_before_put():
    FakeClient.responses = [response(), response(budget_limit=200)]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "90"])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "budget_configuration_changed"
    assert [r[0] for r in FakeClient.instances[0].requests] == ["GET", "GET"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--type", "runaway_run", "--spend", "5", "--calls", "2"],
        [
            "--type",
            "scoped_budget",
            "--scope",
            "tag",
            "--match",
            "env",
            "--limit",
            "5",
            "--mode",
            "hard_deny",
        ],
        ["--type", "budget_threshold", "--percent", "80"],
    ],
)
def test_invalid_add_never_puts(flags):
    FakeClient.responses = [response()]
    result = invoke(["rule", "add", *flags])
    assert result.exit_code == 2, result.output
    assert all(r[0] != "PUT" for c in FakeClient.instances for r in c.requests)


ACTIONS = [
    ("rule", "remove", {"rule_key": "key-one", "deleted": True}, "DELETE"),
    ("rule", "pause", {"rule_key": "key-one", "paused": True}, "POST"),
    ("rule", "resume", {"rule_key": "key-one", "paused": False}, "POST"),
    ("rule", "snooze", {"snoozed_until": "2026-12-01T12:00:00Z"}, "POST"),
    ("rule", "unsnooze", {"snoozed_until": None}, "POST"),
    ("threshold", "snooze", {"snoozed_until": "2026-12-01T12:00:00Z"}, "POST"),
    ("threshold", "unsnooze", {"snoozed_until": None}, "POST"),
]


@pytest.mark.parametrize("group,action,payload,method", ACTIONS)
@pytest.mark.parametrize("fmt", ["json", "table"])
def test_actions_golden(group, action, payload, method, fmt):
    FakeClient.responses = [httpx.Response(200, json=payload)]
    identifier = "80" if group == "threshold" else "key-one"
    flags = ["--until", "2026-12-01T12:00:00Z"] if action == "snooze" else []
    result = invoke([group, action, identifier, *flags], fmt)
    assert result.exit_code == 0, result.output
    request = FakeClient.instances[0].requests[0]
    assert request[0] == method
    assert request[1].endswith(
        f"/{group}/{identifier}" + (f"/{action}" if action != "remove" else "")
    )
    if flags:
        assert request[2]["json"] == payload
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR
        / f"budget-{group}-{action}-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_restore_reads_full_state(fmt):
    FakeClient.responses = [
        httpx.Response(200, json={"project_id": "proj_alpha", "thresholds": []}),
        response(),
    ]
    result = invoke(["restore-defaults"], fmt)
    assert result.exit_code == 0, result.output
    assert [r[0] for r in FakeClient.instances[0].requests] == ["POST", "GET"]
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR
        / f"budget-restore-defaults-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )


@pytest.mark.parametrize("fmt", ["json", "table"])
def test_preview_key_auth_and_draft_channels(monkeypatch, fmt):
    monkeypatch.setattr(
        budget_rules,
        "resolve_credential",
        lambda **_: auth.ProjectKeyCredential(
            api_key="sk_proj_" + "a7" * 32, source="environment"
        ),
    )
    FakeClient.responses = [
        response(),
        httpx.Response(200, json={"results": [ROUTING]}),
    ]
    result = invoke(["rule", "preview", "budget:80", "--channel", "draft"], fmt)
    assert result.exit_code == 0, result.output
    body = FakeClient.instances[0].requests[-1][2]["json"]
    assert body["alerts"][0]["channel_ids"] == ["draft"]
    assert "rule_key" not in body["alerts"][0]["rule"]
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"budget-rule-preview-{fmt}.{'json' if fmt == 'json' else 'txt'}",
    )


def test_naive_snooze_rejected_before_request():
    result = invoke(["rule", "snooze", "key", "--until", "2026-12-01T12:00:00"])
    assert result.exit_code == 2
    assert not FakeClient.instances


def test_relative_snooze_is_sent_as_absolute():
    FakeClient.responses = [httpx.Response(200, json={"snoozed_until": None})]
    result = invoke(["rule", "snooze", "key", "--until", "+2h"])
    assert result.exit_code == 0
    value = FakeClient.instances[0].requests[0][2]["json"]["snoozed_until"]
    assert value.endswith("Z") and not value.startswith("+")


def test_threshold_count_counts_non_threshold_rules():
    constraints = {**BUDGET_RESPONSE["constraints"], "max_thresholds": 6}
    FakeClient.responses = [response(constraints=constraints)]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "90"])
    assert result.exit_code == 1
    assert "budget.threshold_count" in result.stderr
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("saved", [True, False])
def test_503_add_reconciles_full_config_without_replay(saved):
    rule = {
        "type": "budget_threshold",
        "percent": 90,
        "rule_key": "new",
        "channel_ids": [],
        "paused": False,
        "snoozed_until": None,
        "routing": ROUTING,
    }
    error = budget_rules.ApiError(
        status_code=503,
        code="http_503",
        message="Budget backend temporarily unavailable; retry",
        hint=None,
        detail=None,
    )
    FakeClient.responses = [
        response(),
        response(),
        error,
        response(
            thresholds=[*BUDGET_RESPONSE["thresholds"], rule]
            if saved
            else BUDGET_RESPONSE["thresholds"]
        ),
    ]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "90"])
    assert result.exit_code == (0 if saved else 1), result.output
    assert [r[0] for r in FakeClient.instances[0].requests] == [
        "GET",
        "GET",
        "PUT",
        "GET",
    ]
    if not saved:
        assert "budget_update_indeterminate" in result.stderr


@pytest.mark.parametrize("group", ["rule", "threshold"])
def test_action_not_found_exit_five(group):
    code = "rule_not_found" if group == "rule" else "threshold_not_found"
    FakeClient.responses = [
        budget_rules.ApiError(
            status_code=404, code=code, message="Not found", hint=None, detail=None
        )
    ]
    result = invoke([group, "unsnooze", "80"])
    assert result.exit_code == 5 and code in result.stderr


@pytest.mark.parametrize(
    "feature",
    [
        "budget.hard_cap_limit",
        "budget.threshold_count",
        "budget.mode",
        "budget.period",
        "budget.sub_period",
    ],
)
def test_add_402_preserves_core_detail(feature):
    error = error_from_response(
        httpx.Response(
            402,
            json={
                "detail": {
                    "code": "upgrade_required",
                    "feature": feature,
                    "message": "Core plan message",
                    "allowed_values": ["alert_only"],
                    "cta_path": "/settings/billing",
                }
            },
        )
    )
    FakeClient.responses = [response(), response(), error]
    result = invoke(["rule", "add", "--type", "runaway_run", "--spend", "10"])
    assert result.exit_code == 1
    assert (
        feature in result.stderr
        and "Core plan message" in result.stderr
        and "Allowed values: alert_only" in result.stderr
    )


def test_unknown_channels_lists_ids():
    FakeClient.responses = [
        response(),
        response(),
        budget_rules.ApiError(
            status_code=422,
            code="unknown_notification_channel",
            message="Unknown channels",
            hint=None,
            detail={"channel_ids": ["ch-missing", "ch-other"]},
        ),
    ]
    result = invoke(["rule", "add", "--type", "runaway_run", "--spend", "10"])
    assert result.exit_code == 1
    assert "ch-missing, ch-other" in result.stderr


def test_runaway_validation_error_location_is_preserved():
    detail = [
        {
            "loc": ["body", "thresholds", 6, "runaway_run", "mode"],
            "msg": "Mode required",
            "type": "missing",
        }
    ]
    FakeClient.responses = [
        response(),
        response(),
        budget_rules.ApiError(
            status_code=422,
            code="http_422",
            message="Validation failed",
            hint=None,
            detail=detail,
        ),
    ]
    result = invoke(["rule", "add", "--type", "runaway_run", "--spend", "10"])
    assert result.exit_code == 1
    assert "runaway_run" in result.stderr and "mode" in result.stderr
    assert (
        FakeClient.instances[0].requests[-1][2]["json"]["thresholds"][-1]["mode"]
        == "alert_only"
    )


def test_unlimited_threshold_entitlement_allows_add():
    rule = {
        "type": "budget_threshold",
        "percent": 90,
        "rule_key": "new",
        "channel_ids": [],
        "paused": False,
        "snoozed_until": None,
        "routing": ROUTING,
    }
    constraints = {**BUDGET_RESPONSE["constraints"], "max_thresholds": -1}
    FakeClient.responses = [
        response(constraints=constraints),
        response(constraints=constraints),
        response(thresholds=[*BUDGET_RESPONSE["thresholds"], rule]),
    ]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "90"])
    assert result.exit_code == 0, result.output


def test_preview_unsaved_condition_without_status_read():
    FakeClient.responses = [httpx.Response(200, json={"results": [ROUTING]})]
    result = invoke(
        [
            "rule",
            "preview",
            "--type",
            "runaway_run",
            "--spend",
            "10",
            "--mode",
            "hard_deny",
        ]
    )
    assert result.exit_code == 0, result.output
    request = FakeClient.instances[0].requests[0]
    assert request[0:2] == ("POST", "/api/v1/projects/proj_alpha/alerts/preview")
    assert request[2]["json"]["alerts"][0]["rule"]["mode"] == "hard_deny"


@pytest.mark.parametrize("action", ["remove", "pause", "resume", "snooze", "unsnooze"])
def test_rule_mutations_reject_project_key_without_http(monkeypatch, action):
    monkeypatch.setattr(
        budget_rules,
        "resolve_credential",
        lambda **_: auth.ProjectKeyCredential(
            api_key="sk_proj_" + "a7" * 32, source="environment"
        ),
    )
    result = invoke(
        ["rule", action, "key", *(["--until", "+2h"] if action == "snooze" else [])]
    )
    assert result.exit_code == 3
    assert not FakeClient.instances


def test_resume_retains_hard_cap_402_and_role_hint():
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                402,
                json={
                    "detail": {
                        "code": "upgrade_required",
                        "feature": "budget.hard_cap_limit",
                        "message": "Hard cap limit",
                        "cta_path": "/settings/billing",
                    }
                },
            )
        )
    ]
    result = invoke(["rule", "resume", "key"])
    assert result.exit_code == 1 and "budget.hard_cap_limit" in result.stderr
    FakeClient.responses = [
        error_from_response(httpx.Response(403, json={"detail": "permission_required"}))
    ]
    result = invoke(["rule", "resume", "key"])
    assert result.exit_code == 3 and "whoami" in result.stderr


def test_new_threshold_inserted_in_ascending_percentage_order():
    rule = {
        "type": "budget_threshold",
        "percent": 30,
        "rule_key": "new",
        "channel_ids": [],
        "paused": False,
        "snoozed_until": None,
        "routing": ROUTING,
    }
    FakeClient.responses = [
        response(),
        response(current_usage=26),
        response(thresholds=[rule, *BUDGET_RESPONSE["thresholds"]]),
    ]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "30"])
    assert result.exit_code == 0, result.output
    sent = FakeClient.instances[0].requests[-1][2]["json"]["thresholds"]
    assert [r["percent"] for r in sent if r["type"] == "budget_threshold"] == [30, 80]


@pytest.mark.parametrize(
    "flags",
    [
        ["--type", "daily_anomaly", "--multiplier", "2.5", "--lookback", "7"],
        ["--type", "runaway_run", "--spend", "50", "--mode", "hard_deny"],
        [
            "--type",
            "scoped_budget",
            "--scope",
            "model",
            "--match",
            "gpt-4o",
            "--limit",
            "99",
            "--mode",
            "hard_deny",
        ],
    ],
)
def test_duplicate_key_conditions_rejected_despite_other_field_changes(flags):
    FakeClient.responses = [response()]
    result = invoke(["rule", "add", *flags])
    assert result.exit_code == 2, result.output
    assert [r[0] for r in FakeClient.instances[0].requests] == ["GET"]


def test_503_followup_unavailable_is_indeterminate_once():
    error = budget_rules.ApiError(
        status_code=503, code="http_503", message="Unavailable", hint=None, detail=None
    )
    FakeClient.responses = [response(), response(), error, error]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "90"])
    assert result.exit_code == 1 and "budget_update_indeterminate" in result.stderr
    assert [r[0] for r in FakeClient.instances[0].requests] == [
        "GET",
        "GET",
        "PUT",
        "GET",
    ]


def test_payload_rule_limit_is_a_local_usage_error():
    rules = [
        {
            "type": "budget_threshold",
            "percent": i,
            "rule_key": f"key-{i}",
            "channel_ids": [],
            "routing": ROUTING,
        }
        for i in range(1, 51)
    ]
    FakeClient.responses = [
        response(
            thresholds=rules,
            constraints={**BUDGET_RESPONSE["constraints"], "max_thresholds": -1},
        )
    ]
    result = invoke(["rule", "add", "--type", "budget_threshold", "--percent", "60"])
    assert result.exit_code == 2, result.output
    assert [r[0] for r in FakeClient.instances[0].requests] == ["GET"]
