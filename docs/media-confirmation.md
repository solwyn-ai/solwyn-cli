# Observed media settlement and recording recovery

Use this workflow only for caller-managed provider calls. The Solwyn SDK already
owns its instrumented calls. Never add a second reservation or attribution event
for the same SDK-managed work.

Immediately before provider spend, run a budget check with estimates. After the
provider finishes, confirm its non-null reservation with **observed** usage and
the actual served model/provider. Unit-priced calls need the observed quantity
selected by Core's pricing card. Token-priced calls use their token breakdown;
image/audio tokens are subsets of token totals, not image counts or seconds.

```sh
solwyn --json budget confirm \
  --reservation-id "$RESERVATION_ID" --call-id "$CALL_ID" \
  --provider openai --model whisper-1 --modality audio --audio-seconds 120
```

Equivalent MCP arguments:

```json
{
  "reservation_id": "<retained reservation>",
  "call_id": "<caller-persisted canonical UUID>",
  "provider": "openai",
  "model": "whisper-1",
  "modality": "audio",
  "media_usage": {"audio_seconds": 120, "is_estimated": false}
}
```

CLI media flags are `--image-count`, `--generation-count`, `--video-seconds`,
`--audio-seconds`, `--input-characters`, `--resolution` and `--quality`, matching
`record`. The CLI's `--is-estimated` marks both supplied media and token usage.
MCP's `media_usage.is_estimated` marks media separately from the token
`is_estimated` input. Omitted/null quantities stay unobservable; explicit zero
stays zero. Never fill missing observations with the original reservation's
estimate. Core validates pricing variants; a selector's presence does not mean
that a particular pricing card uses it.

Keep `provider_region`, `service_tier`, token subsets, `is_provider_fallback`
and `attempt_index` faithful to the provider observation. The attempt index is
attribution metadata; changing it creates a different deduplication identity.
These inputs never route a request or invoke a provider.

## Separate settlement from attribution

Confirmation first settles Core's budget counter, then ingests an attribution
event. The response has `settlement` and `recorded` states, plus `recording_retry`
containing the original event. `no_record=true` / `--no-record` skips ingest.
When the reservation is null (fail-open/unreserved), skip confirmation; use
ordinary recording if attribution is wanted.

After a **known successful settlement** and failed/unknown attribution, retain
the original result and send only its exact **first** `recording_retry` event:

```json
{"record_only": "<the complete FIRST recording_retry JSON object, not this string>"}
```

For the CLI, save that object as `recording-retry.json` and run:

```sh
solwyn --json budget confirm --record-only recording-retry.json
```

Record-only rejects other confirmation arguments. It never posts a confirmation
or budget check, and it does not apply ambient run/tag defaults. It preserves
the original timestamp, SDK instance, call/attempt identity, observations and
attribution. Its `settlement=not_submitted` describes this invocation: a
caller-supplied event is not an authenticated settlement receipt. Keep the
earlier known settlement state separately.

An exact retry can receive Core's `legacy_key` duplicate acknowledgement because
the timestamp/SDK-instance uniqueness check can run before the call-ID ledger
check. This remains `unknown` in MCP (`failed` in the ordinary CLI), even when
there is one correct durable event. The same acknowledgement can also represent
another call's collision. Never change timestamp, SDK instance or call identity
to manufacture a positive acknowledgement. Inspect recording separately.

## Lost replies, expiry and corrections

Core retains a project-bound Redis settlement receipt for 3,600 seconds. After
a lost complete 204, settlement is potentially applied. If the MCP error result
returns `recording_retry`, recover in this order:

1. Save the **first** exact event and the original confirmation inputs separately.
2. Within the receipt window, replay the identical settlement inputs with
   `no_record=true`. This sends the same confirmation HTTP body and no ingest.
3. After a known 204 (`settlement=confirmed`), call `budget_confirm` with only
   `record_only` set to that **first** event. Keep the known settlement result
   separately; record-only reports `settlement=not_submitted`.

The settlement-only replay prepares an unused new event. **Never replace the
first retained event with the replay's `recording_retry`.** Ordinary fresh-confirm
replay without `no_record=true` generates a new timestamp and SDK instance,
rereads ambient run/tags and ingests that replacement; it is not exact-event
recovery. For a first event already retained in a file, the corresponding CLI
sequence uses the original settlement flags plus `--no-record`, then
`--record-only recording-retry.json` only after known confirmation success.

The receipt does not compare payloads; it cannot correct a successful settlement
or reconstruct an attribution event. Another project is rejected, and a
read-only key cannot confirm or ingest. After reservation/receipt expiry or
loss, inspect the state; never reserve again for completed provider work.

An ordinary CLI transport error after a lost 204 returns a generic error on
stderr, without the prepared event or MCP recovery data. Original command flags
cannot reconstruct that unseen timestamp or SDK instance. The sequence above
requires the first exact event to have been received and saved; it cannot be
performed from that CLI error alone.

Missing required media quantities produce `unsupported_modality` (422). Core
releases the estimate and retains the reservation for its existing correction
window. Supply corrected observations with the **same** funding/call identity;
that rejected confirmation wrote no success receipt and did not run automatic
ingest. A previously accepted unpriced ingest event is different: resubmitting
changed quantities under its same event identity does not update its cost.

Cancellation and work limits cannot undo already transmitted effects. Full MCP
error data retains the prepared event when it fits, and compact recovery keeps
known identities and separate states. A response-size limit may omit the full
event. A wholly lost MCP result likewise cannot recover an event the caller
never received, and there is no persistent local event journal. Persist original
observations and identities before the operation; do not assume transport loss
means the provider, settlement or ingest never happened.

Deploy the Core observed-media settlement change before using these inputs.
The vendored OpenAPI snapshot records the exact verified Core commit. The local
cross-repository tests use real Core pricing, PostgreSQL and Redis with synthetic
observations; they do not exercise providers or production infrastructure.
