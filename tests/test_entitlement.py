"""Best-effort entitlement reads are scoped to one client invocation."""

import httpx

from solwyn_cli.commands._entitlement import get_capabilities


class Client:
    def __init__(self):
        self.calls = 0

    def request(self, method, path):
        self.calls += 1
        assert (method, path) == ("GET", "/api/v1/billing/status")
        return httpx.Response(200, json={"invalid": True})


def test_unavailable_capabilities_are_cached_per_client():
    first = Client()
    assert get_capabilities(client=first) is None
    assert get_capabilities(client=first) is None
    assert first.calls == 1
    second = Client()
    assert get_capabilities(client=second) is None
    assert second.calls == 1


BILLING = {
    "tier": "team",
    "billing_interval": None,
    "limits": {},
    "capabilities": {
        "analytics": {
            "allowed_ranges": ["7d", "30d"],
            "allowed_group_by": ["day", "future_dimension"],
            "tags": True,
            "csv_export_mode": "full",
            "cost_events_paginated": True,
        },
        "budget": {
            "allowed_modes": ["alert_only"],
            "allowed_periods": ["monthly"],
            "sub_period_enabled": True,
            "max_thresholds": 10,
            "max_hard_cap_projects": -1,
            "account_spend_alerts": True,
        },
        "notifications": {
            "notification_channels": [],
            "sms_allowed_events": [],
            "email_allowed_events": [],
        },
        "api_keys": {"expiry_configurable": True},
        "ingest": {"rate_limit_per_second": 30},
        "enforcement": {"checks_per_minute": 12000},
        "failover_entitlement": True,
        "data_retention_days": 180,
    },
}


def test_success_is_typed_and_cached(monkeypatch):
    client = Client()
    calls = []
    monkeypatch.setattr(
        client,
        "request",
        lambda *args: calls.append(args) or httpx.Response(200, json=BILLING),
    )
    capabilities = get_capabilities(client=client)
    assert capabilities.data_retention_days == 180
    assert capabilities.analytics.allowed_group_by == ["day", "future_dimension"]
    assert get_capabilities(client=client) is capabilities
    assert len(calls) == 1
