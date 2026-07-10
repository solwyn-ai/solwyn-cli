"""Guard every response field currently consumed by the CLI."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
# P3+ command tasks extend this registry when they begin reading response fields.
RESPONSE_READS = {
    "TokenResponse": ("access_token", "refresh_token", "token_type"),
    "Error402Response": (
        "detail.code",
        "detail.message",
        "detail.cta_path",
    ),
    "UpgradeRequiredDetail": ("code", "message", "cta_path"),
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
    "CostQueryResponse": ("project_id", "range", "group_by", "groups"),
    "CostGroup": (
        "group_key",
        "provider",
        "total_cost",
        "total_input_tokens",
        "total_output_tokens",
        "call_count",
        "unpriced_call_count",
        "local_call_count",
    ),
}
# method/path -> (JSON shape, consumed response component)
OPERATION_RESPONSE_BINDINGS = {
    ("POST", "/api/v1/auth/login"): ("object", "TokenResponse"),
    ("POST", "/api/v1/auth/refresh"): ("object", "TokenResponse"),
    ("GET", "/api/v1/account/me"): ("object", "MeResponse"),
    ("GET", "/api/v1/projects"): ("array", "ProjectResponse"),
    ("GET", "/api/v1/projects/{project_id}"): ("object", "ProjectResponse"),
    ("GET", "/api/v1/projects/{project_id}/budget"): (
        "object",
        "BudgetStatusResponse",
    ),
    ("PUT", "/api/v1/projects/{project_id}/budget"): (
        "object",
        "BudgetStatusResponse",
    ),
    ("POST", "/api/v1/budgets/check"): ("object", "BudgetCheckResponse"),
    ("GET", "/api/v1/projects/{project_id}/providers"): (
        "array",
        "ProviderHealthResponse",
    ),
    ("GET", "/api/v1/projects/{project_id}/costs"): (
        "object",
        "CostQueryResponse",
    ),
}
_BUDGET_THRESHOLD_COMPONENTS = {
    "budget_threshold": "ThresholdStateResponse",
    "forecast_overrun": "ForecastOverrunStateResponse",
    "daily_anomaly": "DailyAnomalyStateResponse",
    "unit_cost_anomaly": "UnitCostAnomalyStateResponse",
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
        for component in _BUDGET_THRESHOLD_COMPONENTS.values()
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
        for rule_type, component in _BUDGET_THRESHOLD_COMPONENTS.items()
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
    for (method, path), (shape, component) in OPERATION_RESPONSE_BINDINGS.items():
        assert path in paths, f"{method} {path}: path is missing for {component}"
        method_key = method.lower()
        assert method_key in paths[path], (
            f"{method} {path}: operation is missing for {component}"
        )
        responses = paths[path][method_key].get("responses", {})
        assert "200" in responses, (
            f"{method} {path}: 200 response is missing for {component}"
        )
        content = responses["200"].get("content", {})
        assert "application/json" in content, (
            f"{method} {path}: 200 application/json response is missing for {component}"
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


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_every_current_response_read_exists_in_snapshot() -> None:
    assert_response_reads_exist(_snapshot())


def test_every_consumed_success_operation_returns_its_response_component() -> None:
    assert_operation_response_bindings(_snapshot())


@pytest.mark.parametrize(
    ("method", "path", "shape", "component"),
    [
        (method, path, shape, component)
        for (method, path), (shape, component) in OPERATION_RESPONSE_BINDINGS.items()
    ],
)
def test_operation_response_drift_trips_binding_guard(
    method: str,
    path: str,
    shape: str,
    component: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    response_schema = mutated["paths"][path][method.lower()]["responses"]["200"][
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
    ("method", "path", "shape", "component"),
    [
        (method, path, shape, component)
        for (method, path), (shape, component) in OPERATION_RESPONSE_BINDINGS.items()
    ],
)
def test_operation_response_shape_drift_trips_binding_guard(
    method: str,
    path: str,
    shape: str,
    component: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    response = mutated["paths"][path][method.lower()]["responses"]["200"]["content"][
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
    list(_BUDGET_THRESHOLD_COMPONENTS.items()),
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
        ("DailyAnomalyStateResponse", "type"),
        ("DailyAnomalyStateResponse", "rule_key"),
        ("DailyAnomalyStateResponse", "multiplier"),
        ("DailyAnomalyStateResponse", "lookback_days"),
        ("DailyAnomalyStateResponse", "channel_ids"),
        ("DailyAnomalyStateResponse", "routing"),
        ("DailyAnomalyStateResponse", "snoozed_until"),
        ("DailyAnomalyStateResponse", "paused"),
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
