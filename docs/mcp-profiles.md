# MCP profiles and tool contracts

Launch a fixed read surface with an installed executable:

```sh
/absolute/path/to/solwyn --project proj_your_project mcp --profile read
```

Global `--project` and `--api-url` options precede `mcp`. Its `--profile`
option follows `mcp`. Credentials belong in the existing keychain workflow or
the subprocess's `SOLWYN_API_KEY` environment, never in argv or repository JSON.

`read` exposes 13 tools. `guardrails` exposes all 17 and remains the default for
compatibility with `solwyn mcp`. The choice stays fixed for the process lifetime.
Excluded names fail both discovery and invocation: calling one returns the fixed
MCP protocol error `-32602`, `Unknown tool`, before argument validation, credential
resolution or HTTP. A tool argument cannot switch profiles.

| Tools | Profile | Credential requirement |
| --- | --- | --- |
| `budget_status`, `get_project_status`, `get_forecast`, `get_costs`, `get_cost_total`, `list_budget_rules`, `list_tags`, `list_tag_values`, `list_agent_runs`, `get_agent_run` | Both | Project API key or JWT with access to the selected project |
| `list_alert_events` | Both | Project key or JWT for project history; `account=true` requires JWT and ignores `project_id` |
| `list_projects`, `get_overview` | Both | JWT login |
| `budget_check`, `budget_confirm`, `record_call` | `guardrails` | Write-scoped project API key |
| `budget_check_batch` | `guardrails` | Project key; write scope for submit/record-only, read scope accepted for recovery-only |

Tag discovery requires the Team plan. Core still checks project access, plan
entitlements and key scope. A profile only reduces the exposed tool surface; it
never grants permission to spend. Writes use the project authenticated by the
key. `project_id` on read tools overrides launch `--project`, then
`SOLWYN_PROJECT_ID` and `default_project`; it cannot redirect writes to another
project. Credential resolution remains environment project key, keychain project
key, then keychain JWT, freshly evaluated for every operation. A stored key can
therefore mask a JWT needed for account tools.

Discovery includes human titles, behavioral annotations, described input fields
and typed output schemas. All 13 reads have `readOnlyHint=true`; the four writes
have `readOnlyHint=false`, `destructiveHint=true` and `idempotentHint=false`.
The conservative write hints cover budget/accounting changes and composite
operations; they do not claim that every retry duplicates an effect. Reads use
`destructiveHint=false` and `idempotentHint=true` (read values may still change).
All tools have `openWorldHint=true` because calls contact Core. Hints describe
behavior and do not enforce authorization.

Local diagnostics can import `get_tool_catalog(profile="read")` from
`solwyn_cli.mcp.server`. It returns an ordered tuple of fresh `ToolDeclaration`
objects with the actual published definition, runtime models,
`credential_requirement` (`jwt`, `project_key` or `either`) and `credential_note`.
It performs no auth or HTTP. Use the note for conditional account-alert access;
the metadata is not an authorization check or proof that credentials work.

## Cache discovery, evaluate every call

Current MCP `2026-07-28` tool lists carry `ttlMs=60000` and
`cacheScope="private"`. Hosts may reuse this fixed discovery for one minute
within their private client context; keep different launch configurations and
profiles distinct. Legacy clients receive the same applicable tool definitions
without the new cache fields. No change subscriptions are advertised.

Solwyn sets no cache hints on `tools/call` results. Catalog freshness never
extends a budget decision or settlement receipt, triggers polling, caches
credentials, or permits reusing a reservation. Tests exercise the actual SDK2
discovery cache while rotating a synthetic key and then removing it; each call
resolves credentials again.

The compact UTF-8 tool-array regression budgets are 152 KiB for `guardrails` and
72 KiB for `read`, including output schemas. They allow measured growth while
catching accidental schema/catalog duplication. This measures wire bytes, not
tokens sent by a host to its model. Hosts can keep output schemas for validation
without including them in model context.

## Decisions, settlement and recording

Check both the MCP `isError` flag and the structured result. A valid denial is
`ok=true` with `allowed=false`: do not invoke the provider. Execution failures
have `isError=true`; partial failures retain known data. Fail-open with
`allowed=true` and `reservation_id=null` means skip confirmation. If attribution
is desired after that unreserved call, record it once.

The caller owns the provider boundary. Check only immediately before a
caller-owned uninstrumented call; never wrap an SDK-instrumented call with an
additional MCP check/confirm or duplicate its attribution event. Keep a stable
run ID and honor stop directives. Never automatically replay an ambiguous
check: each invocation can create another reservation. The MCP batch consumer submits at most 32 checks to the durable Core journal with
a stable UUIDv7 operation ID. `recover_only` performs GET without admission or
recording; it never renews a historical reservation. See [bounded work and batch
recovery](mcp-bounded-work.md) for limits, indexed outcomes and explicit attribution retry.

Confirm a non-null reservation after its provider call using actual served
model, provider, region/tier and observed token counts. Token subset buckets are
already contained in the corresponding input/output totals; avoid counting them
again. Pass actual observed `media_usage` for unit-priced calls, with the actual
`modality`. Confirmation and attribution receive the same quantities, estimate
marker, resolution and quality. Null means unobservable; observed zero means no
billed units. See [media settlement and recovery](media-confirmation.md).

Core's project-bound confirmation receipt lasts 3,600 seconds. After a lost 204,
save the **first** returned `recording_retry` and original confirmation inputs.
Within the receipt window, replay the identical settlement inputs with
`no_record=true`; this sends no ingest. After a known 204, submit only
`record_only` with that **first** event. The settlement-only replay prepares an
unused new event: never replace the first event with the replay's
`recording_retry`. Ordinary fresh-confirm replay generates a new timestamp/SDK
instance and rereads ambient run/tags, so it is not exact-event recovery.

The receipt does not compare payloads, correct earlier settlement or reconstruct
an attribution event. Expiry, receipt loss or an unresolved outcome needs
explicit state inspection. Do not make a new reservation for completed work.

Confirmation and attribution are separate API operations. `no_record=true`
skips ingest, not settlement (on checks it skips denial attribution, not
admission). Inspect `settlement` and `recorded` independently. Once settlement is
known successful, recover a failed attribution using `budget_confirm` with only
`record_only` set to the **first** retained `recording_retry` event. This preserves every
event field and performs only ingest. It reports `settlement=not_submitted`
because a caller-supplied event is not proof of settlement; keep the original
confirmed result separately. Do not reserve again.

This recipe requires the first exact event to have been returned. Ordinary CLI
transport errors expose only a generic stderr error, with no prepared event;
original flags cannot reconstruct its timestamp/SDK instance. A wholly lost MCP
reply or a compact result that omits the event has the same unseen-event limit.

Generate and persist `call_id` (UUIDv4 or UUIDv7) before sending. Confirmation
accepts the caller's observed `attempt_index` (zero by default) and
`is_provider_fallback`; it never performs routing or another provider attempt.
Core deduplicates `(project_id, call_id, attempt_index)`
while the identity is retained; a duplicate is an acknowledged no-op, not an
update. Inspect ingested, rejected and duplicate result lanes. Omitting
`record_call.call_id` generates a new ID per invocation and cannot protect a
lost-response retry. Preserve settlement and recording state separately.

MCP recording stays `unknown` after a lost/invalid ingest reply or a `legacy_key`
acknowledgement: that duplicate lane alone cannot prove the intended call-ID ledger
entry. Preserve the exact event and inspect attribution separately. An admission
journal or settlement receipt does not establish recording durability.
