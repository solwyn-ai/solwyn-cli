"""Provider visibility command contract tests."""

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
from solwyn_cli.commands import providers as providers_module
from solwyn_cli.errors import ApiError, error_from_response
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "e5" * 32
ACCESS_TOKEN = "providers-access-token"
REFRESH_TOKEN = "providers-refresh-token"

FAILOVERS_RESPONSE = [
    {
        "requested_provider": "openai",
        "requested_model": "gpt-5",
        "served_provider": "anthropic",
        "served_model": "claude-sonnet-4",
        "failover_reason": "primary_error",
        "failover_error_class": "TimeoutError",
        "event_count": 4,
        "total_cost_usd": 1.234567,
        "first_seen": "2026-09-01T10:00:00Z",
        "last_seen": "2026-09-05T12:30:00Z",
        "is_unpriced": False,
    },
    {
        "requested_provider": None,
        "requested_model": None,
        "served_provider": "bedrock",
        "served_model": "opaque-profile",
        "failover_reason": "capacity_shift",
        "failover_error_class": None,
        "event_count": 2,
        "total_cost_usd": 0.0,
        "first_seen": "2026-09-02T09:00:00Z",
        "last_seen": "2026-09-03T09:00:00Z",
        "is_unpriced": True,
    },
]

UNTRACKED_RESPONSE = [
    {
        "provider": "openai",
        "client_shape": "chat.completions",
        "mode": "sync",
        "surface": "responses",
        "rule_kind": "direct",
        "capability_scope": "responses.write",
        "posture": "unsupported",
        "occurrences": 17,
        "first_seen_at": "2026-08-15T10:00:00Z",
        "last_seen_at": "2026-09-05T12:30:00Z",
    }
]

BILLING_STATUS_RESPONSE = {
    "tier": "free",
    "billing_interval": None,
    "limits": {"projects": 3},
    "capabilities": {
        "analytics": {
            "allowed_ranges": ["7d"],
            "allowed_group_by": ["day"],
            "tags": False,
            "csv_export_mode": "none",
            "cost_events_paginated": False,
        },
        "budget": {
            "allowed_modes": ["alert_only"],
            "allowed_periods": ["monthly"],
            "sub_period_enabled": False,
            "max_thresholds": 1,
            "max_hard_cap_projects": 0,
            "account_spend_alerts": False,
        },
        "notifications": {
            "notification_channels": ["email"],
            "sms_allowed_events": [],
            "email_allowed_events": ["budget_threshold"],
        },
        "api_keys": {"expiry_configurable": False},
        "ingest": {"rate_limit_per_second": 10},
        "enforcement": {"checks_per_minute": 300},
        "failover_entitlement": False,
        "data_retention_days": 30,
    },
    "status": None,
    "current_period_end": None,
    "cancel_at": None,
    "scheduled_plan_change": None,
    "grace_period_until": None,
    "stripe_customer_id": None,
    "recent_invoices": [],
    "pending_plan": None,
}


class FakeClient:
    """Minimal closeable command client with queued responses."""

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
    monkeypatch.setattr(providers_module, "SolwynClient", FakeClient, raising=False)
    yield


def _key(**_: object) -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


def test_providers_commands_are_registered() -> None:
    root = runner.invoke(app, ["--help"])
    providers = runner.invoke(app, ["providers", "--help"])

    assert root.exit_code == 0
    assert "providers" in root.stdout
    assert providers.exit_code == 0
    assert "failovers" in providers.stdout
    assert "untracked" in providers.stdout


@pytest.mark.parametrize(
    "credential",
    [
        auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        login_credential(ACCESS_TOKEN),
    ],
)
def test_failovers_supports_key_and_jwt_and_preserves_the_wire_payload(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        providers_module, "resolve_credential", lambda **_: credential, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=FAILOVERS_RESPONSE)]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "failovers",
            "--range",
            "24h",
        ],
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests == [
        (
            "GET",
            "/api/v1/projects/proj_be76331b95dfc399cd776d2f/providers/failovers",
            {"params": {"range": "24h"}},
        )
    ]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "providers-failovers.json")
    assert result.stderr == "Showing top 50 groups of an unknown total.\n"


def test_failovers_table_labels_known_reasons_and_passes_unknown_reasons_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [httpx.Response(200, json=FAILOVERS_RESPONSE)]

    result = runner.invoke(
        app,
        ["--no-color", "-p", "proj_be76331b95dfc399cd776d2f", "providers", "failovers"],
    )

    assert result.exit_code == 0
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / "providers-failovers-table.txt",
    )
    assert "Primary error" in result.stdout
    assert "capacity_shift" in result.stdout
    assert "unpriced" in result.stdout
    assert result.stderr == "Showing top 50 groups of an unknown total.\n"


@pytest.mark.parametrize("time_range", ["24h", "7d", "30d"])
def test_failovers_accepts_only_the_frozen_ranges(
    monkeypatch: pytest.MonkeyPatch,
    time_range: str,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [httpx.Response(200, json=[])]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "failovers",
            "--range",
            time_range,
        ],
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[0][2] == {"params": {"range": time_range}}


@pytest.mark.parametrize(
    "credential",
    [
        auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        login_credential(ACCESS_TOKEN),
    ],
)
def test_untracked_supports_key_and_jwt_and_prints_effective_billing_retention(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
) -> None:
    monkeypatch.setattr(
        providers_module, "resolve_credential", lambda **_: credential, raising=False
    )
    FakeClient.responses = [
        httpx.Response(200, json=BILLING_STATUS_RESPONSE),
        httpx.Response(200, json=UNTRACKED_RESPONSE),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "untracked",
            "--range",
            "90d",
        ],
    )

    assert result.exit_code == 0
    assert same_credential(FakeClient.instances[0].credential, credential)
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/billing/status", {}),
        (
            "GET",
            "/api/v1/projects/proj_be76331b95dfc399cd776d2f/untracked-surfaces",
            {"params": {"range": "90d"}},
        ),
    ]
    assert_matches_golden(result.stdout, GOLDEN_DIR / "providers-untracked.json")
    assert result.stderr == "Showing top 25 observations of an unknown total.\n"


def test_untracked_table_marks_occurrences_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [
        httpx.Response(200, json=BILLING_STATUS_RESPONSE),
        httpx.Response(200, json=UNTRACKED_RESPONSE),
    ]

    result = runner.invoke(
        app,
        [
            "--no-color",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "untracked",
            "--range",
            "90d",
        ],
    )

    assert result.exit_code == 0
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / "providers-untracked-table.txt",
    )
    assert "30 days" in result.stdout
    assert "advisory; never billing truth" in result.stdout


def test_untracked_keeps_the_window_unknown_when_entitlement_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [
        ApiError(
            status_code=503,
            code="service_unavailable",
            message="Service unavailable.",
            hint=None,
            detail=None,
        ),
        httpx.Response(200, json=[]),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "untracked",
            "--range",
            "30d",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["requested_window_days"] == 30
    assert payload["effective_window_days"] is None
    assert payload["data_retention_days"] is None
    assert payload["window_source"] == "unknown"
    assert [path for _, path, _ in FakeClient.instances[0].requests] == [
        "/api/v1/billing/status",
        "/api/v1/projects/proj_be76331b95dfc399cd776d2f/untracked-surfaces",
    ]


@pytest.mark.parametrize("time_range", ["7d", "30d", "90d"])
def test_untracked_accepts_only_the_frozen_ranges(
    monkeypatch: pytest.MonkeyPatch,
    time_range: str,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [
        httpx.Response(200, json=BILLING_STATUS_RESPONSE),
        httpx.Response(200, json=[]),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "-p",
            "proj_be76331b95dfc399cd776d2f",
            "providers",
            "untracked",
            "--range",
            time_range,
        ],
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests[1][2] == {"params": {"range": time_range}}


def test_provider_commands_reject_missing_project_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_calls: list[None] = []
    monkeypatch.setattr(
        providers_module,
        "resolve_credential",
        lambda **_: resolution_calls.append(None),
        raising=False,
    )

    result = runner.invoke(
        app,
        ["--json", "providers", "failovers"],
        env={"SOLWYN_PROJECT_ID": ""},
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_required"
    assert resolution_calls == []
    assert FakeClient.instances == []


def test_provider_failovers_fails_closed_on_extended_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers_module, "resolve_credential", _key, raising=False)
    FakeClient.responses = [
        httpx.Response(200, json=[{**FAILOVERS_RESPONSE[0], "future_field": True}])
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_be76331b95dfc399cd776d2f", "providers", "failovers"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "invalid_provider_failovers_response"
    assert result.stdout == ""


def test_provider_cross_project_key_403_has_an_actionable_project_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _key()
    monkeypatch.setattr(
        providers_module, "resolve_credential", lambda **_: credential, raising=False
    )
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                403,
                json={"detail": "Cannot access a different project"},
            )
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "-p", "proj_d0941e68da8f38151ff86a61", "providers", "failovers"],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "http_error"
    assert error["message"] == "Cannot access a different project"
    assert error["detail"] == "Cannot access a different project"
    assert error["hint"] == (
        "Select the project associated with this key, or configure a key "
        "for the selected project."
    )
    assert VALID_KEY not in result.stderr + repr(result.exception)
