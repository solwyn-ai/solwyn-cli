# Bounded MCP work and batch recovery

Every MCP tool call has one fixed 30-second useful-work deadline, covering input
validation, worker admission, fresh credentials, HTTP retries, pages, attribution
and result construction. No tool argument changes this deadline. Each invocation
also has these limits:

| Resource | Limit and behavior |
| --- | --- |
| Admission batch | 1–32 checks in one POST; all checks validate before authentication or HTTP. |
| Fetched pages | 10, counting empty/filtered pages and an entitlement fallback. |
| HTTP response bodies | 1 MiB in aggregate across every status, retry and refresh, streamed before JSON parsing. |
| Encoded HTTP request body | 4 MiB per request; an oversized body is rejected before sending, never shortened. |
| Complete MCP result | 2 MiB, including text, structured content, UTF-8/JSON escaping and reserved SDK metadata space. |
| Continuation cursor | 4,096 characters; an oversized cursor is rejected. |

Slow headers and continuously arriving body chunks consume the same deadline.
The client requests identity encoding and rejects unexpected compressed bodies.
The request cap accommodates a measured 32-item fixture with maximum-length
four-byte Unicode model/fallback strings, tags and run IDs; unbounded numeric
input or larger encoded requests still meet the explicit body cap.

Cancellation and EOF stop subsequent requests, retries, pages and recording.
An already admitted HTTP request may still be effective. The async HTTP owner
allows up to 250 ms for bounded, shielded stream/pool cleanup. A worker blocked in
an operating-system/keychain call cannot be safely killed by Python: it may finish
local work after cancellation and can delay process exit. If JWT refresh already
returned a new token pair, refresh-then-access persistence finishes before the
next HTTP attempt is blocked. A host's eventual process termination is a separate
policy; neither cancellation nor termination rolls back a reservation or ingest.

A bounded read error can retain complete earlier pages in `data` plus the exact
first unread cursor in `recovery.cursor`. If an entire result exceeds the wire
cap, `data` is omitted and the original cursor is retained, so restarting does
not skip rows that were never delivered. Run-name resolution never declares a
unique match after a truncated or repeated-cursor search; use an exact storage
run ID to avoid that traversal. Normal standalone CLI pagination/export behavior
has no MCP deadline or page cap.

## Submit once with an identity retained by the caller

`budget_check_batch` uses Core's `/api/v1/budgets/check/batches` journal. Deploy the
Core batch migration and endpoints before adopting this tool version. There is
no fallback to the ambiguous legacy endpoint. The standalone
`solwyn budget check --batch` command retains its existing legacy chunk behavior.

Prepare and persist a **fresh UUIDv7** in the harness before sending:

```json
{
  "operation_id": "<caller-retained UUIDv7>",
  "checks": [
    {"model": "gpt-4o", "provider": "openai", "estimated_input_tokens": 100}
  ],
  "no_record": false
}
```

If omitted, the tool generates a UUIDv7 before the HTTP attempt and returns it in
all deliverable outcomes. A generated identity cannot help when the entire MCP
reply is lost; caller-retained identity is necessary for that case. The journal
identity is separate from the MCP request ID and from a provider call ID.

Submission performs one POST with both automatic auth and rate-limit replay
disabled. Core permits only the claim winner to execute. Reusing the same ID and
normalized ordered payload returns historical state; a changed payload gives
409. The client never automatically resubmits admission or chooses a new ID.

The typed `data` retains `operation_id`, `status`, `expires_at`, indexed `items`
and the prior ordered `results` application field:

- `completed` contains a validated decision, including valid denial or fail-open.
- `failed` means Core proved no reservation effect for that attempted item.
- `unknown` means a possible effect cannot be resolved from the journal.
- `unattempted` means Core has not begun the item; lookup never starts it.

Core may still be running an already started item. Do not automatically run an
unattempted suffix or repeat an unknown check. Preserve every completed
reservation ID. Historical decisions retain the original `checked_at`; a
reservation's conservative authority ends at `reservation_expires_at = checked_at
+ 300 seconds`. Reading a historical allowed result does not renew permission to
spend. Skip confirmation for a null reservation, and never call a provider for a
denied or unresolved item.

## Recover using GET only

```json
{"operation_id": "<same retained UUIDv7>", "recover_only": true}
```

Omit `checks`. Recovery neither reconstructs ambient tags/run settings nor starts
admission, resumes a suffix, or records attribution. It uses the authenticated
key's project; a same-project rotated key works, and read-only key scope is
accepted. A key for another project receives 404. The mixed writer tool remains
excluded from the `read` profile even for this mode.

UUIDv7 time fixes the journal's 24-hour lifetime. Expired identities give 410 even
after physical cleanup; future identities and payload conflicts are rejected.
Do not replace an expired ID to replay work whose effects are unresolved. The
journal is bounded historical evidence, not an exactly-once provider executor.

## Retry attribution separately

Submission records valid denials by default; `no_record` skips only this ingest.
Every prepared denial includes a `recording_retry` entry containing its original
index and exact event. Its call ID is UUIDv5(operation UUID, `denial:<index>`), its
SDK instance ID is the operation UUID, its attempt is 0, and its timestamp is the
original `checked_at`. Retain that payload with the operation ID before retrying:

```json
{
  "operation_id": "<same retained UUIDv7>",
  "record_only": ["<exact recording_retry object from the original result>"]
}
```

The placeholders above must be replaced with the actual UUID and JSON object.
`record_only` sends only the supplied exact event to ingest. Omit `checks`, do not
combine it with `recover_only`, and never regenerate its timestamp, tags, run
settings or identity. The Core journal contains admission only: GET cannot
reconstruct an attribution receipt or prove an event was stored. If the original
recording payload was wholly lost, preserve the admission state and investigate
attribution separately; do not reserve or invoke the provider again.

Inspect `recorded`/`recordings` separately from admission and settlement.
`ingested` or a call-ID `deduplicated` disposition acknowledges that event;
`rejected` describes a known rejection. Lost/invalid ingest replies and
`legacy_key` duplicate acknowledgements remain **`unknown`** for MCP.
Core's legacy timestamp/SDK-instance duplicate lane can win even on an identical
redelivery; it can also refer to a different logical call. An echoed call ID in
that lane does not prove the requested call has a durable ledger entry. An
explicit record-only retry reports an execution error while retaining this
unknown disposition. Never alter the payload or IDs to force a positive receipt.

When confirmation already succeeded, `recovery.settlement=confirmed` and the
known reservation/call identity survive an attribution timeout or failure.
Retry attribution with its existing identity; do not reserve again for completed
provider work. Compact failure recovery always preserves known identities and
keeps admission, settlement and recording uncertainty separate.
