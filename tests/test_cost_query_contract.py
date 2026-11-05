"""Guard all adopted cost query fields, including aliases and nullable bounds.

Cost queries are operation parameters, not OpenAPI component request bodies, so
these live alongside REQUEST_MIRRORS rather than inventing component schemas.
"""

import copy
import json
from pathlib import Path

import pytest

SNAPSHOT = Path("src/solwyn_cli/spec/openapi_snapshot.json")
QUERY_PARAMETER_MIRRORS = Path("tests/contract/cost_query_parameters.json")
ACCOUNT_QUERY_PARAMETER_MIRROR = Path(
    "tests/contract/account_cost_query_parameters.json"
)
OPERATIONS = ("", "/events", "/total", "/export")


def parameter_contract(snapshot):
    def structural(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return structural(
                    snapshot["components"]["schemas"][value["$ref"].split("/")[-1]]
                )
            return {
                k: structural(v)
                for k, v in value.items()
                if k not in {"title", "description", "examples"}
            }
        if isinstance(value, list):
            return [structural(v) for v in value]
        return value

    return {
        suffix: {
            p["name"]: structural(p)
            for p in snapshot["paths"]["/api/v1/projects/{project_id}/costs" + suffix][
                "get"
            ]["parameters"]
            if p["in"] == "query"
        }
        for suffix in OPERATIONS
    }


def assert_cost_query_parameters(snapshot):
    assert parameter_contract(snapshot) == json.loads(
        QUERY_PARAMETER_MIRRORS.read_text()
    )


def account_parameter_contract(snapshot):
    parameters = snapshot["paths"]["/api/v1/account/costs"]["get"]["parameters"]
    return {
        parameter["name"]: {
            key: value
            for key, value in parameter.items()
            if key not in {"title", "description", "examples"}
        }
        for parameter in parameters
        if parameter["in"] == "query"
    }


def assert_account_cost_query_parameters(snapshot):
    assert account_parameter_contract(snapshot) == json.loads(
        ACCOUNT_QUERY_PARAMETER_MIRROR.read_text()
    )


def test_every_adopted_query_parameter_matches_core():
    assert_cost_query_parameters(json.loads(SNAPSHOT.read_text()))


def test_every_adopted_account_cost_query_parameter_matches_core():
    assert_account_cost_query_parameters(json.loads(SNAPSHOT.read_text()))


@pytest.mark.parametrize(
    "field",
    ["range", "group_by", "model", "provider", "modality", "from", "to"],
)
def test_account_cost_query_shape_drift_is_detected(field):
    changed = copy.deepcopy(json.loads(SNAPSHOT.read_text()))
    params = changed["paths"]["/api/v1/account/costs"]["get"]["parameters"]
    parameter = next(item for item in params if item["name"] == field)
    parameter["required"] = True
    with pytest.raises(AssertionError):
        assert_account_cost_query_parameters(changed)


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "provider",
        "modality",
        "agent_run",
        "agent_run_id",
        "status",
        "service_tier",
        "provider_region",
        "is_model_fallback",
        "is_provider_fallback",
        "failover_reason",
        "from",
        "to",
        "tag.<key>",
    ],
)
def test_filter_shape_drift_is_detected(field):
    changed = copy.deepcopy(json.loads(SNAPSHOT.read_text()))
    params = changed["paths"]["/api/v1/projects/{project_id}/costs"]["get"][
        "parameters"
    ]
    param = next(p for p in params if p["name"] == field)
    param["required"] = True
    with pytest.raises(AssertionError):
        assert_cost_query_parameters(changed)


def test_no_limit_is_invented_for_aggregate_or_total():
    contract = parameter_contract(json.loads(SNAPSHOT.read_text()))
    assert "limit" not in contract[""]
    assert "limit" not in contract["/total"]
    assert contract["/events"]["limit"]["schema"]["minimum"] == 1
    assert contract["/events"]["limit"]["schema"]["maximum"] == 100


def test_export_response_media_types_and_truncation_header_match_core():
    response = json.loads(SNAPSHOT.read_text())["paths"][
        "/api/v1/projects/{project_id}/costs/export"
    ]["get"]["responses"]["200"]
    assert set(response["content"]) == {"text/csv", "application/x-ndjson"}
    assert all(
        item["schema"] == {"type": "string"} for item in response["content"].values()
    )
    assert response["headers"]["X-Solwyn-Truncated"]["schema"] == {"type": "boolean"}
