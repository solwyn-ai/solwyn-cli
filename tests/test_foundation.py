"""Current Core foundation regressions: fail-closed parsing and safe round trips."""

import copy

import pytest
from pydantic import ValidationError

from solwyn_cli._vendor.wire import (
    BudgetCheckRequest,
    BudgetUpdateRequest,
)
from solwyn_cli.commands.budget import BudgetStatusData, _threshold_configs
from test_budget_command import BUDGET_RESPONSE, EVERY_RULE_TYPE_THRESHOLDS


def current_budget():
    data = copy.deepcopy(BUDGET_RESPONSE)
    data.update(outage_overspend_ceiling_usd=12.5, lease_float_usd=None)
    data["thresholds"] = copy.deepcopy(EVERY_RULE_TYPE_THRESHOLDS)
    data["thresholds"][2]["mode"] = "hard_deny"
    data["thresholds"][-1].update(
        scope="tag", match="team", match_value="", current_usage=None
    )
    return data


def test_current_budget_round_trip_preserves_rules_without_server_fields():
    status = BudgetStatusData.model_validate(current_budget())
    assert status.outage_overspend_ceiling_usd == 12.5
    assert status.lease_float_usd is None
    rules = _threshold_configs(status)
    body = BudgetUpdateRequest(
        limit_usd=100, period="monthly", mode="hard_deny", thresholds=rules
    ).model_dump(mode="json")
    assert body["thresholds"][2]["mode"] == "hard_deny"
    assert body["thresholds"][-1]["match_value"] == ""
    assert all(
        not {"rule_key", "routing", "current_usage"} & rule.keys()
        for rule in body["thresholds"]
    )


@pytest.mark.parametrize("field", ["outage_overspend_ceiling_usd", "current_usage"])
def test_required_budget_fields_cannot_be_omitted(field):
    data = current_budget()
    (data["thresholds"][-1] if field == "current_usage" else data).pop(field)
    with pytest.raises(ValidationError):
        BudgetStatusData.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"match_value": None},
        {"match": "x" * 65},
        {"match_value": "x" * 257},
        {"match": "bad\x00key"},
        {"match_value": "bad\x00value"},
        {"scope": "model", "match_value": "forbidden"},
    ],
)
def test_tag_rule_validation(changes):
    rule = dict(
        type="scoped_budget",
        scope="tag",
        match="team",
        match_value="",
        limit=10,
        mode="hard_deny",
    )
    rule.update(changes)
    with pytest.raises(ValidationError):
        BudgetUpdateRequest(
            limit_usd=100, period="monthly", mode="hard_deny", thresholds=[rule]
        )


def test_price_hints_can_be_requested():
    request = BudgetCheckRequest(
        estimated_input_tokens=10,
        model="gpt-4o",
        provider="openai",
        price_hints_version="1",
    )
    assert request.model_dump()["price_hints_version"] == "1"
