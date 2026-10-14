"""Typed response fields adopted from the pinned Core contract."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CostGroup(_Response):
    group_key: str | None
    provider: str | None = None
    modality: str | None = None
    agent_run_name: str | None = None
    total_cost: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    unpriced_call_count: int = 0
    local_call_count: int = 0


class SavingsSummary(_Response):
    saved_cost: float
    saved_cost_upper: float | None
    saved_input_tokens: int
    blocked_call_count: int


class CostWindow(_Response):
    requested_from: str
    requested_to: str
    effective_from: str
    effective_to: str
    retention_clamped: bool


class CostQueryResponse(_Response):
    project_id: str
    range: str
    group_by: str
    groups: list[CostGroup]
    truncated: bool
    savings: SavingsSummary | None = None
    window: CostWindow


class AccountCostDailyPoint(_Response):
    day: date
    cost: float


class AccountCostGroup(CostGroup):
    project_name: str | None = None
    current_period_cost: float | None = None
    daily: list[AccountCostDailyPoint] | None = None


class AccountCostQueryResponse(_Response):
    range: str
    group_by: str
    groups: list[AccountCostGroup]
    truncated: bool
    analytics_withheld: bool = False
    savings: SavingsSummary | None = None
    window: CostWindow


class MediaBreakdown(_Response):
    image_count: int | None = None
    generation_count: int | None = None
    video_seconds: float | None = None
    audio_seconds: float | None = None
    input_characters: int | None = None
    resolution: str | None = None
    quality: str | None = None
    is_estimated: bool = False


class CostEventResponse(_Response):
    id: str
    project_id: str
    agent_run_id: str | None
    provider: str
    model: str
    modality: Literal["text", "image", "audio", "video", "embedding"] = "text"
    provider_region: str | None = None
    service_tier: (
        Literal["auto", "default", "flex", "scale", "priority", "standard", "optimized"]
        | None
    ) = None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_input_tokens: int | None = None
    audio_output_tokens: int | None = None
    image_input_tokens: int | None = None
    image_output_tokens: int | None = None
    cost: float
    denied_cost: float
    deny_source: str | None
    deny_reason: str | None
    estimated_cost: float | None
    is_unpriced: bool = False
    is_estimated: bool = False
    media: MediaBreakdown | None = None
    tags: dict[str, str] | None = None
    status: str
    is_model_fallback: bool
    latency_ms: float
    sdk_instance_id: str
    created_at: str


class CostEventsResponse(_Response):
    items: list[CostEventResponse]
    next_cursor: str | None
    has_more: bool
    window: CostWindow


class CostTotalResponse(_Response):
    project_id: str
    range: str
    total_cost: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    unpriced_call_count: int
    local_call_count: int
    savings: SavingsSummary | None = None
    window: CostWindow
