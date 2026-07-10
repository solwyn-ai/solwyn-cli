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
PROJECT_RESPONSE_OPERATIONS = {
    "/api/v1/projects": "array",
    "/api/v1/projects/{project_id}": "object",
}
BUDGET_STATUS_OPERATION = "/api/v1/projects/{project_id}/budget"


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


def assert_project_response_operations(snapshot: dict[str, Any]) -> None:
    """Require both consumed project GETs to keep returning ProjectResponse."""
    for path, shape in PROJECT_RESPONSE_OPERATIONS.items():
        response_schema = snapshot["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        project_schema = (
            response_schema.get("items") if shape == "array" else response_schema
        )
        assert isinstance(project_schema, dict), (
            f"GET {path}: expected {shape} ProjectResponse payload"
        )
        assert project_schema.get("$ref") == ("#/components/schemas/ProjectResponse"), (
            f"GET {path}: response no longer uses ProjectResponse"
        )


def assert_budget_status_operation(snapshot: dict[str, Any]) -> None:
    """Require the pure budget GET to keep returning BudgetStatusResponse."""
    operation = snapshot["paths"][BUDGET_STATUS_OPERATION]
    assert "get" in operation, f"GET {BUDGET_STATUS_OPERATION}: operation is missing"
    response_schema = operation["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema.get("$ref") == (
        "#/components/schemas/BudgetStatusResponse"
    ), f"GET {BUDGET_STATUS_OPERATION}: response no longer uses BudgetStatusResponse"


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_every_current_response_read_exists_in_snapshot() -> None:
    assert_response_reads_exist(_snapshot())


def test_both_project_operations_return_the_consumed_response_component() -> None:
    assert_project_response_operations(_snapshot())


def test_budget_status_get_returns_the_consumed_response_component() -> None:
    assert_budget_status_operation(_snapshot())


@pytest.mark.parametrize("path", PROJECT_RESPONSE_OPERATIONS)
def test_project_operation_response_drift_trips_guard(path: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    schema = mutated["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    target = schema["items"] if path == "/api/v1/projects" else schema
    target["$ref"] = "#/components/schemas/MeResponse"

    with pytest.raises(
        AssertionError,
        match=rf"GET {re.escape(path)}.*ProjectResponse",
    ):
        assert_project_response_operations(mutated)


def test_budget_status_operation_response_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    mutated["paths"][BUDGET_STATUS_OPERATION]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"] = "#/components/schemas/ProviderHealthResponse"

    with pytest.raises(
        AssertionError,
        match=r"GET /api/v1/projects/\{project_id\}/budget.*BudgetStatusResponse",
    ):
        assert_budget_status_operation(mutated)


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
