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
from solwyn_cli.commands.costs import CostGrouping, CostRange


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


def _effective_object_contract(
    schema: dict[str, Any],
    document: dict[str, Any],
    active_refs: frozenset[str],
) -> tuple[dict[str, Any], set[str], bool]:
    """Flatten object properties and requiredness composed with allOf."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in active_refs:
            return {}, set(), False
        return _effective_object_contract(
            _resolve_ref(document, reference),
            document,
            active_refs | {reference},
        )

    raw_properties = schema.get("properties")
    properties = dict(raw_properties) if isinstance(raw_properties, dict) else {}
    required = set(schema.get("required", []))
    is_object = isinstance(raw_properties, dict) or schema.get("type") == "object"

    for branch in schema.get("allOf", []):
        branch_properties, branch_required, branch_is_object = (
            _effective_object_contract(branch, document, active_refs)
        )
        is_object = is_object or branch_is_object
        required.update(branch_required)
        for field, field_schema in branch_properties.items():
            if field in properties and properties[field] != field_schema:
                properties[field] = {"allOf": [properties[field], field_schema]}
            else:
                properties[field] = field_schema
    return properties, required, is_object


def _object_shapes(
    schema: dict[str, Any],
    document: dict[str, Any],
    path: str,
    active_refs: frozenset[str] = frozenset(),
) -> dict[str, tuple[set[str], set[str]]]:
    """Collect every nested object field and required set by stable path."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in active_refs:
            return {}
        return _object_shapes(
            _resolve_ref(document, reference),
            document,
            path,
            active_refs | {reference},
        )

    shapes: dict[str, tuple[set[str], set[str]]] = {}

    def merge(nested: dict[str, tuple[set[str], set[str]]]) -> None:
        for nested_path, shape in nested.items():
            if nested_path in shapes:
                assert shapes[nested_path] == shape, (
                    f"{nested_path}: ambiguous object shapes in one schema"
                )
            shapes[nested_path] = shape

    properties, required, is_object = _effective_object_contract(
        schema, document, active_refs
    )
    if is_object:
        shapes[path] = (set(properties), required)
        for field, field_schema in properties.items():
            merge(
                _object_shapes(
                    field_schema,
                    document,
                    f"{path}.{field}" if path else field,
                    active_refs,
                )
            )

    items = schema.get("items")
    if isinstance(items, dict):
        merge(_object_shapes(items, document, f"{path}[]", active_refs))

    for branch_key in ("oneOf", "anyOf"):
        for index, branch in enumerate(schema.get(branch_key, [])):
            branch_path = f"{path}<{_branch_identity(branch, index)}>"
            merge(_object_shapes(branch, document, branch_path, active_refs))
    return shapes


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
        local_shapes = _object_shapes(local, local, component)
        remote_shapes = _object_shapes(remote, snapshot, component)
        for path in sorted(set(local_shapes) | set(remote_shapes)):
            assert path in local_shapes and path in remote_shapes, (
                f"{path}: object path missing; "
                f"CLI={path in local_shapes}, snapshot={path in remote_shapes}"
            )
            local_fields, local_required = local_shapes[path]
            remote_fields, remote_required = remote_shapes[path]
            assert local_fields == remote_fields, (
                f"{path}: field set differs; "
                f"CLI-only={sorted(local_fields - remote_fields)}, "
                f"snapshot-only={sorted(remote_fields - local_fields)}"
            )
            assert local_required == remote_required, (
                f"{path}: required set differs; "
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


def _operation_parameter(
    snapshot: dict[str, Any],
    *,
    path: str,
    method: str,
    location: str,
    name: str,
) -> dict[str, Any]:
    parameters = snapshot["paths"][path][method]["parameters"]
    matches = [
        parameter
        for parameter in parameters
        if parameter.get("in") == location and parameter.get("name") == name
    ]
    assert len(matches) == 1, (
        f"{method.upper()} {path}: expected one {location} parameter {name!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


def _assert_string_parameter_domain(
    parameter: dict[str, Any],
    *,
    values: set[str],
    context: str,
) -> None:
    schema = parameter["schema"]
    assert schema.get("type") == "string", f"{context}: parameter is not a string"
    if "enum" in schema:
        assert set(schema["enum"]) == values, (
            f"{context}: allowed values differ; "
            f"CLI={sorted(values)}, snapshot={sorted(schema['enum'])}"
        )
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            compiled_pattern = re.compile(pattern)
        except re.error:
            raise AssertionError(f"{context}: invalid pattern") from None
        for value in values:
            assert compiled_pattern.search(value) is not None, (
                f"{context}: pattern rejects frozen value {value!r}"
            )
        if match := _FINITE_ALTERNATION.fullmatch(pattern):
            assert set(match.group(1).split("|")) == values, (
                f"{context}: allowed values differ; "
                f"CLI={sorted(values)}, snapshot={sorted(match.group(1).split('|'))}"
            )


def assert_costs_query_contract(snapshot: dict[str, Any]) -> None:
    """Guard the exact query mapping and frozen CLI domains for costs."""
    path = "/api/v1/projects/{project_id}/costs"
    contracts = {
        "range": ({member.value for member in CostRange}, "7d"),
        "group_by": ({member.value for member in CostGrouping}, "model"),
    }
    for name, (values, default) in contracts.items():
        parameter = _operation_parameter(
            snapshot,
            path=path,
            method="get",
            location="query",
            name=name,
        )
        context = f"GET {path} query {name}"
        assert parameter.get("required") is False, f"{context}: requiredness differs"
        assert parameter["schema"].get("default") == default, (
            f"{context}: default differs"
        )
        _assert_string_parameter_domain(parameter, values=values, context=context)


def test_snapshot_matches_every_public_request_mirror() -> None:
    assert_request_mirrors_match(_snapshot())


def test_costs_query_contract_matches_snapshot() -> None:
    assert_costs_query_contract(_snapshot())


def test_costs_cli_domains_are_frozen() -> None:
    assert {member.value for member in CostRange} == {"7d", "30d", "90d", "365d"}
    assert {member.value for member in CostGrouping} == {"day", "model", "provider"}


@pytest.mark.parametrize(
    ("name", "replacement"),
    [("range", "date_range"), ("group_by", "grouping")],
)
def test_costs_query_name_drift_trips_guard(name: str, replacement: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name=name,
    )
    parameter["name"] = replacement

    with pytest.raises(AssertionError, match=rf"query parameter {name!r}"):
        assert_costs_query_contract(mutated)


def test_costs_query_requiredness_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name="range",
    )
    parameter["required"] = True

    with pytest.raises(AssertionError, match="range: requiredness differs"):
        assert_costs_query_contract(mutated)


@pytest.mark.parametrize(
    ("name", "replacement"),
    [("range", "30d"), ("group_by", "day")],
)
def test_costs_query_default_drift_trips_guard(
    name: str,
    replacement: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name=name,
    )
    parameter["schema"]["default"] = replacement

    with pytest.raises(AssertionError, match=rf"{name}: default differs"):
        assert_costs_query_contract(mutated)


def test_costs_query_allowed_value_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name="range",
    )
    parameter["schema"]["enum"] = ["7d", "30d", "90d"]

    with pytest.raises(AssertionError, match="range: allowed values differ"):
        assert_costs_query_contract(mutated)


def test_costs_query_noncanonical_restrictive_pattern_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name="range",
    )
    parameter["schema"]["pattern"] = "^(?:7d|30d)$"

    with pytest.raises(AssertionError, match="range: pattern rejects frozen value"):
        assert_costs_query_contract(mutated)


def test_costs_query_unparseable_pattern_fails_closed() -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name="range",
    )
    parameter["schema"]["pattern"] = "["

    with pytest.raises(AssertionError, match="range: invalid pattern"):
        assert_costs_query_contract(mutated)


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


@pytest.mark.parametrize("mutation", ["removed", "added"])
def test_nested_token_detail_field_drift_trips_guard(mutation: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    properties = _schema(mutated, "TokenDetails")["properties"]
    if mutation == "removed":
        properties.pop("tool_use_input_tokens")
    else:
        properties["future_tokens"] = {"type": "integer"}

    with pytest.raises(
        AssertionError,
        match=r"BudgetConfirmRequest\.token_details: field set",
    ):
        assert_request_mirrors_match(mutated)


@pytest.mark.parametrize("mutation", ["removed", "added"])
def test_changed_nested_threshold_requiredness_trips_drift_guard(
    mutation: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    required = _schema(mutated, "DailyAnomalyConfig")["required"]
    if mutation == "removed":
        required.remove("multiplier")
    else:
        required.append("lookback_days")

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.thresholds\[\]<DailyAnomalyConfig>: "
        r"required set",
    ):
        assert_request_mirrors_match(mutated)


def test_equivalent_all_of_object_shape_is_traversed() -> None:
    mutated = copy.deepcopy(_snapshot())
    schemas = mutated["components"]["schemas"]
    token_details = schemas["TokenDetails"]
    schemas["TokenDetails"] = {"allOf": [token_details]}

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
