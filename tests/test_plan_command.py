"""Plan command contract tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import plan as plan_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "a1" * 32


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
    "cancel_at_period_end": False,
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
        auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
        auth.JwtCredential(
            access_token="access-secret", refresh_token="refresh-secret"
        ),
    ],
)
def test_plan_accepts_project_key_or_jwt_and_matches_json_golden(
    credential: auth.ResolvedCredential,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json=BILLING_STATUS)]

    result = runner.invoke(app, ["--json", "plan"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [("GET", "/api/v1/billing/status", {})]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "plan-team.json")
    assert "cus_private" not in result.stdout
    assert "access-secret" not in result.stdout
    assert VALID_KEY not in result.stdout


def test_plan_table_renders_entitlements_limits_and_dashboard_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="keyring")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json=BILLING_STATUS)]

    result = runner.invoke(app, ["--no-color", "plan"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert_matches_golden(result.stdout, GOLDEN_DIR / "plan-team-table.txt")


def test_plan_project_key_without_account_context_preserves_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda: credential)
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
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    monkeypatch.setattr(plan_module, "resolve_credential", lambda: credential)
    monkeypatch.setattr(plan_module, "SolwynClient", FakeClient)
    FakeClient.responses = [httpx.Response(200, json={"tier": "team"})]

    result = runner.invoke(app, ["--json", "plan"])

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_plan_response"
