"""OpenAPI drift guard for the six public request mirrors."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from solwyn_cli._vendor.wire import (
    BudgetCheckRequest,
    BudgetConfirmRequest,
    BudgetUpdateRequest,
    LoginRequest,
    ProjectCreateRequest,
    RefreshRequest,
)


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
REQUEST_MIRRORS = {
    LoginRequest: "LoginRequest",
    RefreshRequest: "RefreshRequest",
    ProjectCreateRequest: "ProjectCreateRequest",
    BudgetUpdateRequest: "BudgetUpdateRequest",
    BudgetCheckRequest: "BudgetCheckRequest",
    BudgetConfirmRequest: "BudgetConfirmRequest",
}
_FINITE_ALTERNATION = re.compile(r"\^\(([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)*)\)\$")


def _branch_identity(schema: dict[str, Any], index: int) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return reference.rsplit("/", maxsplit=1)[-1].lstrip("_")
    return f"branch-{index}"


def _resolve_ref(document: dict[str, Any], reference: str) -> dict[str, Any]:
    node: Any = document
    for part in reference.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(node, dict)
    return node


def _enum_domains(
    schema: dict[str, Any],
    document: dict[str, Any],
    path: str,
    active_refs: frozenset[str] = frozenset(),
) -> dict[str, set[object]]:
    """Collect enum/const domains by logical field path through refs and unions."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in active_refs:
            return {}
        return _enum_domains(
            _resolve_ref(document, reference),
            document,
            path,
            active_refs | {reference},
        )

    domains: dict[str, set[object]] = {}
    if "enum" in schema:
        domains.setdefault(path, set()).update(schema["enum"])
    if "const" in schema:
        domains.setdefault(path, set()).add(schema["const"])
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and (match := _FINITE_ALTERNATION.fullmatch(pattern)):
        domains.setdefault(path, set()).update(match.group(1).split("|"))

    for union_key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(union_key, [])
        preserve_identity = union_key != "allOf" and any(
            "$ref" in branch for branch in branches
        )
        for index, branch in enumerate(branches):
            branch_path = (
                f"{path}<{_branch_identity(branch, index)}>"
                if preserve_identity
                else path
            )
            for branch_path, values in _enum_domains(
                branch, document, branch_path, active_refs
            ).items():
                domains.setdefault(branch_path, set()).update(values)

    properties = schema.get("properties", {})
    for field, field_schema in properties.items():
        field_path = f"{path}.{field}" if path else field
        for nested_path, values in _enum_domains(
            field_schema, document, field_path, active_refs
        ).items():
            domains.setdefault(nested_path, set()).update(values)

    items = schema.get("items")
    if isinstance(items, dict):
        for nested_path, values in _enum_domains(
            items, document, f"{path}[]", active_refs
        ).items():
            domains.setdefault(nested_path, set()).update(values)
    return domains


def assert_request_mirrors_match(snapshot: dict[str, Any]) -> None:
    """Assert field, requiredness, and recursive enum parity for all mirrors."""
    for mirror, component in REQUEST_MIRRORS.items():
        local = mirror.model_json_schema()
        remote = _schema(snapshot, component)
        local_fields = set(local.get("properties", {}))
        remote_fields = set(remote.get("properties", {}))
        assert local_fields == remote_fields, (
            f"{component}: field set differs; "
            f"CLI-only={sorted(local_fields - remote_fields)}, "
            f"snapshot-only={sorted(remote_fields - local_fields)}"
        )

        local_required = set(local.get("required", []))
        remote_required = set(remote.get("required", []))
        assert local_required == remote_required, (
            f"{component}: required set differs; "
            f"CLI-only={sorted(local_required - remote_required)}, "
            f"snapshot-only={sorted(remote_required - local_required)}"
        )

        local_domains = _enum_domains(local, local, component)
        remote_domains = _enum_domains(remote, snapshot, component)
        for path in sorted(set(local_domains) | set(remote_domains)):
            assert local_domains.get(path, set()) == remote_domains.get(path, set()), (
                f"{path}: enum/const/pattern values differ; "
                f"CLI={sorted(local_domains.get(path, set()), key=repr)}, "
                f"snapshot={sorted(remote_domains.get(path, set()), key=repr)}"
            )


def _snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _schema(snapshot: dict[str, Any], component: str) -> dict[str, Any]:
    return snapshot["components"]["schemas"][component]


def test_snapshot_matches_every_public_request_mirror() -> None:
    assert_request_mirrors_match(_snapshot())


def test_removed_request_field_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "LoginRequest")["properties"].pop("email")

    with pytest.raises(AssertionError, match="LoginRequest: field set"):
        assert_request_mirrors_match(mutated)


def test_changed_request_requiredness_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "LoginRequest")["required"].remove("email")

    with pytest.raises(AssertionError, match="LoginRequest: required set"):
        assert_request_mirrors_match(mutated)


def test_changed_nested_request_enum_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "ThresholdConfig")["properties"]["type"]["const"] = (
        "changed_threshold"
    )

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.thresholds\[\]<ThresholdConfig>\.type: "
        r"enum/const/pattern values",
    ):
        assert_request_mirrors_match(mutated)


def test_changed_finite_pattern_domain_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    mode = _schema(mutated, "ProjectCreateRequest")["properties"]["budget_mode"]
    mode["pattern"] = "^(alert_only)$"

    with pytest.raises(
        AssertionError,
        match=r"ProjectCreateRequest\.budget_mode: enum/const/pattern values",
    ):
        assert_request_mirrors_match(mutated)


def test_swapped_union_discriminators_trip_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    threshold_type = _schema(mutated, "ThresholdConfig")["properties"]["type"]
    daily_type = _schema(mutated, "DailyAnomalyConfig")["properties"]["type"]
    threshold_type["const"], daily_type["const"] = (
        daily_type["const"],
        threshold_type["const"],
    )

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.thresholds\[\]<"
        r"(?:DailyAnomalyConfig|ThresholdConfig)>\.type",
    ):
        assert_request_mirrors_match(mutated)
