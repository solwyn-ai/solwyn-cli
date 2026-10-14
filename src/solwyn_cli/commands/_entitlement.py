"""Typed response fields adopted from the pinned Core contract."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli.auth import AuthError
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import ApiError


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AnalyticsCapabilities(_Response):
    allowed_ranges: list[str]
    allowed_group_by: list[str]
    tags: bool
    csv_export_mode: Literal["none", "aggregated", "full"]
    cost_events_paginated: bool


class BudgetCapabilities(_Response):
    allowed_modes: list[str]
    allowed_periods: list[str]
    sub_period_enabled: bool
    max_thresholds: int
    max_hard_cap_projects: int
    account_spend_alerts: bool


class NotificationCapabilities(_Response):
    notification_channels: list[str]
    sms_allowed_events: list[str]
    email_allowed_events: list[str]


class ApiKeyCapabilities(_Response):
    expiry_configurable: bool


class IngestCapabilities(_Response):
    rate_limit_per_second: int


class EnforcementCapabilities(_Response):
    checks_per_minute: int


class TierCapabilities(_Response):
    analytics: AnalyticsCapabilities
    budget: BudgetCapabilities
    notifications: NotificationCapabilities
    api_keys: ApiKeyCapabilities
    ingest: IngestCapabilities
    enforcement: EnforcementCapabilities
    failover_entitlement: bool
    data_retention_days: int


class InvoiceSummary(_Response):
    hosted_url: str
    amount_due_cents: int
    period_end: str | None
    status: str


class PendingPlan(_Response):
    tier: Literal["production", "team", "scale"]
    billing_interval: Literal["month", "year"]


class ScheduledPlanChange(_Response):
    target_tier: Literal["production", "team", "scale"] | None
    target_billing_interval: Literal["month", "year"] | None
    effective_at: datetime


class SubscriptionStatusResponse(_Response):
    tier: Literal["free", "production", "team", "scale"]
    billing_interval: Literal["month", "year"] | None
    limits: dict[str, int]
    capabilities: TierCapabilities
    status: str | None = None
    current_period_end: datetime | None = None
    cancel_at: datetime | None = None
    grace_period_until: datetime | None = None
    stripe_customer_id: str | None = None
    recent_invoices: list[InvoiceSummary] = Field(default_factory=list)
    pending_plan: PendingPlan | None = None
    scheduled_plan_change: ScheduledPlanChange | None = None


def fetch_billing_status(*, client: SolwynClient) -> SubscriptionStatusResponse:
    """Read billing status once for this client (key or JWT); errors propagate."""
    cached = getattr(client, "_billing_status", None)
    if isinstance(cached, SubscriptionStatusResponse):
        return cached
    data = SubscriptionStatusResponse.model_validate(
        client.request("GET", "/api/v1/billing/status").json()
    )
    client._billing_status = data
    return data


def get_capabilities(*, client: SolwynClient) -> TierCapabilities | None:
    """Best-effort invocation cache; Core remains authoritative on a failed read."""
    if getattr(client, "_capabilities_checked", False):
        return getattr(client, "_capabilities", None)
    client._capabilities_checked = True
    try:
        capabilities = fetch_billing_status(client=client).capabilities
    except (ApiError, AuthError, ValueError, TypeError):
        capabilities = None
    client._capabilities = capabilities
    return capabilities


class BillingCatalogPriceMonth(_Response):
    amount_cents: int
    currency: str


class BillingCatalogPriceYear(_Response):
    amount_cents: int
    monthly_display_cents: int
    currency: str


class BillingCatalogPrices(_Response):
    month: BillingCatalogPriceMonth | None = None
    year: BillingCatalogPriceYear | None = None


class BillingCatalogOverage(_Response):
    included_requests: int
    published_rate: str
    unit_amount_decimal_cents: str


class BillingCatalogTier(_Response):
    tier: Literal["free", "production", "team", "scale"]
    display_name: str
    description: str
    most_popular: bool
    prices: BillingCatalogPrices
    overage: BillingCatalogOverage | None
    limits: dict[str, int]
    capabilities: TierCapabilities


class BillingCatalogResponse(_Response):
    currency: str
    tax_behavior: str
    tax_code: str
    tiers: dict[Literal["free", "production", "team", "scale"], BillingCatalogTier]


def fetch_billing_catalog(*, client: SolwynClient) -> BillingCatalogResponse:
    return BillingCatalogResponse.model_validate(
        client.request("GET", "/api/v1/billing/catalog").json()
    )
