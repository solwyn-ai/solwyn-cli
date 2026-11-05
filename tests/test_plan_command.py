"""Plan command contract tests."""

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

from solwyn_cli import auth
from solwyn_cli.commands import plan as plan_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "a1" * 32


BILLING_STATUS = {
    "tier": "team",
    "billing_interval": "year",
    "limits": {
        "projects": -1,
        "requests_per_month": 500000,
        "api_keys_per_project": 10,
        "webhook_targets_per_project": 5,
    },
    "capabilities": {
        "analytics": {
            "allowed_ranges": ["7d", "30d", "90d"],
            "allowed_group_by": [
                "day",
                "model",
                "provider",
                "status",
                "service_tier",
                "provider_region",
                "failover_reason",
                "agent_run",
            ],
            "tags": True,
            "csv_export_mode": "full",
            "cost_events_paginated": True,
        },
        "budget": {
            "allowed_modes": ["alert_only", "hard_deny"],
            "allowed_periods": ["daily", "weekly", "monthly"],
            "sub_period_enabled": True,
            "max_thresholds": 10,
            "max_hard_cap_projects": -1,
            "account_spend_alerts": True,
        },
        "notifications": {
            "notification_channels": ["slack", "email"],
            "sms_allowed_events": ["budget_exceeded"],
            "email_allowed_events": ["budget_warning", "budget_exceeded"],
        },
        "api_keys": {"expiry_configurable": True},
        "ingest": {"rate_limit_per_second": 30},
        "enforcement": {"checks_per_minute": 12000},
        "failover_entitlement": True,
        "data_retention_days": 180,
    },
    "status": "active",
    "current_period_end": "2026-10-01T00:00:00Z",
    "cancel_at": None,
    "scheduled_plan_change": None,
    "grace_period_until": None,
    "stripe_customer_id": "cus_private",
    "recent_invoices": [],
    "pending_plan": None,
}


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
def reset_fake_client() -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    yield


@pytest.mark.parametrize(
    "credential",
    [
        auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        login_credential("access-secret"),
    ],
)
def test_plan_accepts_project_key_or_jwt_and_matches_json_golden(
    credential: auth.ResolvedCredential,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_module, "resolve_credential", lambda **_: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json=BILLING_STATUS)]

    result = runner.invoke(app, ["--json", "plan"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert same_credential(FakeClient.instances[0].credential, credential)
    assert FakeClient.instances[0].requests == [("GET", "/api/v1/billing/status", {})]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "plan-team.json")
    assert "cus_private" not in result.stdout
    assert "access-secret" not in result.stdout
    assert VALID_KEY not in result.stdout


def test_plan_table_renders_entitlements_limits_and_dashboard_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source="keyring")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda **_: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json=BILLING_STATUS)]

    result = runner.invoke(app, ["--no-color", "plan"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "plan-team-table.txt")


def test_plan_project_key_without_account_context_preserves_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda **_: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [
        ApiError(
            status_code=403,
            code="http_error",
            message="This endpoint requires account context",
            hint=None,
            detail="This endpoint requires account context",
        )
    ]

    result = runner.invoke(app, ["--json", "plan"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["message"] == "This endpoint requires account context"
    assert error["detail"] == "This endpoint requires account context"


def test_plan_invalid_billing_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda **_: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json={"tier": "team"})]

    result = runner.invoke(app, ["--json", "plan"])

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_plan_response"


@pytest.mark.parametrize("output_format", ["json", "table"])
@pytest.mark.parametrize("cancelling", [False, True])
def test_plan_exposes_subscription_lifecycle(
    monkeypatch: pytest.MonkeyPatch, output_format: str, cancelling: bool
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: login_credential("test-access"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    lifecycle = {
        "current_period_end": "2027-10-01T00:00:00Z",
        "cancel_at": "2027-10-01T00:00:00Z" if cancelling else None,
        "grace_period_until": "2026-10-21T00:00:00Z",
        "scheduled_plan_change": {
            "target_tier": "production",
            "target_billing_interval": "month",
            "effective_at": "2027-10-01T00:00:00Z",
        },
        "pending_plan": {"tier": "scale", "billing_interval": "year"},
    }
    FakeClient.responses = [
        httpx.Response(200, json={**BILLING_STATUS, **lifecycle, "status": "past_due"})
    ]
    result = runner.invoke(
        app, ["--json" if output_format == "json" else "--no-color", "plan"]
    )
    assert result.exit_code == 0, result.stderr
    if output_format == "json":
        payload = json.loads(result.stdout)
        for field, value in lifecycle.items():
            assert payload[field] == value
        assert "cancel_at_period_end" not in payload
    else:
        assert ("Ends" if cancelling else "Renews") in result.stdout
        assert "Cancel at" in result.stdout
        assert "Grace until" in result.stdout
        assert "Scheduled plan" in result.stdout
        assert "Pending plan" in result.stdout
        assert "production / month" in result.stdout
        assert "scale / year" in result.stdout
        assert "2027-10-01" in result.stdout
        assert "2026-10-21" in result.stdout


@pytest.mark.parametrize(
    "targets",
    [
        {"target_tier": None, "target_billing_interval": None},
        {"target_tier": "team", "target_billing_interval": "year"},
    ],
)
def test_plan_accepts_required_nullable_scheduled_targets(
    monkeypatch: pytest.MonkeyPatch, targets: dict[str, str | None]
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    change = {**targets, "effective_at": "2027-10-01T00:00:00Z"}
    FakeClient.responses = [
        httpx.Response(200, json={**BILLING_STATUS, "scheduled_plan_change": change})
    ]
    result = runner.invoke(app, ["--json", "plan"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["scheduled_plan_change"] == change


@pytest.mark.parametrize(
    "missing", ["target_tier", "target_billing_interval", "effective_at"]
)
def test_plan_rejects_incomplete_scheduled_change(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    change = {
        "target_tier": None,
        "target_billing_interval": None,
        "effective_at": "2027-10-01T00:00:00Z",
    }
    del change[missing]
    FakeClient.responses = [
        httpx.Response(200, json={**BILLING_STATUS, "scheduled_plan_change": change})
    ]
    result = runner.invoke(app, ["--json", "plan"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_plan_response"


@pytest.mark.parametrize("output_format", ["json", "table"])
def test_plan_optional_lifecycle_can_be_omitted_for_free_accounts(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    minimal = {field: BILLING_STATUS[field] for field in ("limits", "capabilities")}
    FakeClient.responses = [
        httpx.Response(200, json={**minimal, "tier": "free", "billing_interval": None})
    ]
    result = runner.invoke(
        app, ["--json" if output_format == "json" else "--no-color", "plan"]
    )
    assert result.exit_code == 0, result.stderr
    if output_format == "json":
        payload = json.loads(result.stdout)
        for field in (
            "current_period_end",
            "cancel_at",
            "grace_period_until",
            "scheduled_plan_change",
            "pending_plan",
        ):
            assert payload[field] is None
    else:
        assert "Period end" in result.stdout
        assert "Renews" not in result.stdout
        assert "Ends" not in result.stdout
        assert "Grace until" not in result.stdout
        assert "Scheduled plan" in result.stdout
        assert "Pending plan" in result.stdout
        assert_matches_golden(result.stdout, GOLDEN_DIR / "plan-free-table.txt")


@pytest.mark.parametrize(
    "lifecycle, label",
    [
        ({"current_period_end": None}, "Period end"),
        (
            {"current_period_end": None, "cancel_at": "2026-10-01T00:00:00Z"},
            "Period end",
        ),
        ({"tier": "free"}, "Period end"),
        ({"tier": "free", "cancel_at": "2026-10-01T00:00:00Z"}, "Period end"),
        ({"status": None}, "Period end"),
        *[
            ({"status": status}, "Period end")
            for status in (
                "unpaid",
                "canceled",
                "incomplete_expired",
                "paused",
                "incomplete",
                "unknown",
            )
        ],
        *[
            (
                {
                    "status": status,
                    "cancel_at": "2026-10-01T00:00:00Z" if cancelling else None,
                },
                "Ends" if cancelling else "Renews",
            )
            for status in ("active", "trialing", "past_due")
            for cancelling in (False, True)
        ],
    ],
)
def test_plan_period_end_label_preserves_exact_json(
    monkeypatch: pytest.MonkeyPatch, lifecycle: dict[str, Any], label: str
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    response = {**BILLING_STATUS, **lifecycle}
    FakeClient.responses = [httpx.Response(200, json=response)]
    result = runner.invoke(app, ["--no-color", "plan"])
    assert result.exit_code == 0, result.stderr
    assert label in result.stdout
    for other in {"Period end", "Renews", "Ends"} - {label}:
        assert other not in result.stdout

    FakeClient.responses = [httpx.Response(200, json=response)]
    result = runner.invoke(app, ["--json", "plan"])
    assert result.exit_code == 0, result.stderr
    expected = json.loads((GOLDEN_DIR / "plan-team.json").read_text()) | lifecycle
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "field", ["current_period_end", "cancel_at", "grace_period_until"]
)
def test_plan_rejects_invalid_lifecycle_timestamps(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(
        plan_module,
        "resolve_credential",
        lambda **_: auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
    )
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [
        httpx.Response(200, json={**BILLING_STATUS, field: "invalid-date"})
    ]
    result = runner.invoke(app, ["--json", "plan"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_plan_response"
