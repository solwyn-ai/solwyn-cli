"""Guard every response field currently consumed by the CLI."""

import copy
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from solwyn_cli.commands import budget, init, projects


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
# P3+ command tasks extend this registry when they begin reading response fields.
RESPONSE_READS = {
    "SpendControlAttestation": (
        "schema_version",
        "generated_at",
        "scope",
        "range",
        "window",
        "spend_summary",
        "budgets",
        "alerts",
        "account_spend_alert",
        "subscription",
        "audit",
    ),
    "AttestationScope": ("type", "account_id", "project_id"),
    "AttestationWindow": (
        "requested_from",
        "requested_to",
        "effective_from",
        "effective_to",
        "retention_clamped",
    ),
    "AttestationSpendSummary": (
        "real_spend_usd",
        "real_call_count",
        "denied_spend_usd",
        "denial_count",
    ),
    "AttestationBudget": (
        "project_id",
        "project_name",
        "archived",
        "archived_at",
        "budget_limit_usd",
        "budget_period",
        "budget_mode",
        "sub_period",
        "sub_limit_usd",
        "rules",
    ),
    "AttestationAlertEvent": (
        "id",
        "project_id",
        "account_id",
        "scope",
        "rule_key",
        "emitted_at",
        "event_type",
        "rule_type",
        "value_at_event",
        "threshold_at_event",
        "period_key",
        "linked_event_ids",
        "metadata_json",
    ),
    "AttestationAlerts": (
        "begins_at",
        "delivery_evidence_available",
        "delivered_count",
        "coverage_note",
        "events",
    ),
    "AttestationAccountSpendAlert": (
        "evidence_available",
        "configured",
        "spend_threshold_usd",
        "enabled",
        "snoozed_until",
        "channel_ids",
        "coverage_note",
    ),
    "AttestationAuditEvent": (
        "id",
        "account_id",
        "actor_user_id",
        "action",
        "target_type",
        "target_id",
        "detail",
        "created_at",
    ),
    "AttestationSubscriptionSnapshot": (
        "tier",
        "billing_interval",
        "status",
        "pending_update_exists",
        "current_period_start",
        "current_period_end",
        "cancel_at_period_end",
        "pause_collection_exists",
        "grace_period_until",
        "raw_status_at",
        "created_at",
        "updated_at",
    ),
    "AttestationSubscription": (
        "current_tier",
        "snapshot",
        "changes",
        "coverage_note",
    ),
    "AttestationAudit": ("begins_at", "coverage_note", "events"),
    "SubscriptionStatusResponse": (
        "tier",
        "billing_interval",
        "limits",
        "capabilities",
        "status",
        "current_period_end",
        "cancel_at_period_end",
        "grace_period_until",
        "stripe_customer_id",
        "recent_invoices",
        "pending_plan",
    ),
    "TierCapabilities": (
        "analytics",
        "budget",
        "notifications",
        "api_keys",
        "ingest",
        "enforcement",
        "failover_entitlement",
        "data_retention_days",
    ),
    "AnalyticsCapabilities": (
        "allowed_ranges",
        "allowed_group_by",
        "tags",
        "csv_export_mode",
        "cost_events_paginated",
    ),
    "BudgetCapabilities": (
        "allowed_modes",
        "allowed_periods",
        "sub_period_enabled",
        "max_thresholds",
        "max_hard_cap_projects",
        "account_spend_alerts",
    ),
    "NotificationCapabilities": (
        "notification_channels",
        "sms_allowed_events",
        "email_allowed_events",
    ),
    "ApiKeyCapabilities": ("expiry_configurable",),
    "IngestCapabilities": ("rate_limit_per_second",),
    "EnforcementCapabilities": ("checks_per_minute",),
    "InvoiceSummary": ("hosted_url", "amount_due_cents", "period_end", "status"),
    "PendingPlan": ("tier", "billing_interval"),
    "TokenResponse": ("access_token", "refresh_token", "token_type"),
    "Error402Response": (
        "detail.code",
        "detail.message",
        "detail.cta_path",
    ),
    "UpgradeRequiredDetail": ("code", "message", "cta_path", "allowed_values"),
    "QuotaExceededDetail": ("code", "message", "cta_path"),
    "ProjectLimitReachedDetail": ("code", "message", "cta_path"),
    # Generic HTTP error parsing consumes detail; retry timing uses the header.
    "RateLimitErrorResponse": ("detail",),
    "MeResponse": (
        "user_id",
        "email",
        "display_name",
        "subscription_tier",
    ),
    "ProjectResponse": (
        "id",
        "name",
        "budget_limit",
        "budget_period",
        "budget_mode",
        "created_at",
    ),
    "ProjectCreateResponse": (
        "id",
        "name",
        "budget_limit",
        "budget_period",
        "budget_mode",
        "created_at",
        "key",
        "key_prefix",
        "scope",
    ),
    "ProjectKeyResponse": (
        "id",
        "key_prefix",
        "name",
        "last_used_at",
        "expires_at",
        "rotation_grace_until",
        "created_at",
        "scope",
    ),
    "ProjectKeyRotateResponse": (
        "id",
        "key",
        "key_prefix",
        "rotation_grace_until",
        "scope",
    ),
    "BudgetStatusResponse": (
        "project_id",
        "budget_limit",
        "current_usage",
        "remaining_budget",
        "budget_period",
        "budget_mode",
        "period_date",
        "utilization_pct",
        "thresholds",
        "sub_period",
        "sub_limit",
        "constraints",
        "outage_overspend_ceiling_usd",
        "lease_float_usd",
    ),
    "ThresholdStateResponse": (
        "type",
        "rule_key",
        "percent",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "ForecastOverrunStateResponse": (
        "type",
        "rule_key",
        "spend_threshold_usd",
        "period",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "RunawayRunStateResponse": (
        "mode",
        "type",
        "rule_key",
        "spend_threshold_usd",
        "call_threshold",
        "window_minutes",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "DailyAnomalyStateResponse": (
        "type",
        "rule_key",
        "multiplier",
        "lookback_days",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "UnitCostAnomalyStateResponse": (
        "type",
        "rule_key",
        "multiplier",
        "lookback_days",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "ScopedBudgetStateResponse": (
        "match_value",
        "current_usage",
        "type",
        "rule_key",
        "scope",
        "match",
        "limit",
        "mode",
        "channel_ids",
        "routing",
        "snoozed_until",
        "paused",
    ),
    "AlertRouting": (
        "will_send",
        "email",
        "channels",
        "source",
        "muted",
    ),
    "RoutingChannel": ("id", "name"),
    "RoutingMuted": ("reason", "severity_floor"),
    "BudgetConstraintsResponse": (
        "allowed_modes",
        "allowed_periods",
        "sub_period_enabled",
        "max_thresholds",
    ),
    # failover_directive is deliberately unread — declined: it is only attached
    # when a request opts in via failover_directive_version, which the CLI does
    # not send (see the request-side registry in test_request_drift.py).
    "BudgetCheckResponse": (
        "allowed",
        "remaining_budget",
        "reservation_id",
        "mode",
        "budget_limit",
        "current_usage",
        "denied_by_period",
        "project_id",
        "price_hints",
    ),
    "ProviderHealthResponse": (
        "provider",
        "total_calls",
        "success_count",
        "error_count",
        "failover_count",
        "success_rate",
        "avg_latency_ms",
        "circuit_state",
    ),
    "CostQueryResponse": (
        "project_id",
        "range",
        "group_by",
        "groups",
        "truncated",
        "savings",
        "window",
    ),
    "CostGroup": (
        "group_key",
        "provider",
        "modality",
        "agent_run_name",
        "total_cost",
        "total_input_tokens",
        "total_output_tokens",
        "call_count",
        "unpriced_call_count",
        "local_call_count",
    ),
    "CostWindow": (
        "requested_from",
        "requested_to",
        "effective_from",
        "effective_to",
        "retention_clamped",
    ),
    "SavingsSummary": (
        "saved_cost",
        "saved_cost_upper",
        "saved_input_tokens",
        "blocked_call_count",
    ),
    "CostEventResponse": (
        "id",
        "project_id",
        "agent_run_id",
        "provider",
        "model",
        "modality",
        "provider_region",
        "service_tier",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "image_input_tokens",
        "image_output_tokens",
        "cost",
        "denied_cost",
        "deny_source",
        "deny_reason",
        "estimated_cost",
        "is_unpriced",
        "is_estimated",
        "media",
        "tags",
        "status",
        "is_model_fallback",
        "latency_ms",
        "sdk_instance_id",
        "created_at",
    ),
    "CostEventsResponse": ("items", "next_cursor", "has_more", "window"),
    "CostTotalResponse": (
        "project_id",
        "range",
        "total_cost",
        "total_input_tokens",
        "total_output_tokens",
        "call_count",
        "unpriced_call_count",
        "local_call_count",
        "savings",
        "window",
    ),
    "MediaBreakdown": (
        "image_count",
        "generation_count",
        "video_seconds",
        "audio_seconds",
        "input_characters",
        "resolution",
        "quality",
        "is_estimated",
    ),
}
# method/path -> (success status, JSON shape, consumed response component)
OPERATION_RESPONSE_BINDINGS = {
    ("GET", "/api/v1/projects/{project_id}/spend-control-attestation"): (
        "200",
        "object",
        "SpendControlAttestation",
    ),
    ("GET", "/api/v1/account/spend-control-attestation"): (
        "200",
        "object",
        "SpendControlAttestation",
    ),
    ("GET", "/api/v1/projects/{project_id}/costs/events"): (
        "200",
        "object",
        "CostEventsResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/costs/total"): (
        "200",
        "object",
        "CostTotalResponse",
    ),
    ("GET", "/api/v1/billing/status"): ("200", "object", "SubscriptionStatusResponse"),
    ("POST", "/api/v1/auth/login"): ("200", "object", "TokenResponse"),
    ("POST", "/api/v1/auth/refresh"): ("200", "object", "TokenResponse"),
    ("GET", "/api/v1/account/me"): ("200", "object", "MeResponse"),
    ("GET", "/api/v1/projects"): ("200", "array", "ProjectResponse"),
    ("POST", "/api/v1/projects"): (
        "201",
        "object",
        "ProjectCreateResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}"): (
        "200",
        "object",
        "ProjectResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/budget"): (
        "200",
        "object",
        "BudgetStatusResponse",
    ),
    ("PUT", "/api/v1/projects/{project_id}/budget"): (
        "200",
        "object",
        "BudgetStatusResponse",
    ),
    ("POST", "/api/v1/budgets/check"): (
        "200",
        "object",
        "BudgetCheckResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/providers"): (
        "200",
        "array",
        "ProviderHealthResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/costs"): (
        "200",
        "object",
        "CostQueryResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/key"): (
        "200",
        "object",
        "ProjectKeyResponse",
    ),
    ("POST", "/api/v1/projects/{project_id}/key/rotate"): (
        "200",
        "object",
        "ProjectKeyRotateResponse",
    ),
}
NO_CONTENT_OPERATION_BINDINGS = {
    ("DELETE", "/api/v1/projects/{project_id}/key/{key_id}"): "204",
}
_BUDGET_THRESHOLD_COMPONENTS = {
    "budget_threshold": "ThresholdStateResponse",
    "forecast_overrun": "ForecastOverrunStateResponse",
    "runaway_run": "RunawayRunStateResponse",
    "daily_anomaly": "DailyAnomalyStateResponse",
    "unit_cost_anomaly": "UnitCostAnomalyStateResponse",
    "scoped_budget": "ScopedBudgetStateResponse",
}
# Rule types the live API can return that the budget commands do NOT model.
#
# Empty: the CLI now models every rule type the spec declares. Keep the registry
# — it is how a future unmodelled rule type is declared, and an unmodelled type
# that is NOT registered here still trips the guard rather than slipping in.
#
# Any entry added here is an OPEN DEFECT, NOT A BENIGN OMISSION. _BudgetStatusData's
# threshold union is closed and extra="forbid", so budget status, budget set, and
# the MCP budget_status tool hard-fail (exit 1, invalid_budget_response) against
# the live API for any project that configures an unmodelled rule. Failing closed
# is the deliberate, safety-correct behavior for a budget tool — silently dropping
# an unknown guardrail would misrepresent the user's spend protection — so the fix
# is always to ADOPT the rule type, not to relax the union.
_UNMODELLED_THRESHOLD_COMPONENTS: dict[str, str] = {}
_ALL_THRESHOLD_COMPONENTS = {
    **_BUDGET_THRESHOLD_COMPONENTS,
    **_UNMODELLED_THRESHOLD_COMPONENTS,
}


def _assert_field_path(
    schema: dict[str, Any],
    document: dict[str, Any],
    segments: tuple[str, ...],
    registered_path: str,
    context: str,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        component = reference.rsplit("/", maxsplit=1)[-1]
        target = document["components"]["schemas"][component]
        _assert_field_path(target, document, segments, registered_path, component)
        return

    if not segments:
        return

    all_of = schema.get("allOf")
    if all_of is not None:
        candidates = list(all_of)
        if schema.get("properties"):
            candidates.insert(0, {"properties": schema["properties"]})
        for branch in candidates:
            try:
                _assert_field_path(branch, document, segments, registered_path, context)
            except AssertionError:
                continue
            return
        raise AssertionError(
            f"{registered_path}: no allOf branch provides the consumed field "
            f"in {context}"
        )

    union = schema.get("oneOf")
    if union is None:
        union = schema.get("anyOf")
    if union is not None:
        branches = [branch for branch in union if branch.get("type") != "null"]
        if not branches:
            raise AssertionError(
                f"{registered_path}: no traversable non-null branch in {context}"
            )
        for branch in branches:
            _assert_field_path(branch, document, segments, registered_path, context)
        return

    field, *remaining = segments
    properties = schema.get("properties", {})
    if field not in properties:
        raise AssertionError(
            f"{registered_path}: consumed field '{field}' is missing in {context}"
        )
    _assert_field_path(
        properties[field],
        document,
        tuple(remaining),
        registered_path,
        context,
    )


def _assert_component_ref(
    schema: dict[str, Any],
    *,
    expected_component: str,
    context: str,
) -> None:
    expected_ref = f"#/components/schemas/{expected_component}"
    assert schema.get("$ref") == expected_ref, (
        f"{context}: expected component {expected_component} ({expected_ref})"
    )


def _assert_nullable_component_ref(
    schema: dict[str, Any],
    *,
    expected_component: str,
    context: str,
) -> None:
    branches = schema.get("anyOf")
    assert isinstance(branches, list), f"{context}: expected nullable component union"
    refs = [
        branch.get("$ref")
        for branch in branches
        if isinstance(branch, dict) and branch.get("type") != "null"
    ]
    null_branches = [
        branch
        for branch in branches
        if isinstance(branch, dict) and branch.get("type") == "null"
    ]
    expected_ref = f"#/components/schemas/{expected_component}"
    assert refs == [expected_ref] and len(null_branches) == 1, (
        f"{context}: expected {expected_component} or null"
    )


def assert_budget_nested_response_bindings(snapshot: dict[str, Any]) -> None:
    """Bind every nested response component validated by the budget command."""
    schemas = snapshot["components"]["schemas"]
    status_properties = schemas["BudgetStatusResponse"]["properties"]

    thresholds = status_properties["thresholds"]
    items = thresholds.get("items")
    assert isinstance(items, dict), (
        "BudgetStatusResponse.thresholds: expected array items schema"
    )
    branches = items.get("oneOf")
    assert isinstance(branches, list), (
        "BudgetStatusResponse.thresholds: expected oneOf state components"
    )
    expected_refs = {
        f"#/components/schemas/{component}"
        for component in _ALL_THRESHOLD_COMPONENTS.values()
    }
    actual_refs = [
        branch.get("$ref") if isinstance(branch, dict) else None for branch in branches
    ]
    assert (
        len(actual_refs) == len(expected_refs) and set(actual_refs) == expected_refs
    ), "BudgetStatusResponse.thresholds: threshold state component refs differ"

    discriminator = items.get("discriminator")
    assert isinstance(discriminator, dict), (
        "BudgetStatusResponse.thresholds: discriminator is missing"
    )
    assert discriminator.get("propertyName") == "type", (
        "BudgetStatusResponse.thresholds: discriminator propertyName must be type"
    )
    expected_mapping = {
        rule_type: f"#/components/schemas/{component}"
        for rule_type, component in _ALL_THRESHOLD_COMPONENTS.items()
    }
    assert discriminator.get("mapping") == expected_mapping, (
        "BudgetStatusResponse.thresholds: discriminator mapping differs"
    )

    _assert_component_ref(
        status_properties["budget_period"],
        expected_component="BudgetPeriod",
        context="BudgetStatusResponse.budget_period",
    )
    _assert_component_ref(
        status_properties["budget_mode"],
        expected_component="BudgetMode",
        context="BudgetStatusResponse.budget_mode",
    )
    _assert_nullable_component_ref(
        status_properties["sub_period"],
        expected_component="BudgetPeriod",
        context="BudgetStatusResponse.sub_period",
    )
    _assert_component_ref(
        status_properties["constraints"],
        expected_component="BudgetConstraintsResponse",
        context="BudgetStatusResponse.constraints",
    )
    for component in _BUDGET_THRESHOLD_COMPONENTS.values():
        _assert_component_ref(
            schemas[component]["properties"]["routing"],
            expected_component="AlertRouting",
            context=f"{component}.routing",
        )

    alert_routing = schemas["AlertRouting"]["properties"]
    channels = alert_routing["channels"].get("items")
    assert isinstance(channels, dict), "AlertRouting.channels: items schema is missing"
    _assert_component_ref(
        channels,
        expected_component="RoutingChannel",
        context="AlertRouting.channels",
    )

    _assert_nullable_component_ref(
        alert_routing["muted"],
        expected_component="RoutingMuted",
        context="AlertRouting.muted",
    )


def assert_response_reads_exist(snapshot: dict[str, Any]) -> None:
    """Validate the explicit response-read registry against the snapshot."""
    schemas = snapshot["components"]["schemas"]
    for component, paths in RESPONSE_READS.items():
        assert component in schemas, f"Missing response component: {component}"
        for path in paths:
            registered_path = f"{component}.{path}"
            _assert_field_path(
                schemas[component],
                snapshot,
                tuple(path.split(".")),
                registered_path,
                component,
            )
    assert_budget_nested_response_bindings(snapshot)


def assert_operation_response_bindings(snapshot: dict[str, Any]) -> None:
    """Validate every consumed success operation against its response component."""
    paths = snapshot["paths"]
    for (method, path), (
        status,
        shape,
        component,
    ) in OPERATION_RESPONSE_BINDINGS.items():
        assert path in paths, f"{method} {path}: path is missing for {component}"
        method_key = method.lower()
        assert method_key in paths[path], (
            f"{method} {path}: operation is missing for {component}"
        )
        responses = paths[path][method_key].get("responses", {})
        assert status in responses, (
            f"{method} {path}: {status} response is missing for {component}"
        )
        content = responses[status].get("content", {})
        assert "application/json" in content, (
            f"{method} {path}: {status} application/json response is missing "
            f"for {component}"
        )
        response_schema = content["application/json"].get("schema")
        assert isinstance(response_schema, dict), (
            f"{method} {path}: expected {shape} {component} response schema"
        )

        if shape == "array":
            assert response_schema.get("type") == "array", (
                f"{method} {path}: expected array response of {component}"
            )
            component_schema = response_schema.get("items")
        else:
            assert response_schema.get("type") != "array", (
                f"{method} {path}: expected object response of {component}"
            )
            component_schema = response_schema

        assert isinstance(component_schema, dict), (
            f"{method} {path}: expected {shape} response using {component}"
        )
        expected_ref = f"#/components/schemas/{component}"
        assert component_schema.get("$ref") == expected_ref, (
            f"{method} {path}: expected {shape} response using {component} "
            f"({expected_ref})"
        )


def assert_no_content_operation_bindings(snapshot: dict[str, Any]) -> None:
    """Validate success operations whose contract is an empty response body."""
    paths = snapshot["paths"]
    for (method, path), status in NO_CONTENT_OPERATION_BINDINGS.items():
        responses = paths[path][method.lower()]["responses"]
        assert status in responses, f"{method} {path}: {status} response is missing"
        assert "content" not in responses[status], (
            f"{method} {path}: {status} response must remain bodyless"
        )
        alternate_successes = {
            candidate
            for candidate in responses
            if candidate != status
            and candidate.isdigit()
            and 200 <= int(candidate) <= 299
        }
        assert not alternate_successes, (
            f"{method} {path}: success must remain {status}-only"
        )


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_every_current_response_read_exists_in_snapshot() -> None:
    assert_response_reads_exist(_snapshot())


def test_threshold_registries_match_the_rule_types_the_cli_actually_models() -> None:
    """Split the union honestly: the registry cannot excuse a modelled rule type."""
    union, _ = get_args(budget._ThresholdState)
    modelled = {
        get_args(member.model_fields["type"].annotation)[0]
        for member in get_args(union)
    }

    assert modelled == set(_BUDGET_THRESHOLD_COMPONENTS)
    assert not modelled & set(_UNMODELLED_THRESHOLD_COMPONENTS)
    assert set(_ALL_THRESHOLD_COMPONENTS) == modelled | set(
        _UNMODELLED_THRESHOLD_COMPONENTS
    )


def test_project_create_response_fields_and_201_operation_are_registered() -> None:
    assert RESPONSE_READS["ProjectCreateResponse"] == (
        "id",
        "name",
        "budget_limit",
        "budget_period",
        "budget_mode",
        "created_at",
        "key",
        "key_prefix",
        "scope",
    )
    assert OPERATION_RESPONSE_BINDINGS[("POST", "/api/v1/projects")] == (
        "201",
        "object",
        "ProjectCreateResponse",
    )


def test_attestation_response_tree_and_both_routes_are_registered() -> None:
    assert RESPONSE_READS["SpendControlAttestation"] == (
        "schema_version",
        "generated_at",
        "scope",
        "range",
        "window",
        "spend_summary",
        "budgets",
        "alerts",
        "account_spend_alert",
        "subscription",
        "audit",
    )
    assert OPERATION_RESPONSE_BINDINGS[
        ("GET", "/api/v1/projects/{project_id}/spend-control-attestation")
    ] == ("200", "object", "SpendControlAttestation")
    assert OPERATION_RESPONSE_BINDINGS[
        ("GET", "/api/v1/account/spend-control-attestation")
    ] == ("200", "object", "SpendControlAttestation")


def test_init_reuses_guarded_project_login_and_budget_response_operations() -> None:
    """Init's composed reads remain covered by the shared response immune system."""
    expected = {
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/projects"),
        ("POST", "/api/v1/projects"),
        ("GET", "/api/v1/projects/{project_id}/budget"),
    }

    assert expected <= OPERATION_RESPONSE_BINDINGS.keys()
    assert init.fetch_projects is projects.fetch_projects
    assert init.fetch_budget_status is budget.fetch_budget_status


def test_every_consumed_success_operation_returns_its_response_component() -> None:
    assert_operation_response_bindings(_snapshot())


def test_every_consumed_no_content_operation_is_bodyless() -> None:
    assert_no_content_operation_bindings(_snapshot())


@pytest.mark.parametrize("mutation", ["status", "body", "alternate"])
def test_project_key_revoke_response_drift_trips_guard(mutation: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    responses = mutated["paths"]["/api/v1/projects/{project_id}/key/{key_id}"][
        "delete"
    ]["responses"]
    if mutation == "status":
        responses["200"] = responses.pop("204")
    elif mutation == "body":
        responses["204"]["content"] = {
            "application/json": {"schema": {"type": "object"}}
        }
    else:
        responses["200"] = {"description": "Alternate success"}

    with pytest.raises(AssertionError):
        assert_no_content_operation_bindings(mutated)


@pytest.mark.parametrize(
    ("method", "path", "status", "shape", "component"),
    [
        (method, path, status, shape, component)
        for (method, path), (status, shape, component) in (
            OPERATION_RESPONSE_BINDINGS.items()
        )
    ],
)
def test_operation_response_drift_trips_binding_guard(
    method: str,
    path: str,
    status: str,
    shape: str,
    component: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    response_schema = mutated["paths"][path][method.lower()]["responses"][status][
        "content"
    ]["application/json"]["schema"]
    component_schema = response_schema["items"] if shape == "array" else response_schema
    component_schema["$ref"] = "#/components/schemas/UnrelatedResponse"

    with pytest.raises(
        AssertionError,
        match=rf"{method} {re.escape(path)}.*{component}",
    ):
        assert_operation_response_bindings(mutated)


def test_missing_consumed_operation_path_trips_binding_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    path = "/api/v1/auth/login"
    mutated["paths"].pop(path)

    with pytest.raises(
        AssertionError,
        match=rf"POST {re.escape(path)}.*path is missing.*TokenResponse",
    ):
        assert_operation_response_bindings(mutated)


def test_missing_consumed_operation_method_trips_binding_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    path = "/api/v1/projects/{project_id}/providers"
    mutated["paths"][path].pop("get")

    with pytest.raises(
        AssertionError,
        match=rf"GET {re.escape(path)}.*operation is missing.*ProviderHealthResponse",
    ):
        assert_operation_response_bindings(mutated)


@pytest.mark.parametrize(
    ("method", "path", "status", "shape", "component"),
    [
        (method, path, status, shape, component)
        for (method, path), (status, shape, component) in (
            OPERATION_RESPONSE_BINDINGS.items()
        )
    ],
)
def test_operation_response_shape_drift_trips_binding_guard(
    method: str,
    path: str,
    status: str,
    shape: str,
    component: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    response = mutated["paths"][path][method.lower()]["responses"][status]["content"][
        "application/json"
    ]
    if shape == "array":
        response["schema"] = {"$ref": f"#/components/schemas/{component}"}
    else:
        response["schema"] = {
            "type": "array",
            "items": {"$ref": f"#/components/schemas/{component}"},
        }

    with pytest.raises(
        AssertionError,
        match=rf"{method} {re.escape(path)}.*{shape}.*{component}",
    ):
        assert_operation_response_bindings(mutated)


def test_removed_consumed_union_field_trips_response_subset_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    schemas = mutated["components"]["schemas"]
    schemas["QuotaExceededDetail"]["properties"].pop("message")

    with pytest.raises(
        AssertionError,
        match=r"Error402Response\.detail\.message.*QuotaExceededDetail",
    ):
        assert_response_reads_exist(mutated)


def test_union_without_traversable_branch_trips_response_subset_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    detail = mutated["components"]["schemas"]["Error402Response"]["properties"]
    detail["detail"] = {"oneOf": [{"type": "null"}]}

    with pytest.raises(
        AssertionError,
        match=r"Error402Response\.detail\.code.*no traversable non-null branch",
    ):
        assert_response_reads_exist(mutated)


def test_all_of_wrapper_is_traversed_for_registered_reads() -> None:
    mutated = copy.deepcopy(_snapshot())
    schemas = mutated["components"]["schemas"]
    original = schemas["Error402Response"]
    schemas["Error402Response"] = {"allOf": [original]}

    assert_response_reads_exist(mutated)


def test_all_of_without_consumed_path_trips_response_subset_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    schemas = mutated["components"]["schemas"]
    schemas["Error402Response"] = {"allOf": [{"type": "object", "properties": {}}]}

    with pytest.raises(
        AssertionError,
        match=r"Error402Response\.detail\.code.*no allOf branch",
    ):
        assert_response_reads_exist(mutated)


def test_replacing_every_budget_threshold_state_ref_trips_response_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    thresholds = mutated["components"]["schemas"]["BudgetStatusResponse"]["properties"][
        "thresholds"
    ]
    for branch in thresholds["items"]["oneOf"]:
        branch["$ref"] = "#/components/schemas/UnrelatedResponse"

    with pytest.raises(AssertionError, match="BudgetStatusResponse.thresholds"):
        assert_response_reads_exist(mutated)


@pytest.mark.parametrize(
    ("rule_type", "component"),
    list(_ALL_THRESHOLD_COMPONENTS.items()),
)
@pytest.mark.parametrize(
    "mutation", ["replace_ref", "remove_ref", "replace_mapping", "remove_mapping"]
)
def test_each_budget_threshold_component_binding_mutation_trips_guard(
    rule_type: str,
    component: str,
    mutation: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    items = mutated["components"]["schemas"]["BudgetStatusResponse"]["properties"][
        "thresholds"
    ]["items"]
    expected_ref = f"#/components/schemas/{component}"
    if mutation in {"replace_ref", "remove_ref"}:
        branch = next(
            candidate
            for candidate in items["oneOf"]
            if candidate.get("$ref") == expected_ref
        )
        if mutation == "replace_ref":
            branch["$ref"] = "#/components/schemas/UnrelatedResponse"
        else:
            branch.pop("$ref")
    elif mutation == "replace_mapping":
        items["discriminator"]["mapping"][rule_type] = (
            "#/components/schemas/UnrelatedResponse"
        )
    else:
        items["discriminator"]["mapping"].pop(rule_type)

    with pytest.raises(AssertionError, match="BudgetStatusResponse.thresholds"):
        assert_budget_nested_response_bindings(mutated)


@pytest.mark.parametrize("mutation", ["replace", "remove"])
def test_budget_threshold_discriminator_property_name_mutation_trips_guard(
    mutation: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    discriminator = mutated["components"]["schemas"]["BudgetStatusResponse"][
        "properties"
    ]["thresholds"]["items"]["discriminator"]
    if mutation == "replace":
        discriminator["propertyName"] = "kind"
    else:
        discriminator.pop("propertyName")

    with pytest.raises(
        AssertionError,
        match=r"BudgetStatusResponse\.thresholds.*propertyName",
    ):
        assert_budget_nested_response_bindings(mutated)


@pytest.mark.parametrize(
    ("parent", "field", "shape", "expected_component"),
    [
        ("BudgetStatusResponse", "budget_period", "direct", "BudgetPeriod"),
        ("BudgetStatusResponse", "budget_mode", "direct", "BudgetMode"),
        ("BudgetStatusResponse", "sub_period", "nullable", "BudgetPeriod"),
        (
            "BudgetStatusResponse",
            "constraints",
            "direct",
            "BudgetConstraintsResponse",
        ),
        *[
            (component, "routing", "direct", "AlertRouting")
            for component in _BUDGET_THRESHOLD_COMPONENTS.values()
        ],
        ("AlertRouting", "channels", "array", "RoutingChannel"),
        ("AlertRouting", "muted", "nullable", "RoutingMuted"),
    ],
)
@pytest.mark.parametrize("mutation", ["replace", "remove"])
def test_consumed_nested_component_ref_mutation_trips_guard(
    parent: str,
    field: str,
    shape: str,
    expected_component: str,
    mutation: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    field_schema = mutated["components"]["schemas"][parent]["properties"][field]
    if shape == "array":
        target = field_schema["items"]
    elif shape == "nullable":
        target = next(branch for branch in field_schema["anyOf"] if "$ref" in branch)
    else:
        target = field_schema
    assert target["$ref"] == f"#/components/schemas/{expected_component}"
    if mutation == "replace":
        target["$ref"] = "#/components/schemas/UnrelatedResponse"
    else:
        target.pop("$ref")

    with pytest.raises(AssertionError, match=rf"{parent}\.{field}"):
        assert_budget_nested_response_bindings(mutated)


@pytest.mark.parametrize(
    ("component", "field"),
    [
        ("TokenResponse", "access_token"),
        ("TokenResponse", "token_type"),
        ("MeResponse", "display_name"),
        ("ProjectResponse", "name"),
        ("ProjectResponse", "budget_limit"),
        ("ProjectResponse", "budget_period"),
        ("ProjectResponse", "budget_mode"),
        ("ProjectResponse", "created_at"),
        ("ProjectCreateResponse", "id"),
        ("ProjectCreateResponse", "name"),
        ("ProjectCreateResponse", "budget_limit"),
        ("ProjectCreateResponse", "budget_period"),
        ("ProjectCreateResponse", "budget_mode"),
        ("ProjectCreateResponse", "created_at"),
        ("ProjectCreateResponse", "key"),
        ("ProjectCreateResponse", "key_prefix"),
        ("ProjectCreateResponse", "scope"),
        ("ProjectKeyResponse", "id"),
        ("ProjectKeyResponse", "key_prefix"),
        ("ProjectKeyResponse", "name"),
        ("ProjectKeyResponse", "last_used_at"),
        ("ProjectKeyResponse", "expires_at"),
        ("ProjectKeyResponse", "rotation_grace_until"),
        ("ProjectKeyResponse", "created_at"),
        ("ProjectKeyResponse", "scope"),
        ("ProjectKeyRotateResponse", "id"),
        ("ProjectKeyRotateResponse", "key"),
        ("ProjectKeyRotateResponse", "key_prefix"),
        ("ProjectKeyRotateResponse", "rotation_grace_until"),
        ("ProjectKeyRotateResponse", "scope"),
        ("BudgetStatusResponse", "project_id"),
        ("BudgetStatusResponse", "budget_limit"),
        ("BudgetStatusResponse", "current_usage"),
        ("BudgetStatusResponse", "remaining_budget"),
        ("BudgetStatusResponse", "budget_period"),
        ("BudgetStatusResponse", "budget_mode"),
        ("BudgetStatusResponse", "period_date"),
        ("BudgetStatusResponse", "utilization_pct"),
        ("BudgetStatusResponse", "thresholds"),
        ("BudgetStatusResponse", "sub_period"),
        ("BudgetStatusResponse", "sub_limit"),
        ("BudgetStatusResponse", "constraints"),
        ("ThresholdStateResponse", "type"),
        ("ThresholdStateResponse", "rule_key"),
        ("ThresholdStateResponse", "percent"),
        ("ThresholdStateResponse", "channel_ids"),
        ("ThresholdStateResponse", "routing"),
        ("ThresholdStateResponse", "snoozed_until"),
        ("ThresholdStateResponse", "paused"),
        ("ForecastOverrunStateResponse", "type"),
        ("ForecastOverrunStateResponse", "rule_key"),
        ("ForecastOverrunStateResponse", "spend_threshold_usd"),
        ("ForecastOverrunStateResponse", "period"),
        ("ForecastOverrunStateResponse", "channel_ids"),
        ("ForecastOverrunStateResponse", "routing"),
        ("ForecastOverrunStateResponse", "snoozed_until"),
        ("ForecastOverrunStateResponse", "paused"),
        ("RunawayRunStateResponse", "type"),
        ("RunawayRunStateResponse", "rule_key"),
        ("RunawayRunStateResponse", "spend_threshold_usd"),
        ("RunawayRunStateResponse", "call_threshold"),
        ("RunawayRunStateResponse", "window_minutes"),
        ("RunawayRunStateResponse", "channel_ids"),
        ("RunawayRunStateResponse", "routing"),
        ("RunawayRunStateResponse", "snoozed_until"),
        ("RunawayRunStateResponse", "paused"),
        ("DailyAnomalyStateResponse", "type"),
        ("DailyAnomalyStateResponse", "rule_key"),
        ("DailyAnomalyStateResponse", "multiplier"),
        ("DailyAnomalyStateResponse", "lookback_days"),
        ("DailyAnomalyStateResponse", "channel_ids"),
        ("DailyAnomalyStateResponse", "routing"),
        ("DailyAnomalyStateResponse", "snoozed_until"),
        ("DailyAnomalyStateResponse", "paused"),
        ("ScopedBudgetStateResponse", "type"),
        ("ScopedBudgetStateResponse", "rule_key"),
        ("ScopedBudgetStateResponse", "scope"),
        ("ScopedBudgetStateResponse", "match"),
        ("ScopedBudgetStateResponse", "limit"),
        ("ScopedBudgetStateResponse", "mode"),
        ("ScopedBudgetStateResponse", "channel_ids"),
        ("ScopedBudgetStateResponse", "routing"),
        ("ScopedBudgetStateResponse", "snoozed_until"),
        ("ScopedBudgetStateResponse", "paused"),
        ("UnitCostAnomalyStateResponse", "type"),
        ("UnitCostAnomalyStateResponse", "rule_key"),
        ("UnitCostAnomalyStateResponse", "multiplier"),
        ("UnitCostAnomalyStateResponse", "lookback_days"),
        ("UnitCostAnomalyStateResponse", "channel_ids"),
        ("UnitCostAnomalyStateResponse", "routing"),
        ("UnitCostAnomalyStateResponse", "snoozed_until"),
        ("UnitCostAnomalyStateResponse", "paused"),
        ("AlertRouting", "will_send"),
        ("AlertRouting", "email"),
        ("AlertRouting", "channels"),
        ("AlertRouting", "source"),
        ("AlertRouting", "muted"),
        ("RoutingChannel", "id"),
        ("RoutingChannel", "name"),
        ("RoutingMuted", "reason"),
        ("RoutingMuted", "severity_floor"),
        ("BudgetConstraintsResponse", "allowed_modes"),
        ("BudgetConstraintsResponse", "allowed_periods"),
        ("BudgetConstraintsResponse", "sub_period_enabled"),
        ("BudgetConstraintsResponse", "max_thresholds"),
        ("BudgetCheckResponse", "allowed"),
        ("BudgetCheckResponse", "remaining_budget"),
        ("BudgetCheckResponse", "reservation_id"),
        ("BudgetCheckResponse", "mode"),
        ("BudgetCheckResponse", "budget_limit"),
        ("BudgetCheckResponse", "current_usage"),
        ("BudgetCheckResponse", "denied_by_period"),
        ("BudgetCheckResponse", "project_id"),
        ("BudgetCheckResponse", "price_hints"),
        ("ProviderHealthResponse", "provider"),
        ("ProviderHealthResponse", "total_calls"),
        ("ProviderHealthResponse", "success_count"),
        ("ProviderHealthResponse", "error_count"),
        ("ProviderHealthResponse", "failover_count"),
        ("ProviderHealthResponse", "success_rate"),
        ("ProviderHealthResponse", "avg_latency_ms"),
        ("ProviderHealthResponse", "circuit_state"),
        ("CostQueryResponse", "project_id"),
        ("CostQueryResponse", "range"),
        ("CostQueryResponse", "group_by"),
        ("CostQueryResponse", "groups"),
        ("CostGroup", "group_key"),
        ("CostGroup", "provider"),
        ("CostGroup", "total_cost"),
        ("CostGroup", "total_input_tokens"),
        ("CostGroup", "total_output_tokens"),
        ("CostGroup", "call_count"),
        ("CostGroup", "unpriced_call_count"),
        ("CostGroup", "local_call_count"),
    ],
)
def test_command_response_field_removal_trips_subset_guard(
    component: str, field: str
) -> None:
    mutated = copy.deepcopy(_snapshot())
    mutated["components"]["schemas"][component]["properties"].pop(field)

    with pytest.raises(AssertionError, match=rf"{component}\.{field}"):
        assert_response_reads_exist(mutated)
