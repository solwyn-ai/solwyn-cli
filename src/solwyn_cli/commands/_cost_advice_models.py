"""Typed response models for cost analysis and pricing advice."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from solwyn_cli._vendor.wire import ProviderName, ServiceTier
from solwyn_cli.commands._cost_models import CostWindow


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class BaselineResult(_Response):
    project_id: str
    lookback_days: int
    days_with_data: int
    mean_daily_spend: float
    stdev_daily_spend: float
    armed: bool


class DeltaCell(_Response):
    value: float
    prev_value: float | None
    delta_pct: float | None
    context: str


class CauseSpan(_Response):
    kind: str
    label: str
    contribution_pct: float


class CauseResult(_Response):
    summary: str
    confidence: Literal["low", "medium", "high"]
    spans: list[CauseSpan]


class UnitEconomicsResponse(_Response):
    range: Literal["7d", "30d", "90d"]
    window_start: str
    window_end: str
    prev_window_start: str | None
    prev_window_end: str | None
    cost_per_request: DeltaCell
    cost_per_agent_run: DeltaCell | None
    tokens_per_dollar: DeltaCell
    avg_prompt_tokens: DeltaCell
    suspected_cause: CauseResult | None


class DriftGroup(_Response):
    provider: str
    model: str
    actual_cost: float
    estimated_cost: float
    difference: float
    event_count: int


class DriftCaveats(_Response):
    estimate_heuristic: str = "server_side_input_equals_output"
    null_estimates_excluded: bool = True
    unit_priced_and_unpriced_estimates_are_null: bool = True
    denied_events_included: bool = True


class CostDriftResponse(_Response):
    project_id: str
    range: str
    window: CostWindow
    actual_total: float
    estimate_total: float
    difference: float
    included_event_count: int
    missing_estimate_count: int
    groups: list[DriftGroup]
    caveats: DriftCaveats


class PricedModelTarget(_Response):
    provider: ProviderName
    model: str
    modality: Literal["text", "image", "audio", "video", "embedding"]


class PricedModelsResponse(_Response):
    project_id: str
    pricing_version: str
    provenance: Literal["first_party_vendored_dataset"] = "first_party_vendored_dataset"
    models: list[PricedModelTarget]


class CostSimulationTarget(_Response):
    provider: ProviderName
    model: str
    modality: Literal["text", "image", "audio", "video", "embedding"]
    region: str | None
    service_tier: ServiceTier | None


class SimulationExcludedCounts(_Response):
    unpriced: int = 0
    local: int = 0
    modality_mismatch: int = 0
    missing_breakdown: int = 0


class SimulationCaveats(_Response):
    pricing_version: str = "2026-09-23"
    current_rates_snapshot: bool = True
    baseline_uses_stored_actual_cost: bool = True
    tokenizer_equivalence_approximation: bool = True
    unpriced_and_local_excluded: bool = True
    modality_mismatches_excluded: bool = True


class CostSimulationResponse(_Response):
    project_id: str
    range: str
    window: CostWindow
    target: CostSimulationTarget
    baseline_actual_cost: float
    simulated_cost: float
    delta_cost: float
    scanned_event_count: int
    baseline_event_count: int
    included_event_count: int
    excluded_event_count: int
    excluded: SimulationExcludedCounts
    caveats: SimulationCaveats
