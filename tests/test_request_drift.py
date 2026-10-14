"""OpenAPI drift guard for the public request mirrors and query contracts."""

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from solwyn_cli._vendor.notification_wire import (
    NotificationConfigUpdate,
    NotificationChannelCreate,
    NotificationChannelUpdate,
    AlertEmailUpdate,
    SlackTestRequest,
    PagerDutyTestRequest,
)
from solwyn_cli._vendor.wire import (
    AccountAlertUpdateRequest,
    AccountAlertPreviewRequest,
    AccountAlertSnoozeRequest,
    BudgetCheckRequest,
    BudgetCheckBatchRequest,
    BudgetConfirmRequest,
    BudgetUpdateRequest,
    CostSimulationRequest,
    LoginRequest,
    IngestMetadataEvent,
    ProjectCreateRequest,
    ProjectKeyRotateRequest,
    ProjectUpdateRequest,
    RefreshRequest,
    SnoozeRequest,
    ProjectAlertsPreviewRequest,
    WebhookCreateRequest,
)
from solwyn_cli.commands import init as init_command
from solwyn_cli.commands import login, projects
from solwyn_cli.commands.costs import CostRange
from solwyn_cli.commands.providers import FailoverRange, UntrackedRange
from solwyn_cli.commands.audit import AuditRange


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
REQUEST_MIRRORS = {
    NotificationConfigUpdate: "NotificationConfigUpdate",
    NotificationChannelCreate: "NotificationChannelCreate",
    NotificationChannelUpdate: "NotificationChannelUpdate",
    AlertEmailUpdate: "AlertEmailUpdate",
    SlackTestRequest: "SlackTestRequest",
    PagerDutyTestRequest: "PagerDutyTestRequest",
    AccountAlertUpdateRequest: "AccountAlertUpdateRequest",
    AccountAlertPreviewRequest: "AccountAlertPreviewRequest",
    AccountAlertSnoozeRequest: "AccountAlertSnoozeRequest",
    LoginRequest: "LoginRequest",
    RefreshRequest: "RefreshRequest",
    ProjectCreateRequest: "ProjectCreateRequest",
    ProjectKeyRotateRequest: "ProjectKeyRotateRequest",
    WebhookCreateRequest: "WebhookCreateRequest",
    ProjectUpdateRequest: "ProjectUpdateRequest",
    BudgetUpdateRequest: "BudgetUpdateRequest",
    BudgetCheckRequest: "BudgetCheckRequest",
    BudgetConfirmRequest: "BudgetConfirmRequest",
    SnoozeRequest: "SnoozeRequest",
    ProjectAlertsPreviewRequest: "ProjectAlertsPreviewRequest",
    IngestMetadataEvent: "IngestMetadataEvent",
    CostSimulationRequest: "CostSimulationRequest",
    BudgetCheckBatchRequest: "BudgetCheckBatchRequest",
}
DECLARED_DEFAULT_FIELDS = {
    "ProjectCreateRequest": (
        "budget_limit",
        "budget_period",
        "budget_mode",
        # Declared but omitted from the body unless chosen; pinning the two
        # defaults together is what makes the omission behavior-preserving.
        "scope",
    ),
    "ProjectKeyRotateRequest": ("grace_seconds",),
    "WebhookCreateRequest": ("events",),
    "BudgetCheckRequest": ("uncounted_calls", "uncounted_tokens"),
}
# Optional request fields the API accepts that the CLI deliberately does not
# send. Every entry is proven to exist and to be optional in the snapshot before
# it is pruned, so a listed field that becomes required, or disappears, still
# trips the guard — and any field NOT listed here trips it the moment Core adds
# it. Mirrors stay an exact match against everything the CLI actually posts.
UNADOPTED_OPTIONAL_FIELDS = {
    "IngestMetadataEvent": (
        # SDK-internal velocity observation and aggregate receipt accounting.
        "velocity_flags",
        "receipt_aggregate_count",
        "receipt_pricing_input_tokens",
        # SDK router/abort state is not observed by the CLI.
        "possibly_succeeded",
        "failover_reason",
        "failover_error_class",
        "requested_provider",
        "requested_model",
        # Reservation-only wave: the CLI has no lease plane, including batch input.
        "lease_id",
    ),
    "BudgetCheckRequest": (
        # Declined: the directive it requests only tunes SDK-side retry and
        # circuit-breaker behavior, which the CLI does not perform; opting in
        # would also switch the check response to null-omitting encoding.
        "failover_directive_version",
        # Declined: inert on this endpoint — the budget aggregator never reads
        # it; Core derives modality from (provider, model) itself.
        "modality",
    ),
    "BudgetConfirmRequest": (
        # No CLI lease plane: confirmation always requires a reservation.
        "lease_id",
        # Declined: same as check — inert on this endpoint.
        "modality",
    ),
}
# Threshold rule configs the CLI cannot construct.
#
# Empty: the mirror now covers every config branch the spec declares. Keep the
# registry — it is how a future unadopted config is declared, and a config that
# is NOT registered here still trips the guard rather than slipping in.
#
# Unlike the optional fields above, any entry added here is an OPEN DEFECT, NOT A
# BENIGN OMISSION: budget set round-trips the rules budget status read, and the
# matching response rule types are rejected outright by the command's closed
# threshold union — see the write-up on _UNMODELLED_THRESHOLD_COMPONENTS in
# test_response_subset.py. Adopt the rule type; do not relax the union instead.
UNADOPTED_THRESHOLD_CONFIGS: tuple[str, ...] = ()
UNADOPTED_FIELD_OWNERS = {
    "IngestMetadataEvent": IngestMetadataEvent,
    "BudgetCheckRequest": BudgetCheckRequest,
    "BudgetConfirmRequest": BudgetConfirmRequest,
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
            "$ref" in branch
            and _effective_object_contract(branch, document, frozenset())[2]
            for branch in branches
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


def _prune_field(schema: dict[str, Any], field: str) -> tuple[bool, set[str]]:
    """Drop one property from an object schema, following allOf composition."""
    properties = schema.get("properties")
    removed = isinstance(properties, dict) and properties.pop(field, None) is not None
    required = set(schema.get("required", []))
    for branch in schema.get("allOf", []):
        branch_removed, branch_required = _prune_field(branch, field)
        removed = removed or branch_removed
        required |= branch_required
    return removed, required


def _adopted_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reduce the snapshot to the request surface the CLI actually mirrors."""
    pruned = copy.deepcopy(snapshot)
    schemas = pruned["components"]["schemas"]

    for component, fields in UNADOPTED_OPTIONAL_FIELDS.items():
        for field in fields:
            removed, required = _prune_field(schemas[component], field)
            assert removed, (
                f"{component}.{field}: registered as unadopted but the snapshot "
                f"no longer declares it; drop the stale registry entry"
            )
            assert field not in required, (
                f"{component}.{field}: registered as unadopted but the snapshot "
                f"now requires it; the CLI must send it"
            )

    # Reservation-only specialization of Core's XOR funding contract. Assert
    # the upstream shape before narrowing it; never waive general requiredness.
    confirm = schemas["BudgetConfirmRequest"]
    reservation = confirm["properties"].get("reservation_id", {})
    assert "reservation_id" not in confirm.get("required", []), (
        "BudgetConfirmRequest.reservation_id: Core optionality changed"
    )
    assert reservation.get("anyOf") == [{"type": "string"}, {"type": "null"}], (
        "BudgetConfirmRequest.reservation_id: funding-source shape changed"
    )
    confirm["required"].append("reservation_id")
    confirm["properties"]["reservation_id"] = {"type": "string"}

    thresholds = schemas["BudgetUpdateRequest"]["properties"]["thresholds"]
    branches = thresholds["items"]["oneOf"]
    retained = [
        branch
        for index, branch in enumerate(branches)
        if _branch_identity(branch, index) not in UNADOPTED_THRESHOLD_CONFIGS
    ]
    assert len(retained) == len(branches) - len(UNADOPTED_THRESHOLD_CONFIGS), (
        "BudgetUpdateRequest.thresholds[]: registered unadopted configs "
        f"{sorted(UNADOPTED_THRESHOLD_CONFIGS)} are no longer all present"
    )
    thresholds["items"]["oneOf"] = retained
    return pruned


def assert_request_mirrors_match(snapshot: dict[str, Any]) -> None:
    """Assert field, requiredness, and recursive enum parity for all mirrors."""
    snapshot = _adopted_snapshot(snapshot)
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

        for field in DECLARED_DEFAULT_FIELDS.get(component, ()):
            local_default = local["properties"][field].get("default")
            remote_default = remote["properties"][field].get("default")
            assert local_default == remote_default, (
                f"{component}.{field}: default differs; "
                f"CLI={local_default!r}, snapshot={remote_default!r}"
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
        "group_by": ({"model"}, "model"),
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


def assert_forecast_and_provider_query_contracts(snapshot: dict[str, Any]) -> None:
    """Guard the exact query names, enum domains, and defaults for W3 reads."""
    contracts = (
        (
            "/api/v1/projects/{project_id}/forecast",
            "period",
            {"mtd"},
            "mtd",
        ),
        (
            "/api/v1/projects/{project_id}/providers/failovers",
            "range",
            {member.value for member in FailoverRange},
            "7d",
        ),
        (
            "/api/v1/projects/{project_id}/untracked-surfaces",
            "range",
            {member.value for member in UntrackedRange},
            "7d",
        ),
    )
    for path, name, values, default in contracts:
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


def assert_audit_query_contract(snapshot: dict[str, Any]) -> None:
    """Guard the audit filter domains and fixed server pagination contract."""
    path = "/api/v1/account/audit-events"
    parameters = snapshot["paths"][path]["get"]["parameters"]
    assert {parameter["name"] for parameter in parameters} == {
        "range",
        "cursor",
        "action",
    }
    range_parameter = _operation_parameter(
        snapshot,
        path=path,
        method="get",
        location="query",
        name="range",
    )
    assert range_parameter.get("required") is False
    assert range_parameter["schema"].get("default") == "30d"
    _assert_string_parameter_domain(
        range_parameter,
        values={member.value for member in AuditRange},
        context=f"GET {path} query range",
    )
    action = _operation_parameter(
        snapshot,
        path=path,
        method="get",
        location="query",
        name="action",
    )["schema"]["anyOf"][0]
    assert action.get("minLength") == 1
    assert action.get("maxLength") == 100


def assert_budget_check_operation_request_binding(snapshot: dict[str, Any]) -> None:
    """Bind the guardrail POST to its exact required request component."""
    path = "/api/v1/budgets/check"
    operation = snapshot["paths"][path]["post"]
    request_body = operation["requestBody"]
    assert request_body.get("required") is True, (
        f"POST {path}: request body must remain required"
    )
    content = request_body.get("content", {})
    assert "application/json" in content, (
        f"POST {path}: application/json request body is missing"
    )
    schema = content["application/json"].get("schema", {})
    assert schema.get("$ref") == "#/components/schemas/BudgetCheckRequest", (
        f"POST {path}: request must use BudgetCheckRequest"
    )


def assert_project_create_operation_request_binding(
    snapshot: dict[str, Any],
) -> None:
    """Bind project creation to its exact required request component."""
    path = "/api/v1/projects"
    request_body = snapshot["paths"][path]["post"]["requestBody"]
    assert request_body.get("required") is True, (
        f"POST {path}: request body must remain required"
    )
    content = request_body.get("content", {})
    assert "application/json" in content, (
        f"POST {path}: application/json request body is missing"
    )
    schema = content["application/json"].get("schema", {})
    assert schema.get("$ref") == "#/components/schemas/ProjectCreateRequest", (
        f"POST {path}: request must use ProjectCreateRequest"
    )


def assert_project_key_rotate_operation_request_binding(
    snapshot: dict[str, Any],
) -> None:
    """Bind key rotation to its exact required request component and bounds."""
    path = "/api/v1/projects/{project_id}/key/rotate"
    request_body = snapshot["paths"][path]["post"]["requestBody"]
    assert request_body.get("required") is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema.get("$ref") == "#/components/schemas/ProjectKeyRotateRequest"
    remote = _schema(snapshot, "ProjectKeyRotateRequest")["properties"][
        "grace_seconds"
    ]["anyOf"][0]
    local = ProjectKeyRotateRequest.model_json_schema()["properties"]["grace_seconds"][
        "anyOf"
    ][0]
    assert local.get("minimum") == remote.get("minimum") == 0
    assert local.get("maximum") == remote.get("maximum") == 604800


def assert_webhook_create_operation_request_binding(
    snapshot: dict[str, Any],
) -> None:
    """Bind webhook creation to the exact open-ended request component."""
    path = "/api/v1/projects/{project_id}/webhooks"
    request_body = snapshot["paths"][path]["post"]["requestBody"]
    assert request_body.get("required") is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema.get("$ref") == "#/components/schemas/WebhookCreateRequest"

    local_properties = WebhookCreateRequest.model_json_schema()["properties"]
    remote_properties = _schema(snapshot, "WebhookCreateRequest")["properties"]
    local_url = local_properties["url"]
    remote_url = remote_properties["url"]
    assert local_url.get("format") == remote_url.get("format") == "uri"
    assert local_url.get("minLength") == remote_url.get("minLength") == 1
    assert local_url.get("maxLength") == remote_url.get("maxLength") == 2083
    for events in (local_properties["events"], remote_properties["events"]):
        assert "enum" not in events
        assert "pattern" not in events


def assert_project_update_operation_request_binding(snapshot: dict[str, Any]) -> None:
    """Bind project rename to its exact required request component."""
    path = "/api/v1/projects/{project_id}"
    request_body = snapshot["paths"][path]["put"]["requestBody"]
    assert request_body.get("required") is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema.get("$ref") == "#/components/schemas/ProjectUpdateRequest"


def assert_budget_update_operation_request_binding(snapshot: dict[str, Any]) -> None:
    """Bind budget mutation PUT to its exact required request component."""
    path = "/api/v1/projects/{project_id}/budget"
    operation = snapshot["paths"][path]["put"]
    request_body = operation["requestBody"]
    assert request_body.get("required") is True, (
        f"PUT {path}: request body must remain required"
    )
    content = request_body.get("content", {})
    assert "application/json" in content, (
        f"PUT {path}: application/json request body is missing"
    )
    schema = content["application/json"].get("schema", {})
    assert schema.get("$ref") == "#/components/schemas/BudgetUpdateRequest", (
        f"PUT {path}: request must use BudgetUpdateRequest"
    )


def assert_budget_confirm_contract(snapshot: dict[str, Any]) -> None:
    """Freeze confirm request details and its exact no-content operation."""
    snapshot = _adopted_snapshot(snapshot)
    path = "/api/v1/budgets/confirm"
    operation = snapshot["paths"][path]["post"]
    request_body = operation["requestBody"]
    assert request_body.get("required") is True, (
        f"POST {path}: request body must remain required"
    )
    content = request_body.get("content", {})
    assert "application/json" in content, (
        f"POST {path}: application/json request body is missing"
    )
    schema = content["application/json"].get("schema", {})
    assert schema.get("$ref") == "#/components/schemas/BudgetConfirmRequest", (
        f"POST {path}: request must use BudgetConfirmRequest"
    )
    responses = operation.get("responses", {})
    assert "204" in responses, f"POST {path}: 204 response is missing"
    assert "content" not in responses["204"], (
        f"POST {path}: 204 response must remain bodyless"
    )
    alternate_successes = {
        status
        for status in responses
        if status != "204" and status.isdigit() and 200 <= int(status) <= 299
    }
    assert not alternate_successes, f"POST {path}: success must remain 204-only"

    local_confirm = BudgetConfirmRequest.model_json_schema()
    remote_confirm = _schema(snapshot, "BudgetConfirmRequest")
    local_token_details = _resolve_ref(
        local_confirm,
        local_confirm["properties"]["token_details"]["$ref"],
    )
    remote_token_details = _schema(snapshot, "TokenDetails")
    expected_token_fields = {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_5m_tokens",
        "cache_creation_1h_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "image_input_tokens",
        "image_output_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
        "tool_use_input_tokens",
    }
    for confirm, token_details in (
        (local_confirm, local_token_details),
        (remote_confirm, remote_token_details),
    ):
        assert confirm["properties"]["is_provider_fallback"].get("default") is False, (
            "BudgetConfirmRequest.is_provider_fallback: default differs"
        )
        assert confirm["properties"]["model"].get("maxLength") == 2048, (
            "BudgetConfirmRequest.model: maxLength differs"
        )
        assert confirm["properties"]["call_id"].get("maxLength") == 36, (
            "BudgetConfirmRequest.call_id: maxLength differs"
        )
        region = confirm["properties"]["provider_region"]
        region_string = next(
            branch for branch in region["anyOf"] if branch.get("type") == "string"
        )
        assert region_string.get("maxLength") == 32, (
            "BudgetConfirmRequest.provider_region: maxLength differs"
        )
        assert set(token_details["properties"]) == expected_token_fields | {
            "is_estimated"
        }, "BudgetConfirmRequest.token_details: field set differs"
        for field in expected_token_fields:
            field_schema = token_details["properties"][field]
            assert field_schema.get("minimum") == 0, (
                f"BudgetConfirmRequest.token_details.{field}: minimum differs"
            )
            assert field_schema.get("maximum") == 100_000_000, (
                f"BudgetConfirmRequest.token_details.{field}: maximum differs"
            )
            assert field_schema.get("default") == 0, (
                f"BudgetConfirmRequest.token_details.{field}: default differs"
            )
        assert token_details["properties"]["is_estimated"].get("default") is False, (
            "BudgetConfirmRequest.token_details.is_estimated: default differs"
        )


def test_snapshot_matches_every_public_request_mirror() -> None:
    assert_request_mirrors_match(_snapshot())


def test_project_update_operation_uses_the_registered_request_mirror() -> None:
    assert_project_update_operation_request_binding(_snapshot())


def test_audit_query_matches_the_fixed_page_contract() -> None:
    assert_audit_query_contract(_snapshot())


def test_unadopted_registry_names_only_fields_no_mirror_sends() -> None:
    """The registry may excuse absent fields, never hide an adopted one."""
    assert set(UNADOPTED_OPTIONAL_FIELDS) == set(UNADOPTED_FIELD_OWNERS)
    for component, fields in UNADOPTED_OPTIONAL_FIELDS.items():
        adopted = set(fields) & set(UNADOPTED_FIELD_OWNERS[component].model_fields)
        assert not adopted, (
            f"{component}: registry lists fields the CLI does send: {sorted(adopted)}"
        )


def test_budget_check_request_mirrors_the_agent_run_id_bound() -> None:
    snapshot = _snapshot()
    remote = _schema(snapshot, "BudgetCheckRequest")["properties"]["agent_run_id"]
    local = BudgetCheckRequest.model_json_schema()["properties"]["agent_run_id"]
    remote_string = next(
        branch for branch in remote["anyOf"] if branch.get("type") == "string"
    )
    local_string = next(
        branch for branch in local["anyOf"] if branch.get("type") == "string"
    )

    assert local_string.get("maxLength") == remote_string.get("maxLength") == 256
    assert "agent_run_id" not in _schema(snapshot, "BudgetCheckRequest")["required"]


def test_new_unregistered_optional_request_field_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["properties"]["future_field"] = {
        "type": "string"
    }

    with pytest.raises(AssertionError, match="BudgetCheckRequest: field set"):
        assert_request_mirrors_match(mutated)


def test_unadopted_request_field_becoming_required_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["required"].append("modality")

    with pytest.raises(
        AssertionError,
        match=r"BudgetCheckRequest\.modality: .*now requires it",
    ):
        assert_request_mirrors_match(mutated)


def test_stale_unadopted_request_field_entry_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["properties"].pop("modality")

    with pytest.raises(
        AssertionError,
        match=r"BudgetCheckRequest\.modality: .*no longer declares it",
    ):
        assert_request_mirrors_match(mutated)


def test_new_unregistered_threshold_config_trips_drift_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    mutated["components"]["schemas"]["FutureConfig"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"type": {"type": "string", "const": "future_rule"}},
        "required": ["type"],
    }
    _schema(mutated, "BudgetUpdateRequest")["properties"]["thresholds"]["items"][
        "oneOf"
    ].append({"$ref": "#/components/schemas/FutureConfig"})

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.thresholds\[\]<FutureConfig>",
    ):
        assert_request_mirrors_match(mutated)


def test_stale_unadopted_threshold_config_entry_trips_drift_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered config that leaves the snapshot still trips the guard.

    Nothing is unadopted today, so parametrizing over the registry would collect
    no cases and quietly stop covering the mechanism. Register a real config
    branch instead, then withdraw it from the snapshot: that is exactly the
    staleness a future gap would depend on the registry to catch.
    """
    monkeypatch.setattr(
        sys.modules[__name__],
        "UNADOPTED_THRESHOLD_CONFIGS",
        ("ScopedBudgetConfig",),
    )
    mutated = copy.deepcopy(_snapshot())
    thresholds = _schema(mutated, "BudgetUpdateRequest")["properties"]["thresholds"]
    thresholds["items"]["oneOf"] = [
        branch
        for branch in thresholds["items"]["oneOf"]
        if branch.get("$ref") != "#/components/schemas/ScopedBudgetConfig"
    ]

    with pytest.raises(AssertionError, match="are no longer all present"):
        assert_request_mirrors_match(mutated)


def test_init_reuses_guarded_login_and_project_create_requests() -> None:
    """Init composes existing request mirrors instead of inventing wire models."""
    snapshot = _snapshot()

    assert REQUEST_MIRRORS[LoginRequest] == "LoginRequest"
    assert REQUEST_MIRRORS[ProjectCreateRequest] == "ProjectCreateRequest"
    assert init_command.authenticate_with_password is login.authenticate_with_password
    assert init_command.request_project_creation is projects.request_project_creation
    assert_project_create_operation_request_binding(snapshot)


def test_costs_query_contract_matches_snapshot() -> None:
    assert_costs_query_contract(_snapshot())


def test_forecast_and_provider_query_contracts_match_snapshot() -> None:
    assert_forecast_and_provider_query_contracts(_snapshot())


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/api/v1/projects/{project_id}/forecast", "period"),
        ("/api/v1/projects/{project_id}/providers/failovers", "range"),
        ("/api/v1/projects/{project_id}/untracked-surfaces", "range"),
    ],
)
def test_forecast_and_provider_query_drift_trips_guard(path: str, name: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    parameter = _operation_parameter(
        mutated,
        path=path,
        method="get",
        location="query",
        name=name,
    )
    parameter["schema"]["default"] = "changed"

    with pytest.raises(AssertionError, match="default differs"):
        assert_forecast_and_provider_query_contracts(mutated)


def test_budget_check_operation_uses_required_request_component() -> None:
    assert_budget_check_operation_request_binding(_snapshot())


def test_project_create_operation_uses_required_request_component() -> None:
    assert_project_create_operation_request_binding(_snapshot())


def test_project_key_rotate_request_and_operation_match_snapshot() -> None:
    assert_project_key_rotate_operation_request_binding(_snapshot())


def test_webhook_create_request_and_operation_match_snapshot() -> None:
    snapshot = _snapshot()

    assert_request_mirrors_match(snapshot)
    assert_webhook_create_operation_request_binding(snapshot)


@pytest.mark.parametrize("mutation", ["field", "default", "format", "binding"])
def test_webhook_create_request_drift_trips_guard(mutation: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    schema = mutated["components"]["schemas"]["WebhookCreateRequest"]
    if mutation == "field":
        schema["properties"]["event_types"] = schema["properties"].pop("events")
    elif mutation == "default":
        schema["properties"]["events"]["default"] = "budget_threshold"
    elif mutation == "format":
        schema["properties"]["url"]["format"] = "url"
    else:
        mutated["paths"]["/api/v1/projects/{project_id}/webhooks"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"][
            "$ref"
        ] = "#/components/schemas/ProjectCreateRequest"

    with pytest.raises(AssertionError):
        assert_request_mirrors_match(mutated)
        assert_webhook_create_operation_request_binding(mutated)


@pytest.mark.parametrize("mutation", ["field", "default", "bounds", "binding"])
def test_project_key_rotate_request_drift_trips_guard(mutation: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    schema = mutated["components"]["schemas"]["ProjectKeyRotateRequest"]
    if mutation == "field":
        schema["properties"]["delay_seconds"] = schema["properties"].pop(
            "grace_seconds"
        )
    elif mutation == "default":
        schema["properties"]["grace_seconds"]["default"] = 30
    elif mutation == "bounds":
        schema["properties"]["grace_seconds"]["anyOf"][0]["maximum"] = 60
    else:
        mutated["paths"]["/api/v1/projects/{project_id}/key/rotate"]["post"][
            "requestBody"
        ]["content"]["application/json"]["schema"][
            "$ref"
        ] = "#/components/schemas/ProjectCreateRequest"

    with pytest.raises(AssertionError):
        assert_request_mirrors_match(mutated)
        assert_project_key_rotate_operation_request_binding(mutated)


def test_budget_update_operation_uses_required_request_component() -> None:
    assert_budget_update_operation_request_binding(_snapshot())


def test_budget_confirm_request_and_operation_match_snapshot() -> None:
    assert_budget_confirm_contract(_snapshot())


def test_costs_cli_domains_are_frozen() -> None:
    assert {member.value for member in CostRange} == {"7d", "30d", "90d", "365d"}
    parameter = _operation_parameter(
        _snapshot(),
        path="/api/v1/projects/{project_id}/costs",
        method="get",
        location="query",
        name="group_by",
    )
    assert parameter["schema"]["type"] == "string"
    assert "enum" not in parameter["schema"]
    assert "pattern" not in parameter["schema"]


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


def test_budget_check_request_field_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["properties"].pop("fallback_models")

    with pytest.raises(AssertionError, match="BudgetCheckRequest: field set"):
        assert_request_mirrors_match(mutated)


def test_budget_check_request_requiredness_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["required"].remove("provider")

    with pytest.raises(AssertionError, match="BudgetCheckRequest: required set"):
        assert_request_mirrors_match(mutated)


def test_budget_check_request_provider_enum_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "ProviderName")["enum"].remove("openai")

    with pytest.raises(
        AssertionError,
        match=r"BudgetCheckRequest\.(?:provider|fallback_providers\[\]): "
        r"enum/const/pattern values",
    ):
        assert_request_mirrors_match(mutated)


def test_budget_update_request_field_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetUpdateRequest")["properties"].pop("sub_limit_usd")

    with pytest.raises(AssertionError, match="BudgetUpdateRequest: field set"):
        assert_request_mirrors_match(mutated)


def test_budget_update_request_requiredness_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetUpdateRequest")["required"].remove("mode")

    with pytest.raises(AssertionError, match="BudgetUpdateRequest: required set"):
        assert_request_mirrors_match(mutated)


def test_budget_update_request_enum_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetMode")["enum"].remove("hard_deny")

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.mode: enum/const/pattern values",
    ):
        assert_request_mirrors_match(mutated)


def test_budget_update_request_union_branch_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    thresholds = _schema(mutated, "BudgetUpdateRequest")["properties"]["thresholds"]
    # Removed by identity, not by position: the snapshot's branch order is Core's
    # to choose, and the trailing branch is one the CLI does not mirror.
    thresholds["items"]["oneOf"] = [
        branch
        for branch in thresholds["items"]["oneOf"]
        if branch.get("$ref") != "#/components/schemas/UnitCostAnomalyConfig"
    ]

    with pytest.raises(
        AssertionError,
        match=r"BudgetUpdateRequest\.thresholds\[\]<UnitCostAnomalyConfig>",
    ):
        assert_request_mirrors_match(mutated)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("required", "request body must remain required"),
        ("content", "application/json request body is missing"),
        ("component", "request must use BudgetCheckRequest"),
    ],
)
def test_budget_check_operation_request_drift_trips_guard(
    mutation: str,
    message: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    request_body = mutated["paths"]["/api/v1/budgets/check"]["post"]["requestBody"]
    if mutation == "required":
        request_body["required"] = False
    elif mutation == "content":
        request_body["content"].pop("application/json")
    else:
        request_body["content"]["application/json"]["schema"]["$ref"] = (
            "#/components/schemas/UnrelatedRequest"
        )

    with pytest.raises(AssertionError, match=message):
        assert_budget_check_operation_request_binding(mutated)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("required", "request body must remain required"),
        ("content", "application/json request body is missing"),
        ("component", "request must use ProjectCreateRequest"),
    ],
)
def test_project_create_operation_request_drift_trips_guard(
    mutation: str,
    message: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    request_body = mutated["paths"]["/api/v1/projects"]["post"]["requestBody"]
    if mutation == "required":
        request_body["required"] = False
    elif mutation == "content":
        request_body["content"].pop("application/json")
    else:
        request_body["content"]["application/json"]["schema"]["$ref"] = (
            "#/components/schemas/UnrelatedRequest"
        )

    with pytest.raises(AssertionError, match=message):
        assert_project_create_operation_request_binding(mutated)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("required", "request body must remain required"),
        ("content", "application/json request body is missing"),
        ("component", "request must use BudgetUpdateRequest"),
    ],
)
def test_budget_update_operation_request_drift_trips_guard(
    mutation: str,
    message: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    request_body = mutated["paths"]["/api/v1/projects/{project_id}/budget"]["put"][
        "requestBody"
    ]
    if mutation == "required":
        request_body["required"] = False
    elif mutation == "content":
        request_body["content"].pop("application/json")
    else:
        request_body["content"]["application/json"]["schema"]["$ref"] = (
            "#/components/schemas/UnrelatedRequest"
        )

    with pytest.raises(AssertionError, match=message):
        assert_budget_update_operation_request_binding(mutated)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("required", "request body must remain required"),
        ("content", "application/json request body is missing"),
        ("component", "request must use BudgetConfirmRequest"),
        ("response", "204 response is missing"),
        ("response-content", "204 response must remain bodyless"),
        ("alternate-success", "success must remain 204-only"),
        ("alternate-success-205", "success must remain 204-only"),
    ],
)
def test_budget_confirm_operation_drift_trips_guard(
    mutation: str,
    message: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    operation = mutated["paths"]["/api/v1/budgets/confirm"]["post"]
    request_body = operation["requestBody"]
    if mutation == "required":
        request_body["required"] = False
    elif mutation == "content":
        request_body["content"].pop("application/json")
    elif mutation == "component":
        request_body["content"]["application/json"]["schema"]["$ref"] = (
            "#/components/schemas/UnrelatedRequest"
        )
    elif mutation == "response":
        operation["responses"].pop("204")
    elif mutation == "response-content":
        operation["responses"]["204"]["content"] = {
            "application/json": {"schema": {"type": "object"}}
        }
    elif mutation == "alternate-success-205":
        operation["responses"]["205"] = {"description": "Changed success"}
    else:
        operation["responses"]["200"] = {"description": "Changed success"}

    with pytest.raises(AssertionError, match=message):
        assert_budget_confirm_contract(mutated)


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


def test_budget_confirm_request_field_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetConfirmRequest")["properties"].pop("call_id")

    with pytest.raises(AssertionError, match="BudgetConfirmRequest: field set"):
        assert_request_mirrors_match(mutated)


def test_budget_confirm_request_requiredness_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetConfirmRequest")["required"].remove("token_details")

    with pytest.raises(AssertionError, match="BudgetConfirmRequest: required set"):
        assert_request_mirrors_match(mutated)


def test_budget_confirm_nested_requiredness_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "TokenDetails")["required"] = ["input_tokens"]

    with pytest.raises(
        AssertionError,
        match=r"BudgetConfirmRequest\.token_details: required set",
    ):
        assert_request_mirrors_match(mutated)


def test_budget_confirm_service_tier_enum_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    service_tier = _schema(mutated, "BudgetConfirmRequest")["properties"][
        "service_tier"
    ]
    service_tier["anyOf"][0]["enum"].remove("optimized")

    with pytest.raises(
        AssertionError,
        match=r"BudgetConfirmRequest\.service_tier: enum/const/pattern values",
    ):
        assert_request_mirrors_match(mutated)


def test_budget_confirm_fallback_default_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetConfirmRequest")["properties"]["is_provider_fallback"][
        "default"
    ] = True

    with pytest.raises(
        AssertionError,
        match=r"BudgetConfirmRequest\.is_provider_fallback: default",
    ):
        assert_budget_confirm_contract(mutated)


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        ("model", "BudgetConfirmRequest.model: maxLength"),
        ("call_id", "BudgetConfirmRequest.call_id: maxLength"),
        ("provider_region", "BudgetConfirmRequest.provider_region: maxLength"),
    ],
)
def test_budget_confirm_top_level_bound_drift_trips_guard(
    field: str,
    expected_message: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    field_schema = _schema(mutated, "BudgetConfirmRequest")["properties"][field]
    if field == "provider_region":
        field_schema = next(
            branch for branch in field_schema["anyOf"] if branch.get("type") == "string"
        )
    field_schema["maxLength"] += 1

    with pytest.raises(AssertionError, match=expected_message):
        assert_budget_confirm_contract(mutated)


@pytest.mark.parametrize("attribute", ["minimum", "maximum", "default"])
def test_nested_token_detail_numeric_contract_drift_trips_guard(
    attribute: str,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "TokenDetails")["properties"]["input_tokens"][attribute] = 1

    with pytest.raises(
        AssertionError,
        match=rf"BudgetConfirmRequest\.token_details\.input_tokens: {attribute}",
    ):
        assert_budget_confirm_contract(mutated)


def test_nested_token_detail_boolean_default_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "TokenDetails")["properties"]["is_estimated"]["default"] = True

    with pytest.raises(
        AssertionError,
        match=r"BudgetConfirmRequest\.token_details\.is_estimated: default",
    ):
        assert_budget_confirm_contract(mutated)


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


@pytest.mark.parametrize(
    ("field", "changed_default"),
    [
        ("budget_limit", 250.0),
        ("budget_period", "daily"),
        ("budget_mode", "hard_deny"),
    ],
)
def test_changed_project_create_default_trips_drift_guard(
    field: str,
    changed_default: object,
) -> None:
    mutated = copy.deepcopy(_snapshot())
    project_create = _schema(mutated, "ProjectCreateRequest")
    project_create["properties"][field]["default"] = changed_default

    with pytest.raises(
        AssertionError,
        match=rf"ProjectCreateRequest\.{field}: default differs",
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


@pytest.mark.parametrize("mutation", ["required", "not_nullable", "removed"])
def test_reservation_funding_specialization_still_guards_core_shape(mutation):
    snapshot = copy.deepcopy(_snapshot())
    confirm = _schema(snapshot, "BudgetConfirmRequest")
    if mutation == "required":
        confirm["required"].append("reservation_id")
    elif mutation == "not_nullable":
        confirm["properties"]["reservation_id"] = {"type": "string"}
    else:
        confirm["properties"].pop("reservation_id")
    with pytest.raises(AssertionError, match="BudgetConfirmRequest.reservation_id"):
        assert_request_mirrors_match(snapshot)


def test_budget_batch_request_operation_and_limits():
    snapshot = _snapshot()
    body = snapshot["paths"]["/api/v1/budgets/check/batch"]["post"]["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BudgetCheckBatchRequest"
    }
    assert REQUEST_MIRRORS[BudgetCheckBatchRequest] == "BudgetCheckBatchRequest"


@pytest.mark.parametrize("field", ["uncounted_calls", "uncounted_tokens"])
def test_budget_check_outage_default_drift_trips_guard(field: str) -> None:
    mutated = copy.deepcopy(_snapshot())
    _schema(mutated, "BudgetCheckRequest")["properties"][field]["default"] = 1
    with pytest.raises(AssertionError, match=rf"BudgetCheckRequest\.{field}: default"):
        assert_request_mirrors_match(mutated)


@pytest.mark.parametrize("field", ["uncounted_calls", "uncounted_tokens"])
def test_budget_check_outage_bounds_match_snapshot(field: str) -> None:
    remote = _schema(_snapshot(), "BudgetCheckRequest")
    local = BudgetCheckRequest.model_json_schema()
    assert field not in local["required"]
    assert field not in remote["required"]
    for attribute in ("minimum", "maximum", "default", "type"):
        assert (
            local["properties"][field][attribute]
            == remote["properties"][field][attribute]
        )
