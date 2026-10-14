"""Strict customer billing mirrors shared by CLI writes and MCP preview reads."""

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli.auth import JwtCredential, JwtRequiredError
from solwyn_cli.client import SolwynClient

PaidTier = Literal["production", "team", "scale"]
BillingInterval = Literal["month", "year"]


class _BillingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PlanPreviewRequest(_BillingModel):
    target_tier: PaidTier
    target_billing_interval: BillingInterval


class PlanChangeRequest(_BillingModel):
    target_tier: PaidTier
    target_billing_interval: BillingInterval


class CheckoutRequest(_BillingModel):
    tier: PaidTier
    billing_interval: BillingInterval
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class PlanPreviewLineResponse(_BillingModel):
    description: str
    amount_cents: int
    currency: str
    period_start: datetime | None
    period_end: datetime | None
    proration: bool


class PlanPreviewResponse(_BillingModel):
    target_tier: PaidTier
    target_billing_interval: BillingInterval
    mode: Literal["immediate", "scheduled"]
    effective_at: datetime | None
    proration_date: int
    amount_due_cents: int
    total_cents: int
    currency: str
    lines: list[PlanPreviewLineResponse]


class PlanChangeResponse(_BillingModel):
    mode: Literal["immediate", "scheduled"]
    status: str
    proration_date: int | None = None
    effective_at: datetime | None = None


class CancelResponse(_BillingModel):
    status: str
    cancel_at: datetime | None


class CheckoutResponse(_BillingModel):
    checkout_url: str


class PortalResponse(_BillingModel):
    portal_url: str


def require_billing_jwt(client: SolwynClient) -> None:
    """Respect credential selection; never bypass a selected project key."""
    if not isinstance(client.current_credential, JwtCredential):
        raise JwtRequiredError(
            "Billing requires JWT. Run solwyn login; a selected project key takes precedence."
        )


def fetch_plan_preview(
    *, client: SolwynClient, tier: PaidTier, interval: BillingInterval
) -> PlanPreviewResponse:
    """Read Stripe's estimate without changing the subscription or billing."""
    require_billing_jwt(client)
    request = PlanPreviewRequest(target_tier=tier, target_billing_interval=interval)
    data = PlanPreviewResponse.model_validate(
        client.request(
            "POST", "/api/v1/billing/plan-preview", json=request.model_dump()
        ).json()
    )
    if (data.target_tier, data.target_billing_interval) != (tier, interval):
        raise ValueError("Preview target did not match the request.")
    return data


def require_empty_204(response: httpx.Response) -> None:
    if response.status_code != 204 or response.content:
        raise ValueError("Expected an empty billing 204 response.")
