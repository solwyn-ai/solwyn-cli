"""Strict alert history and receipt models shared across customer reads."""

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from solwyn_cli.commands._cost_models import CostWindow


class AlertEventWindow(CostWindow):
    """Alert history uses the same requested/effective window shape as costs."""

    model_config = ConfigDict(extra="forbid")


class AlertDeliveryLegEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leg_key: str
    channel_type: str
    channel_name: str | None = None
    status: Literal["pending", "retrying", "accepted", "delivered", "failed"]
    attempt_count: int
    last_attempted_at: datetime | None = None
    delivered_at: datetime | None = None
    error: str | None = None


class AlertDeliveryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "pending", "retrying", "processed", "delivered", "partial", "failed"
    ]
    attempt_count: int
    next_attempt_at: datetime | None = None
    dead_lettered_at: datetime | None = None
    legs: list[AlertDeliveryLegEntry]


class AlertEventEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str | None = None
    account_id: str
    scope: str
    rule_key: str
    emitted_at: datetime
    event_type: str
    rule_type: str | None = None
    value_at_event: float | None = None
    threshold_at_event: float | None = None
    period_key: str | None = None
    linked_event_ids: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    delivery: AlertDeliveryStatus | None = None


class AlertEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[AlertEventEntry]
    next_cursor: str | None = None
    window: AlertEventWindow
