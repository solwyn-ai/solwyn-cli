"""The opt-in journal adapter is pinned to the proposed Core API contract."""

import json
import re
from pathlib import Path

import pytest

from solwyn_cli._vendor.batch import (
    BudgetCheckOutcome,
    RecoverableBudgetCheckRequest,
    RecoverableBudgetCheckResponse,
)
from solwyn_cli.commands.budget import BudgetCheckData

SNAPSHOT = Path(__file__).parents[1] / "src/solwyn_cli/spec/openapi_snapshot.json"


def test_journal_route_and_response_shape_match_pinned_core_contract():
    spec = json.loads(SNAPSHOT.read_text())
    schemas = spec["components"]["schemas"]
    assert re.fullmatch(r"[0-9a-f]{40}", spec["x-solwyn-core-ref"])
    root = "/api/v1/budgets/check/batches"
    assert spec["paths"][root]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/RecoverableBudgetCheckRequest")
    for path, method in ((root, "post"), (root + "/{operation_id}", "get")):
        assert spec["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"].endswith("/RecoverableBudgetCheckResponse")
    request = RecoverableBudgetCheckRequest.model_json_schema()
    assert set(request["properties"]) == set(
        schemas["RecoverableBudgetCheckRequest"]["properties"]
    )
    assert (
        request["properties"]["checks"]["minItems"]
        == schemas["RecoverableBudgetCheckRequest"]["properties"]["checks"]["minItems"]
        == 1
    )
    assert (
        request["properties"]["checks"]["maxItems"]
        == schemas["RecoverableBudgetCheckRequest"]["properties"]["checks"]["maxItems"]
        == 32
    )
    for local, remote in (
        (BudgetCheckOutcome[BudgetCheckData], "BudgetCheckOutcome"),
        (
            RecoverableBudgetCheckResponse[BudgetCheckData],
            "RecoverableBudgetCheckResponse",
        ),
    ):
        actual = local.model_json_schema()
        assert set(actual["properties"]) == set(schemas[remote]["properties"])
        assert set(actual["required"]) == set(schemas[remote]["required"])
        for field in ("state", "status"):
            if field in actual["properties"]:
                assert (
                    actual["properties"][field]["enum"]
                    == schemas[remote]["properties"][field]["enum"]
                )
        assert (
            actual["additionalProperties"]
            is schemas[remote]["additionalProperties"]
            is False
        )


@pytest.mark.parametrize("checks", [[], [{}] * 33])
def test_out_of_bounds_journal_request_cannot_be_serialized(checks):
    with pytest.raises(ValueError):
        RecoverableBudgetCheckRequest(
            operation_id="01991a42-0000-7000-8000-000000000001", checks=checks
        )
