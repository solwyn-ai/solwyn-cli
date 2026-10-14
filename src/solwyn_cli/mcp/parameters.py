"""Shared descriptions only where named inputs have the same semantics.

Tool declarations select these explicitly by parameter name. Missing descriptions
fail construction; these strings do not implement validation or authorization.
"""

from copy import copy

from pydantic import BaseModel, create_model

from solwyn_cli._vendor import wire
from solwyn_cli.commands.budget import DenialRecordingRetry as _DenialRecordingRetry

PROJECT = "Read project ID; non-empty when supplied. Overrides launch --project, then SOLWYN_PROJECT_ID/config default_project. A key must have access to this project; this never changes the authenticated write project."
TAGS = "Attribution tags; explicit keys override SOLWYN_TAGS. At most 10 merged keys, keys 1–64 characters, values at most 256, no NUL. Whitespace and case are preserved."
RUN_ID = "Stable caller run ID (at most 256 characters); overrides SOLWYN_AGENT_RUN_ID. Reuse across this run; never replace it to evade a stopped run."
TOKEN_COUNTS = {
    "input_tokens": "Total observed input tokens, including cached, cache creation, audio, image and tool-use input subsets; integer 0–100,000,000. The sum of the three cache buckets must not exceed this total.",
    "output_tokens": "Total observed output tokens, including reasoning, audio, image and prediction subsets; integer 0–100,000,000.",
    "cached_input_tokens": "Cached-read input token subset already included in input_tokens; integer 0–100,000,000, not additional tokens.",
    "cache_creation_5m_tokens": "Input tokens written to the five-minute cache; subset of input_tokens, integer 0–100,000,000.",
    "cache_creation_1h_tokens": "Input tokens written to the one-hour cache; subset of input_tokens, integer 0–100,000,000.",
    "reasoning_tokens": "Reasoning token subset already included in output_tokens; integer 0–100,000,000, not additional tokens.",
    "audio_input_tokens": "Audio token subset already included in input_tokens; integer 0–100,000,000. Distinct from audio seconds.",
    "audio_output_tokens": "Audio token subset already included in output_tokens; integer 0–100,000,000. Distinct from audio seconds.",
    "image_input_tokens": "Image token subset already included in input_tokens; integer 0–100,000,000. Distinct from image count.",
    "image_output_tokens": "Image token subset already included in output_tokens; integer 0–100,000,000. Distinct from image count.",
    "accepted_prediction_tokens": "Accepted prediction token subset of output_tokens; integer 0–100,000,000. Do not add again to output total.",
    "rejected_prediction_tokens": "Rejected prediction token bucket reported by the provider; integer 0–100,000,000. Preserve provider usage without double-counting totals.",
    "tool_use_input_tokens": "Tool-use input token subset already included in input_tokens; integer 0–100,000,000, not additional tokens.",
}
COMMON = {
    "project_id": PROJECT,
    "tags": TAGS,
    "agent_run_id": RUN_ID,
    "model": "Model used for this call, at most 2048 characters; use the actual served model when recording or confirming.",
    "provider": "Provider used for this call; use the actual served provider when recording or confirming.",
    "is_estimated": "True when usage quantities are estimates rather than observed provider usage; false by default.",
    "estimated_input_tokens": "Estimated input token count for admission; nonnegative integer. This estimate reserves budget, not provider output.",
    "estimated_output_tokens": "Optional output token bound, integer 0–100,000,000, for denial attribution only; does not set the reservation amount or provider generation limit.",
    "fallback_providers": "Ordered provider alternatives available to the caller, at most 8; must align one-for-one with fallback_models. Inspect the decision before routing; Solwyn does not invoke providers.",
    "fallback_models": "Ordered model alternatives available to the caller, at most 8 models of at most 2048 characters each; must align one-for-one with fallback_providers. Inspect returned routing before spending.",
    "image_count": "Estimated number of billed images, nonnegative integer; null means unobservable, zero means no billed images.",
    "generation_count": "Estimated number of billed generations, nonnegative integer; null means unobservable, zero means none.",
    "video_seconds": "Estimated billed video duration in seconds, nonnegative; null means unobservable, zero means no duration.",
    "audio_seconds": "Estimated billed audio duration in seconds, nonnegative; null means unobservable, zero means no duration.",
    "input_characters": "Estimated billed input character count, nonnegative integer; null means unobservable, zero means none.",
    "resolution": "Pricing resolution selector, at most 32 characters; preserve the model's supported variant rather than inventing a default.",
    "quality": "Pricing quality selector, at most 32 characters; preserve the model's supported variant rather than inventing a default.",
    "provider_region": "Actual served provider region for regional pricing, at most 32 characters; omit when unavailable.",
    "service_tier": "Actual served service tier for pricing; omit when unavailable so Core applies its default pricing behavior.",
    "latency_ms": "Observed call latency in milliseconds; finite, nonnegative number. Used for attribution, not reservation settlement pricing.",
    "status": "Completed provider-call outcome: success or error. Used for attribution; an error does not imply zero observed usage.",
    "parent_run_id": "Parent caller run ID for attribution, at most 256 characters; does not change the reservation's run identity.",
    "parent_agent_run_id": "Parent caller run ID for attribution, at most 256 characters; omit for a root run.",
    "run_name": "Human-readable run name for attribution, at most 255 characters; distinct from stable agent_run_id.",
    "agent_run_name": "Human-readable run name for attribution, at most 255 characters; distinct from stable agent_run_id.",
    "limit": "Maximum entries in this page, integer 1–100; defaults to 25. Continue using the returned cursor.",
    "cursor": "Opaque cursor from a previous page of the same query; preserve filters and ordering when continuing.",
    "time_range": "Preset query window; paired custom start/end timestamps override this preset. Available values are declared by the schema.",
    "from_": "Custom window start: timezone-aware ISO timestamp paired with to; the pair overrides time_range.",
    "to": "Custom window end: timezone-aware ISO timestamp paired with from_, strictly later than from_; overrides time_range.",
    **TOKEN_COUNTS,
}
COST_FILTERS = {
    "model": "Filter stored costs by exact served model identifier; omit to include all models.",
    "provider": "Filter stored costs by served provider; omit to include all providers.",
    "modality": "Filter stored costs by call modality; omit to include all modalities.",
    "status": "Filter stored costs by recorded call status; omit to include all statuses.",
    "service_tier": "Filter stored costs by served service tier; omit to include all tiers.",
    "provider_region": "Filter stored costs by served provider region; omit to include all regions.",
    "agent_run": "Filter by run_<32 lowercase hex> storage ID or otherwise exact raw caller run name, at most 256 characters.",
    "failover_reason": "Filter stored costs by failover reason; omit to include all reasons.",
    "model_fallback": "Filter is_model_fallback; false selects non-fallback calls, null includes both.",
    "provider_fallback": "Filter is_provider_fallback; false selects non-fallback calls, null includes both.",
    "tag": "AND filters as key=value strings; unique keys 1–64 characters, values at most 256, no NUL. Empty values, whitespace and case are preserved.",
    "group_by": "Core grouping dimension (for example model, provider or modality), or tag:<key> with key 1–64 characters and no NUL. Core validates dimensions.",
}
SPECIFIC = {
    "budget_check": {
        "no_record": "True skips denial attribution ingest only; the reservation decision still runs. False records denied calls by default.",
    },
    "budget_check_batch": {
        "operation_id": "Caller-retained canonical UUIDv7. Prepare before submission to recover a wholly lost reply; expires 24 hours after its encoded creation time. Required for recover_only and record_only.",
        "recover_only": "True performs only historical GET using operation_id; omit checks. It never resumes admission, records denial attribution or renews original reservation authority. Read-only key scope is accepted.",
        "record_only": "Explicit attribution-only retry: exact recording_retry entries from the original result, with the same operation_id, original index and event payload. Omit checks; never regenerates timestamps or identity from ambient settings.",
        "checks": "1–32 ordered check bodies for one durable admission operation; omit entirely for recover_only or record_only. Validate before submission; never automatically replay unknown outcomes.",
        "no_record": "True skips denial attribution for every item only; checks still run and allowed items may reserve budget.",
    },
    "budget_confirm": {
        "media_usage": "Actual observed media billing quantities and pricing variants, forwarded unchanged to settlement and attribution. Null quantities are unobservable; zero means none. Never substitute admission estimates after provider completion.",
        "modality": "Actual completed call modality for attribution (text by default); the pricing card selects settlement units from observed media_usage.",
        "reservation_id": "Non-null, non-empty reservation ID returned by check, required for settlement. Confirm only after its call using the same project key identity; omit for record_only.",
        "call_id": "Caller-persisted canonical UUID, at most 36 characters; required for settlement. Keep identical across settlement and recording retries; omit for record_only.",
        "model": "Actual served model, required for settlement, at most 2048 characters; omit for record_only.",
        "provider": "Actual served provider, required for settlement; omit for record_only.",
        "attempt_index": "Observed nonnegative provider-attempt index, 0 by default; retained in attribution, never creates or retries a provider attempt. Keep identical on recording retry.",
        "record_only": "Exact FIRST retained recording_retry event; omit every other argument. After a lost 204, first obtain a known 204 by replaying identical settlement inputs with no_record=true. Never replace the FIRST event with that replay's unused new event. Only ingests the supplied event; reports settlement=not_submitted, not proof of earlier settlement.",
        "is_provider_fallback": "True when the served provider differs through fallback routing; report the actual served provider and model.",
        "no_record": "True skips ingest only; settlement still occurs. After a lost 204, replay identical settlement inputs with no_record=true; after a known 204, use record_only with the FIRST retained event, never this replay's unused new event. Inspect settlement and recording separately.",
    },
    "record_call": {
        "call_id": "Caller-persisted UUIDv4 or UUIDv7, at most 36 characters; reuse for retries with identical payload and attempt_index=0. Omission generates a new ID on each invocation, so cannot deduplicate lost-response retries.",
        "input_tokens": TOKEN_COUNTS["input_tokens"]
        + " If omitted, use token_details.input_tokens or zero; when both are supplied they must match.",
        "output_tokens": TOKEN_COUNTS["output_tokens"]
        + " If omitted, use token_details.output_tokens or zero; when both are supplied they must match.",
        "token_details": "Observed token totals and subset buckets; subsets are not additional tokens. Explicit top-level input/output counts must equal these totals.",
        "media_usage": "Observed media billing quantities and pricing variants; null quantities mean unobservable, zero means none. Used for ingest pricing, not reservation settlement.",
        "modality": "Recorded call modality (text by default); report the actual modality for server-side pricing and analytics.",
    },
    "list_tag_values": {
        "key": "Verbatim tag key, 1–64 characters and no NUL; whitespace and case are significant. Team plan required.",
        "prefix": "Case-sensitive value prefix, at most 256 characters and no NUL; omit for all values of this key.",
    },
    "list_agent_runs": {
        "from_time": "Custom run-selection start: timezone-aware RFC3339 timestamp paired with to_time; overrides time_range. Costs remain lifetime totals.",
        "to_time": "Custom run-selection end: timezone-aware RFC3339 timestamp after from_time; provide both bounds. Costs remain lifetime totals.",
        "query": "Case-insensitive run-name search text, at most 255 characters; omit to include all qualifying runs.",
        "parent": "Parent run storage ID or raw caller run ID, 1–256 characters; selects children of this parent.",
        "sort": "Descending page sort key; defaults to last_seen_at. Must match the cursor's stored sort when continuing; remove cursor when changing sort.",
    },
    "get_agent_run": {
        "run": "Run storage ID or exact raw caller run name, 1–255 characters; ambiguous names fail with guidance to list IDs.",
    },
    "list_alert_events": {
        "account": "True reads account-wide history and requires JWT login; project_id is ignored. False reads the selected project using a project key or JWT.",
        "time_range": "Alert-history preset: 7d, 30d or 90d (default 30d); paired custom timestamps override it. Core may clamp retention.",
        "rule_type": "Filter by alert rule type, at most 32 characters and no NUL; omit to include all types.",
        "scope": "Filter by rule scope, at most 32 characters and no NUL; this is not project/account selection (use account).",
        "failed_only": "True keeps only partial/failed delivery rollups using a local filter; a page can be empty while next_cursor remains present.",
        "all_pages": "True fetches at most 10 history pages within one 30-second/1 MiB response budget, counting empty filtered pages. A fixed limit error retains the cursor of the first unread page. False returns one page and its continuation cursor.",
    },
}


def parameter_descriptions(name: str, names: list[str]) -> dict[str, str]:
    descriptions = (
        COMMON
        | (COST_FILTERS if name in {"get_costs", "get_cost_total"} else {})
        | SPECIFIC.get(name, {})
    )
    return {field: descriptions[field] for field in names}


def _described_model(
    model: type[BaseModel],
    descriptions: dict[str, str],
    *,
    annotations: dict[str, object] | None = None,
) -> type[BaseModel]:
    """Retain wire validation/defaults; add input-only explanatory metadata."""
    if set(descriptions) != set(model.model_fields):
        raise ValueError("Nested input descriptions must cover every field")
    fields = {}
    for name, original in model.model_fields.items():
        field = copy(original)
        field.description = descriptions[name]
        fields[name] = ((annotations or {}).get(name, field.annotation), field)
    return create_model(model.__name__, __base__=model, **fields)


TokenDetails = _described_model(
    wire.TokenDetails, TOKEN_COUNTS | {"is_estimated": COMMON["is_estimated"]}
)
MediaUsage = _described_model(
    wire.MediaUsage,
    {
        name: COMMON[name].replace("Estimated", "Billed")
        for name in wire.MediaUsage.model_fields
    },
)
BudgetCheckRequest = _described_model(
    wire.BudgetCheckRequest,
    {
        **{
            name: COMMON[name]
            for name in wire.BudgetCheckRequest.model_fields
            if name in COMMON
        },
        "uncounted_calls": "Advisory count of outage fail-open calls since the last successful contact; default zero because the CLI admits no offline calls. Never affects budget pricing.",
        "uncounted_tokens": "Advisory tokens consumed by those outage calls; default zero because the CLI admits no offline calls. Must be between zero and 9223372036854775807.",
        "estimated_media": "Estimated media billing quantities and pricing variants for admission; null uses token-based estimation. Null quantities are unknown, zero means none.",
        "tags": "Attribution tags replace SOLWYN_TAGS for this item when supplied (including null); omission uses ambient tags. At most 10 keys, keys 1–64 characters, values at most 256, no NUL; preserve whitespace/case.",
        "agent_run_id": "Stable caller run ID, at most 256 characters; omission uses SOLWYN_AGENT_RUN_ID, explicit null disables that fallback. Never replace an ID to evade a stopped run.",
        "price_hints_version": "Optional input version, only '1' or null accepted; after validation the CLI always sends '1' to request pricing hints.",
        "run_directive_version": "Optional input version, only '1' or null accepted; after validation the CLI sends '1' when agent_run_id is non-null, otherwise null. Honor returned stop directives.",
    },
    annotations={"estimated_media": MediaUsage | None},
)


_DenialEvent = _described_model(
    wire.IngestMetadataEvent,
    {
        "model": "Original attempted model from the checked item, at most 2048 characters; preserve exactly.",
        "provider": "Original attempted provider from the checked item; preserve exactly.",
        "input_tokens": "Original estimated input-token count for the denied candidate; no provider call took place.",
        "output_tokens": "Original output-token count, zero for these denied candidates; preserve exactly.",
        "latency_ms": "Original latency in milliseconds, zero for these denied candidates; preserve exactly.",
        "status": "Original budget_denied status; recording retry never changes the decision.",
        "is_model_fallback": "Original fallback attribution flag, false for these denied candidates; preserve exactly.",
        "call_id": "Original UUIDv5 derived from operation_id and the check index; preserve exactly for deduplication.",
        "sdk_instance_id": "Original batch operation UUIDv7; preserve exactly on every recording retry.",
        "timestamp": "Original timezone-aware checked_at timestamp from the journal; never regenerate on retry.",
        "modality": "Original event modality; preserve the returned event without selecting a new modality.",
        "token_details": "Original token detail buckets, if present; preserve without changing estimated or observed semantics.",
        "media_usage": "Original estimated media billing quantities and pricing variants from the denied candidate; no provider usage was observed.",
        "service_tier": "Original service-tier selector, if present; preserve exactly without supplying a new default.",
        "provider_region": "Original provider-region selector, if present; preserve exactly without supplying a new default.",
        "agent_run_id": "Original caller run ID, if present; no ambient SOLWYN_AGENT_RUN_ID fallback is applied.",
        "parent_agent_run_id": "Original parent caller run ID, if present; preserve exactly.",
        "agent_run_name": "Original human-readable run name, if present; preserve exactly.",
        "tags": "Exact original tag mapping, including null; no ambient SOLWYN_TAGS merge is performed on retry.",
        "deny_source": "Original denial source, server for these batch events; preserve exactly.",
        "deny_reason": "Original denial reason from the completed decision; preserve exactly.",
        "denied_by_period": "Original budget period or stopped-run reason; preserve exactly.",
        "estimated_output_bound": "Original optional estimated output-token bound for denial attribution; preserve exactly.",
        "attempt_index": "Original primary attempt index, fixed at 0; no routing retry is created.",
        "is_provider_fallback": "Original provider fallback flag, fixed false for this attribution-only CLI path.",
    },
    annotations={
        "token_details": TokenDetails | None,
        "media_usage": MediaUsage | None,
    },
)
DenialRecordingRetry = _described_model(
    _DenialRecordingRetry,
    {
        "index": "Original check index, 0–31, retained with its exact denial event.",
        "event": "Exact recording_retry event returned by the original admission; preserve all fields, especially identity and timestamp. Never rebuild from ambient tags or run defaults.",
    },
    annotations={"event": _DenialEvent},
)

ConfirmationRecordingRetry = _described_model(
    wire.IngestMetadataEvent,
    {
        **{
            name: "Original value; preserve exactly. " + COMMON[name]
            for name in wire.IngestMetadataEvent.model_fields
            if name in COMMON
        },
        "call_id": "Original canonical UUID of the completed call; preserve exactly for deduplication.",
        "sdk_instance_id": "Original emitting SDK instance identity; never regenerate to bypass a duplicate acknowledgement.",
        "timestamp": "Original timezone-aware event timestamp; preserve exactly, including on a lost ingest acknowledgement.",
        "is_model_fallback": "Original caller-reported model fallback flag; reporting it never invokes a provider.",
        "modality": "Original observed call modality; preserve without selecting a new default.",
        "token_details": "Original observed token totals and subsets; top-level totals must match. Preserve is_estimated and all subsets.",
        "media_usage": "Original observed media quantities, is_estimated and pricing variants; null means unobservable, zero means none. Never replace with reservation estimates.",
        "tags": "Exact original tags, including null; no ambient SOLWYN_TAGS merge occurs on retry.",
        "agent_run_id": "Original caller run ID, including null; no ambient SOLWYN_AGENT_RUN_ID fallback occurs on retry.",
        "deny_source": "Original denial metadata, normally null for a completed success/error event; preserve exactly.",
        "deny_reason": "Original denial reason, normally null for completed provider calls; preserve exactly.",
        "denied_by_period": "Original denied budget period, normally null for completed provider calls; preserve exactly.",
        "estimated_output_bound": "Original optional output bound, normally null; never substitute it for observed output usage.",
        "attempt_index": "Original nonnegative provider-attempt index; preserve exactly for the project/call/attempt deduplication key.",
        "is_provider_fallback": "Original caller-reported served-provider fallback flag; preserve exactly without performing routing.",
    },
    annotations={
        "token_details": TokenDetails | None,
        "media_usage": MediaUsage | None,
    },
)
