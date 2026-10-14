"""Reusable analytics windows and stored-dimension query filters."""

import re
from datetime import datetime


class CostQueryError(ValueError):
    """A local query error, distinct from a malformed API response."""


class AccountProjectConflictError(CostQueryError):
    """Explicit account and project modes cannot be combined."""


class UnsupportedAccountCostFilterError(CostQueryError):
    """Account costs support fewer dimensions than project costs."""


def window_params(
    *, time_range: str = "7d", from_: str | None = None, to: str | None = None
) -> dict[str, str]:
    """Resolve a preset or a paired, timezone-aware custom window."""
    if from_ is None and to is None:
        return {"range": time_range}
    if from_ is None or to is None:
        raise CostQueryError("--from and --to must be provided together.")
    try:
        start, end = datetime.fromisoformat(from_), datetime.fromisoformat(to)
    except ValueError:
        raise CostQueryError(
            "--from and --to must be ISO timestamps with a timezone."
        ) from None
    if start.utcoffset() is None or end.utcoffset() is None:
        raise CostQueryError("--from and --to must include a timezone.")
    if end <= start:
        raise CostQueryError("--to must be later than --from.")
    return {"from": from_, "to": to}


def validate_group_by(group_by: str | None) -> None:
    """Core validates dimensions; reject malformed dynamic tag keys locally."""
    if group_by is not None and group_by.startswith("tag:"):
        key = group_by[4:]
        if not 1 <= len(key) <= 64 or "\0" in key:
            raise CostQueryError(
                "Tag group keys must contain 1–64 characters and no NUL."
            )


def cost_params(
    *,
    time_range: str = "7d",
    from_: str | None = None,
    to: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    modality: str | None = None,
    status: str | None = None,
    service_tier: str | None = None,
    provider_region: str | None = None,
    agent_run: str | None = None,
    failover_reason: str | None = None,
    model_fallback: bool | None = None,
    provider_fallback: bool | None = None,
    tag: list[str] | None = None,
) -> dict[str, str | bool]:
    """Map the shared CLI/MCP filters to Core, preserving false and empty tags."""
    params: dict[str, str | bool] = dict(
        window_params(time_range=time_range, from_=from_, to=to)
    )
    for name, value in {
        "model": model,
        "provider": provider,
        "modality": modality,
        "status": status,
        "service_tier": service_tier,
        "provider_region": provider_region,
        "failover_reason": failover_reason,
        "is_model_fallback": model_fallback,
        "is_provider_fallback": provider_fallback,
    }.items():
        if value is not None:
            params[name] = value
    if agent_run is not None:
        if len(agent_run) > 256:
            raise CostQueryError("--agent-run must contain at most 256 characters.")
        params[
            "agent_run_id"
            if re.fullmatch(r"run_[0-9a-f]{32}", agent_run)
            else "agent_run"
        ] = agent_run
    for item in tag or []:
        key, sep, value = item.partition("=")
        if not sep or not 1 <= len(key) <= 64 or len(value) > 256 or "\0" in item:
            raise CostQueryError(
                "--tag requires key=value, key 1–64, value at most 256, and no NUL."
            )
        if f"tag.{key}" in params:
            raise CostQueryError("Duplicate tag filter keys are not allowed.")
        params[f"tag.{key}"] = value
    return params
