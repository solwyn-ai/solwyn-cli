"""Pin billing lifecycle field optionality and nullability to Core's snapshot."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from solwyn_cli.commands._billing import (
    CancelResponse,
    CheckoutRequest,
    CheckoutResponse,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanPreviewLineResponse,
    PlanPreviewRequest,
    PlanPreviewResponse,
    PortalResponse,
)
from solwyn_cli.commands._entitlement import (
    PendingPlan,
    ScheduledPlanChange,
    SubscriptionStatusResponse,
)
from solwyn_cli.errors import (
    BillingConflictDetail,
    BillingNotFoundDetail,
    BillingUnavailableDetail,
    BillingValidationDetail,
)


@pytest.mark.parametrize(
    ("model", "component"),
    [
        *[
            (model, model.__name__)
            for model in (
                CancelResponse,
                CheckoutRequest,
                CheckoutResponse,
                PortalResponse,
                PlanPreviewRequest,
                PlanChangeRequest,
                PlanPreviewLineResponse,
                PlanPreviewResponse,
                PlanChangeResponse,
                BillingConflictDetail,
                BillingUnavailableDetail,
                BillingNotFoundDetail,
                BillingValidationDetail,
            )
        ],
        (SubscriptionStatusResponse, "SubscriptionStatusResponse"),
        (PendingPlan, "PendingPlan"),
        (ScheduledPlanChange, "ScheduledPlanChangeResponse"),
    ],
)
def test_billing_status_mirrors_core_optionality_and_nullability(
    model: type[BaseModel], component: str
) -> None:
    document = json.loads(Path("src/solwyn_cli/spec/openapi_snapshot.json").read_text())
    remote = document["components"]["schemas"][component]
    local = model.model_json_schema()
    assert set(local["properties"]) == set(remote["properties"])
    assert set(local["required"]) == set(remote["required"])
    for field in local["properties"]:
        local_field = local["properties"][field]
        remote_field = remote["properties"][field]
        assert local_field.get("default") == remote_field.get("default")
        assert any(
            branch.get("type") == "null" for branch in local_field.get("anyOf", [])
        ) == any(
            branch.get("type") == "null" for branch in remote_field.get("anyOf", [])
        )
