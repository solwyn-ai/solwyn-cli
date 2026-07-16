"""Contract tests for the request models vendored by the CLI."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from solwyn_cli._vendor.wire import (
    BudgetCheckRequest,
    BudgetConfirmRequest,
    BudgetUpdateRequest,
    LoginRequest,
    ProjectCreateRequest,
    ProjectKeyRotateRequest,
    RefreshRequest,
)


@pytest.mark.parametrize(
    ("model", "fields", "required"),
    [
        (
            LoginRequest,
            {"email", "password", "plan", "billing_interval"},
            {"email", "password"},
        ),
        (RefreshRequest, {"refresh_token"}, {"refresh_token"}),
        (
            ProjectCreateRequest,
            {"name", "budget_limit", "budget_period", "budget_mode", "scope"},
            {"name"},
        ),
        (
            ProjectKeyRotateRequest,
            {"grace_seconds", "scope"},
            set(),
        ),
        (
            BudgetUpdateRequest,
            {
                "limit_usd",
                "period",
                "mode",
                "sub_period",
                "sub_limit_usd",
                "thresholds",
            },
            {"limit_usd", "period", "mode", "thresholds"},
        ),
        (
            BudgetCheckRequest,
            {
                "estimated_input_tokens",
                "model",
                "provider",
                "fallback_providers",
                "fallback_models",
                "agent_run_id",
            },
            {"estimated_input_tokens", "model", "provider"},
        ),
        (
            BudgetConfirmRequest,
            {
                "reservation_id",
                "model",
                "provider",
                "is_provider_fallback",
                "call_id",
                "token_details",
                "provider_region",
                "service_tier",
            },
            {"reservation_id", "model", "provider", "call_id", "token_details"},
        ),
    ],
)
def test_request_model_field_sets_and_required_fields(
    model: type, fields: set[str], required: set[str]
) -> None:
    schema = model.model_json_schema()

    assert set(schema["properties"]) == fields
    assert set(schema.get("required", [])) == required


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (LoginRequest, {"email": "dev@example.com", "password": "secret"}),
        (RefreshRequest, {"refresh_token": "refresh"}),
        (ProjectCreateRequest, {"name": "Example"}),
        (
            BudgetUpdateRequest,
            {
                "limit_usd": 10,
                "period": "monthly",
                "mode": "hard_deny",
                "thresholds": [],
            },
        ),
        (
            BudgetCheckRequest,
            {
                "estimated_input_tokens": 10,
                "model": "test-model",
                "provider": "openai",
            },
        ),
        (
            BudgetConfirmRequest,
            {
                "reservation_id": "reservation",
                "model": "test-model",
                "provider": "openai",
                "call_id": "call-id",
                "token_details": {},
            },
        ),
    ],
)
def test_request_models_forbid_unknown_fields(model: type, payload: dict) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**payload, "unexpected": True})


def test_login_and_project_create_preserve_wire_defaults() -> None:
    login = LoginRequest(email="dev@example.com", password="secret")
    project = ProjectCreateRequest(name="Example")

    assert login.model_dump() == {
        "email": "dev@example.com",
        "password": "secret",
        "plan": None,
        "billing_interval": None,
    }
    assert project.model_dump() == {
        "name": "Example",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "alert_only",
        "scope": "full",
    }
    # The declared scope default documents the API's own default; it reaches the
    # wire only when a caller sets it, which is what keeps omission faithful.
    assert project.model_dump(exclude_unset=True) == {"name": "Example"}
    assert ProjectCreateRequest(name="Example", scope="read_only").model_dump(
        exclude_unset=True
    ) == {"name": "Example", "scope": "read_only"}


def test_login_request_redacts_password_but_serializes_wire_value() -> None:
    password = "synthetic-password-value"

    request = LoginRequest(email="dev@example.com", password=password)

    assert password not in repr(request)
    assert password not in str(request)
    assert request.model_dump()["password"] == password


def test_budget_update_accepts_each_rule_union_and_preserves_defaults() -> None:
    rules = [
        {"percent": 80},
        {
            "type": "forecast_overrun",
            "spend_threshold_usd": 120,
        },
        {"type": "daily_anomaly", "multiplier": 2},
        {"type": "unit_cost_anomaly", "multiplier": 3},
    ]

    request = BudgetUpdateRequest(
        limit_usd=100,
        period="monthly",
        mode="hard_deny",
        thresholds=rules,
    )

    assert [rule.type for rule in request.thresholds] == [
        "budget_threshold",
        "forecast_overrun",
        "daily_anomaly",
        "unit_cost_anomaly",
    ]
    assert request.thresholds[0].channel_ids == []
    assert request.thresholds[0].snoozed_until is None
    assert request.thresholds[0].paused is False
    assert request.thresholds[1].period == "month"
    assert request.thresholds[2].lookback_days == 7


def test_budget_call_models_preserve_failover_token_and_tier_contract() -> None:
    check = BudgetCheckRequest(
        estimated_input_tokens=42,
        model="test-model",
        provider="openai",
        fallback_providers=["anthropic", "bedrock"],
        fallback_models=["fallback-1", "fallback-2"],
    )
    confirm = BudgetConfirmRequest(
        reservation_id="reservation",
        model="test-model",
        provider="bedrock",
        is_provider_fallback=True,
        call_id="call-id",
        token_details={"input_tokens": 20, "output_tokens": 5},
        provider_region="us-east-1",
        service_tier="optimized",
    )

    assert check.fallback_providers == ["anthropic", "bedrock"]
    assert check.fallback_models == ["fallback-1", "fallback-2"]
    assert confirm.token_details.input_tokens == 20
    assert confirm.token_details.cache_creation_1h_tokens == 0
    assert confirm.provider_region == "us-east-1"
    assert confirm.service_tier == "optimized"


def test_budget_update_serializes_rule_datetimes_as_wire_strings() -> None:
    request = BudgetUpdateRequest(
        limit_usd=100,
        period="monthly",
        mode="alert_only",
        thresholds=[
            {
                "percent": 90,
                "snoozed_until": datetime(2026, 7, 10, 12, tzinfo=UTC),
            }
        ],
    )

    assert request.model_dump(mode="json")["thresholds"][0]["snoozed_until"] == (
        "2026-07-10T12:00:00Z"
    )
