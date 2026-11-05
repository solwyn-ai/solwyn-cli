"""Pin the exact supported history/export query shapes independently of Core."""

import copy
import json
from pathlib import Path

import pytest

SNAPSHOT = Path("src/solwyn_cli/spec/openapi_snapshot.json")
QUERY_PARAMETER_MIRRORS = Path("tests/contract/alert_query_parameters.json")
PATHS = ("/api/v1/projects/{project_id}/alert-events", "/api/v1/account/alert-events")


def parameter_contract(snapshot):
    def structural(value):
        if isinstance(value, dict):
            return {
                k: structural(v)
                for k, v in value.items()
                if k not in {"description", "title", "examples"}
            }
        if isinstance(value, list):
            return [structural(v) for v in value]
        return value

    return {
        path + suffix: {
            p["name"]: structural(p)
            for p in snapshot["paths"][path + suffix]["get"]["parameters"]
            if p["in"] == "query"
        }
        for path in PATHS
        for suffix in ("", "/export")
    }


def test_adopted_alert_query_parameters_match_core():
    contract = parameter_contract(json.loads(SNAPSHOT.read_text()))
    assert contract == json.loads(QUERY_PARAMETER_MIRRORS.read_text())
    for path, params in contract.items():
        assert "limit" not in params
        assert "failed_only" not in params
        assert set(params) == (
            {"range", "from", "to", "format"}
            if path.endswith("/export")
            else {"range", "from", "to", "scope", "rule_type", "cursor"}
        )


@pytest.mark.parametrize(
    "name", ["range", "from", "to", "rule_type", "scope", "cursor"]
)
def test_drift_detects_query_removal(name):
    snapshot = copy.deepcopy(json.loads(SNAPSHOT.read_text()))
    operation = snapshot["paths"][PATHS[0]]["get"]
    operation["parameters"] = [p for p in operation["parameters"] if p["name"] != name]
    assert parameter_contract(snapshot) != json.loads(
        QUERY_PARAMETER_MIRRORS.read_text()
    )


READ_PATHS = {
    "/api/v1/account/alerts/activity": "get",
    "/api/v1/account/alerts/open-deliveries": "get",
    "/api/v1/account/alerts/latest-events": "post",
}


def read_parameter_contract(snapshot):
    # Reuse the same structural serializer by presenting each route as a
    # history operation; endpoint names/methods remain pinned independently.
    def structural(value):
        if isinstance(value, dict):
            return {
                k: structural(v)
                for k, v in value.items()
                if k not in {"description", "title", "examples"}
            }
        if isinstance(value, list):
            return [structural(v) for v in value]
        return value

    return {
        path: {
            p["name"]: structural(p)
            for p in snapshot["paths"][path][method].get("parameters", [])
            if p["in"] == "query"
        }
        for path, method in READ_PATHS.items()
    }


def test_new_alert_read_query_contract():
    snapshot = json.loads(SNAPSHOT.read_text())
    expected = json.loads(
        Path("tests/contract/alert_read_query_parameters.json").read_text()
    )
    assert read_parameter_contract(snapshot) == expected
    assert set(expected["/api/v1/account/alerts/activity"]) == {
        "range",
        "event_type",
        "cursor",
    }
    assert set(expected["/api/v1/account/alerts/open-deliveries"]) == {"cursor"}
    assert set(expected["/api/v1/account/alerts/latest-events"]) == {"range"}


@pytest.mark.parametrize("path,method", list(READ_PATHS.items()))
def test_new_read_query_drift_is_detected(path, method):
    snapshot = json.loads(SNAPSHOT.read_text())
    expected = read_parameter_contract(snapshot)
    snapshot["paths"][path][method]["parameters"].pop()
    assert read_parameter_contract(snapshot) != expected
