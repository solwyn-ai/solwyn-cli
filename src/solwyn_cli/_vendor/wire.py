"""Pydantic v2 mirrors of the request bodies constructed by the CLI."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderName(StrEnum):
    """Provider identifiers accepted by budget request endpoints."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    BEDROCK = "bedrock"
    XAI = "xai"
    DEEPSEEK = "deepseek"
    MISTRAL = "mistral"
    QWEN = "qwen"
    GROQ = "groq"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    PERPLEXITY = "perplexity"
    AZURE_OPENAI = "azure_openai"
    OPENROUTER = "openrouter"
    ZAI = "zai"
    OLLAMA = "ollama"
    VLLM = "vllm"
    LMSTUDIO = "lmstudio"
    OPENAI_COMPATIBLE = "openai_compatible"


class ServiceTier(StrEnum):
    """Provider service tiers accepted by budget confirmation."""

    AUTO = "auto"
    DEFAULT = "default"
    FLEX = "flex"
    SCALE = "scale"
    PRIORITY = "priority"
    STANDARD = "standard"
    OPTIMIZED = "optimized"


class BudgetPeriod(StrEnum):
    """Rolling window for budget accounting."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class BudgetMode(StrEnum):
    """Budget enforcement mode."""

    ALERT_ONLY = "alert_only"
    HARD_DENY = "hard_deny"


class ProjectKeyScope(StrEnum):
    """Capability scope granted to a single project API key."""

    FULL = "full"
    READ_ONLY = "read_only"


class _ThresholdConfig(_WireModel):
    type: Literal["budget_threshold"] = "budget_threshold"
    percent: Annotated[int, Field(ge=1, le=100)]
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


class _ForecastOverrunConfig(_WireModel):
    type: Literal["forecast_overrun"]
    spend_threshold_usd: Annotated[float, Field(gt=0)]
    period: Literal["month"] = "month"
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


class _RunawayRunConfig(_WireModel):
    type: Literal["runaway_run"]
    spend_threshold_usd: Annotated[float, Field(gt=0, le=1999999.99)]
    call_threshold: Annotated[int, Field(ge=1)] | None = None
    window_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


class _DailyAnomalyConfig(_WireModel):
    type: Literal["daily_anomaly"]
    multiplier: Annotated[float, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


class _UnitCostAnomalyConfig(_WireModel):
    type: Literal["unit_cost_anomaly"]
    multiplier: Annotated[float, Field(gt=1, le=100)]
    lookback_days: Annotated[int, Field(ge=3, le=30)] = 7
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


class _ScopedBudgetConfig(_WireModel):
    type: Literal["scoped_budget"]
    scope: Literal["model", "provider", "agent_run"]
    match: Annotated[str, Field(min_length=1, max_length=2048)]
    limit: Annotated[float, Field(gt=0, le=999999.999999)]
    mode: BudgetMode
    channel_ids: list[str] = Field(default_factory=list)
    snoozed_until: datetime | None = None
    paused: bool = False


_BudgetRule = (
    _ThresholdConfig
    | _ForecastOverrunConfig
    | _RunawayRunConfig
    | _DailyAnomalyConfig
    | _UnitCostAnomalyConfig
    | _ScopedBudgetConfig
)


class TokenDetails(_WireModel):
    """Normalized provider token usage used to settle a reservation."""

    input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    cached_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    cache_creation_5m_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    cache_creation_1h_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    reasoning_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    audio_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    audio_output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    accepted_prediction_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    rejected_prediction_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    tool_use_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    is_estimated: bool = False


class LoginRequest(_WireModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: str
    password: Annotated[SecretStr, Field(max_length=128)]
    plan: Literal["founder", "team", "scale"] | None = None
    billing_interval: Literal["month", "year"] | None = None

    @field_serializer("password")
    def serialize_password(self, value: SecretStr) -> str:
        """Reveal the password only while constructing the login wire body."""
        return value.get_secret_value()


class RefreshRequest(_WireModel):
    refresh_token: str


class ProjectCreateRequest(_WireModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    budget_limit: Annotated[float, Field(gt=0)] = 100.0
    budget_period: Annotated[str, Field(pattern=r"^(daily|weekly|monthly)$")] = (
        "monthly"
    )
    budget_mode: Annotated[str, Field(pattern=r"^(alert_only|hard_deny)$")] = (
        "alert_only"
    )
    # Declared, never posted unless chosen: the request is serialized with
    # exclude_unset, so an unset scope is omitted and the API applies this same
    # default. The drift guard pins the two defaults together.
    scope: ProjectKeyScope = ProjectKeyScope.FULL


class ProjectKeyRotateRequest(_WireModel):
    grace_seconds: Annotated[int, Field(ge=0, le=604800)] | None = None
    # No default on the wire: a null scope asks the API to carry the replaced
    # key's scope over to the replacement.
    scope: ProjectKeyScope | None = None


class BudgetUpdateRequest(_WireModel):
    limit_usd: Annotated[float, Field(gt=0)]
    period: BudgetPeriod
    mode: BudgetMode
    sub_period: BudgetPeriod | None = None
    sub_limit_usd: float | None = None
    thresholds: Annotated[list[_BudgetRule], Field(max_length=50)]


class BudgetCheckRequest(_WireModel):
    estimated_input_tokens: Annotated[int, Field(ge=0)]
    model: Annotated[str, Field(max_length=2048)]
    provider: ProviderName
    fallback_providers: list[ProviderName] = Field(default_factory=list)
    fallback_models: Annotated[
        list[Annotated[str, Field(max_length=2048)]], Field(max_length=8)
    ] = Field(default_factory=list)
    agent_run_id: Annotated[str, Field(max_length=256)] | None = None


class BudgetConfirmRequest(_WireModel):
    reservation_id: str
    model: Annotated[str, Field(max_length=2048)]
    provider: ProviderName
    is_provider_fallback: bool = False
    call_id: Annotated[str, Field(max_length=36)]
    token_details: TokenDetails
    provider_region: Annotated[str, Field(max_length=32)] | None = None
    service_tier: ServiceTier | None = None
