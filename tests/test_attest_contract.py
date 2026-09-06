"""Vendored query, media, and auth contracts for attestation exports."""

import json
from pathlib import Path

import pytest

from pydantic import BaseModel

from solwyn_cli.commands.attest import (
    AttestationAccountSpendAlert,
    AttestationAlertEvent,
    AttestationAlerts,
    AttestationAudit,
    AttestationAuditEvent,
    AttestationBudget,
    AttestationFormat,
    AttestationPresetRange,
    AttestationScope,
    AttestationSpendSummary,
    AttestationSubscription,
    AttestationSubscriptionSnapshot,
    AttestationWindow,
    SpendControlAttestation,
)


SNAPSHOT = Path("src/solwyn_cli/spec/openapi_snapshot.json")
ATTESTATION_QUERY_PARAMETER_MIRRORS = {
    "/api/v1/projects/{project_id}/spend-control-attestation": {
        "format": ({member.value for member in AttestationFormat}, "json"),
        "range": ({member.value for member in AttestationPresetRange}, "30d"),
        "from": (None, None),
        "to": (None, None),
    },
    "/api/v1/account/spend-control-attestation": {
        "format": ({member.value for member in AttestationFormat}, "json"),
        "range": ({member.value for member in AttestationPresetRange}, "30d"),
        "from": (None, None),
        "to": (None, None),
    },
}
ATTESTATION_RESPONSE_MIRRORS: dict[type[BaseModel], str] = {
    SpendControlAttestation: "SpendControlAttestation",
    AttestationScope: "AttestationScope",
    AttestationWindow: "AttestationWindow",
    AttestationSpendSummary: "AttestationSpendSummary",
    AttestationBudget: "AttestationBudget",
    AttestationAlertEvent: "AttestationAlertEvent",
    AttestationAlerts: "AttestationAlerts",
    AttestationAccountSpendAlert: "AttestationAccountSpendAlert",
    AttestationAuditEvent: "AttestationAuditEvent",
    AttestationSubscriptionSnapshot: "AttestationSubscriptionSnapshot",
    AttestationSubscription: "AttestationSubscription",
    AttestationAudit: "AttestationAudit",
}


def _snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("model", "component"), ATTESTATION_RESPONSE_MIRRORS.items())
def test_attestation_response_mirrors_match_fields_and_requiredness(
    model: type[BaseModel],
    component: str,
) -> None:
    remote = _snapshot()["components"]["schemas"][component]
    local = model.model_json_schema()

    assert set(local["properties"]) == set(remote["properties"])
    assert set(local["required"]) == set(remote["required"])
    assert local["additionalProperties"] is remote["additionalProperties"] is False


def test_attestation_response_mirrors_keep_closed_wire_domains() -> None:
    document = _snapshot()
    schemas = document["components"]["schemas"]
    local_report = SpendControlAttestation.model_json_schema()["properties"]
    local_scope = AttestationScope.model_json_schema()["properties"]

    report_ranges = {
        "7d",
        "30d",
        "90d",
        "365d",
        "custom",
    }
    scope_types = {
        "account",
        "project",
    }
    assert set(local_report["range"]["enum"]) == report_ranges
    assert set(schemas["SpendControlAttestation"]["properties"]["range"]["enum"]) == (
        report_ranges
    )
    assert set(local_scope["type"]["enum"]) == scope_types
    assert set(schemas["AttestationScope"]["properties"]["type"]["enum"]) == (
        scope_types
    )
    assert local_report["schema_version"]["const"] == "1"
    assert (
        schemas["SpendControlAttestation"]["properties"]["schema_version"]["const"]
        == "1"
    )


@pytest.mark.parametrize("path", ATTESTATION_QUERY_PARAMETER_MIRRORS)
def test_attestation_query_parameter_mirrors_match_snapshot(path: str) -> None:
    document = _snapshot()
    operation = document["paths"][path]["get"]
    query = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "query"
    }
    expected = ATTESTATION_QUERY_PARAMETER_MIRRORS[path]

    assert set(query) == set(expected)
    for name, (choices, default) in expected.items():
        parameter = query[name]
        assert parameter["required"] is False
        schema = parameter["schema"]
        assert schema.get("default") == default
        if choices is not None:
            assert set(schema["enum"]) == choices
        else:
            assert schema["anyOf"] == [
                {"type": "string", "format": "date-time"},
                {"type": "null"},
            ]


@pytest.mark.parametrize("path", ATTESTATION_QUERY_PARAMETER_MIRRORS)
def test_attestation_success_media_bind_to_strict_json_and_csv(path: str) -> None:
    response = _snapshot()["paths"][path]["get"]["responses"]["200"]
    content = response["content"]

    assert content["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SpendControlAttestation"
    }
    assert content["text/csv"]["schema"] == {"type": "string"}


def test_attestation_auth_contract_is_account_jwt_and_project_dual_auth() -> None:
    paths = _snapshot()["paths"]

    assert paths["/api/v1/account/spend-control-attestation"]["get"]["security"] == [
        {"BearerAuth": []}
    ]
    assert paths["/api/v1/projects/{project_id}/spend-control-attestation"]["get"][
        "security"
    ] == [{"BearerAuth": []}, {"ApiKeyAuth": []}]
