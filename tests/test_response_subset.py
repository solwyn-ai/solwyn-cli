"""Guard every response field currently consumed by the CLI."""

import copy
import json
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
    "ProjectResponse": ("id", "name"),
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


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_every_current_response_read_exists_in_snapshot() -> None:
    assert_response_reads_exist(_snapshot())


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
    ],
)
def test_command_response_field_removal_trips_subset_guard(
    component: str, field: str
) -> None:
    mutated = copy.deepcopy(_snapshot())
    mutated["components"]["schemas"][component]["properties"].pop(field)

    with pytest.raises(AssertionError, match=rf"{component}\.{field}"):
        assert_response_reads_exist(mutated)
