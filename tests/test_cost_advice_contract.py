"""OpenAPI drift guards for cost advice request and response contracts."""

import copy
import json
from pathlib import Path

import pytest
from test_request_drift import REQUEST_MIRRORS, assert_request_mirrors_match
from test_response_subset import OPERATION_RESPONSE_BINDINGS, RESPONSE_READS

from solwyn_cli._vendor.wire import CostSimulationRequest

SNAPSHOT = Path("src/solwyn_cli/spec/openapi_snapshot.json")
EXPECTED_RESPONSES = {
    ("GET", "/api/v1/projects/{project_id}/spend-baseline"): "BaselineResult",
    ("GET", "/api/v1/projects/{project_id}/unit-economics"): ("UnitEconomicsResponse"),
    ("GET", "/api/v1/projects/{project_id}/costs/drift"): "CostDriftResponse",
    ("GET", "/api/v1/projects/{project_id}/costs/priced-models"): (
        "PricedModelsResponse"
    ),
    ("POST", "/api/v1/projects/{project_id}/costs/simulate"): (
        "CostSimulationResponse"
    ),
}


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _query(snapshot: dict, path: str, method: str) -> dict[str, dict]:
    parameters = {}
    for parameter in snapshot["paths"][path][method]["parameters"]:
        if parameter["in"] != "query":
            continue
        copied = copy.deepcopy(parameter)
        copied["schema"].pop("title", None)
        parameters[parameter["name"]] = copied
    return parameters


def test_cost_advice_models_and_operations_are_registered_for_drift() -> None:
    assert REQUEST_MIRRORS[CostSimulationRequest] == "CostSimulationRequest"
    assert set(EXPECTED_RESPONSES.values()) <= set(RESPONSE_READS)
    for operation, component in EXPECTED_RESPONSES.items():
        assert OPERATION_RESPONSE_BINDINGS[operation] == ("200", "object", component)


def test_cost_simulation_operation_uses_required_json_request_mirror() -> None:
    operation = _snapshot()["paths"]["/api/v1/projects/{project_id}/costs/simulate"][
        "post"
    ]

    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CostSimulationRequest"
    }


def test_cost_advice_query_contracts_match_cli_bounds_and_defaults() -> None:
    snapshot = _snapshot()
    baseline = _query(snapshot, "/api/v1/projects/{project_id}/spend-baseline", "get")
    unit = _query(snapshot, "/api/v1/projects/{project_id}/unit-economics", "get")
    drift = _query(snapshot, "/api/v1/projects/{project_id}/costs/drift", "get")
    simulate = _query(snapshot, "/api/v1/projects/{project_id}/costs/simulate", "post")
    priced = _query(
        snapshot, "/api/v1/projects/{project_id}/costs/priced-models", "get"
    )

    assert baseline["days"]["schema"] == {
        "type": "integer",
        "maximum": 30,
        "minimum": 3,
        "default": 7,
    }
    assert unit["range"]["schema"] == {
        "enum": ["7d", "30d", "90d"],
        "type": "string",
        "default": "30d",
    }
    assert unit["vs_prev"]["schema"] == {"type": "boolean", "default": True}
    assert unit["cause"]["schema"] == {"type": "boolean", "default": False}
    for query in (drift, simulate):
        assert set(query) == {"range", "from", "to"}
        assert query["range"]["schema"] == {"type": "string", "default": "7d"}
    assert priced == {}


def test_cost_simulation_request_field_drift_trips_guard() -> None:
    mutated = copy.deepcopy(_snapshot())
    mutated["components"]["schemas"]["CostSimulationRequest"]["properties"].pop(
        "target_region"
    )

    with pytest.raises(AssertionError, match="CostSimulationRequest: field set"):
        assert_request_mirrors_match(mutated)
