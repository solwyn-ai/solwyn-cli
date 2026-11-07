# Caller-owned spend reference

The contract details for attributing, batching, and governing spend the CLI handles
on the caller's behalf. The [README](../README.md) covers the common case; this page
holds the full rules for `record`, `budget check --batch`, and budget rules.

## Recording caller-owned spend with `record`

`budget confirm` records attribution after successful settlement by default.
`--no-record` skips attribution; a recording failure warns without undoing settlement.
Use `record` for completed calls that have no reservation, including local providers:

```sh
export SOLWYN_AGENT_RUN_ID="job-42"
export SOLWYN_TAGS="environment=prod,team=ops"
solwyn record --provider ollama --model llama3 --input-tokens 120 --output-tokens 30
solwyn --json record --batch events.json
solwyn --project YOUR_PROJECT_ID tags list
```

Reuse the same canonical lowercase `--call-id` UUID for retries of one logical event
(generate one with `uuidgen | tr A-F a-f`). `record --batch FILE|-` takes a nonempty
JSON array of event inputs and chunks it into at most 1000 events per request. Each
invocation uses a fresh `sdk_instance_id` across all its chunks. Never send `cost_usd`
or `lease_id`; Core computes prices. Inspect `rejected` and `duplicates`: call-ID
replays succeed, while rejections and legacy timestamp collisions exit 1. On an uncertain
or partial write, retain the returned `call_ids` in input order before retrying; the
result has `status=failed` and preserves the dispositions already received.

Core's explicitly unaccepted ingest capacity responses use one of three fixed
`detail` strings with `Retry-After: 1`: `Budget admission is busy; retry shortly`,
`Account tag registry is busy; retry the batch.`, or
`Accounting maintenance is in progress; retry the batch.` These prove that
individual attempt recorded nothing. The client retries ingest once when any
`Retry-After` is present, sharing the existing 429 retry budget and 10-second
wait cap; this replay policy does not itself prove absence.
If a recognized rejection remains, `record` exits 1 with a retry hint and
`status=not_recorded` only when no earlier chunk completed and every attempt
for the failed chunk is provably nonaccepting. Recognized rejections and Core's
pre-ingest 429/401 responses meet that boundary. An earlier potentially accepted
attempt stays uncertain even if the final retry is a recognized rejection;
no later attempt can erase that uncertainty or replenish the shared retry allowance.
A valid 202 with complete canonical dispositions resolves the call normally.
A partial multi-chunk invocation retains `status=failed` and all known
dispositions; the recognized
rejection proves only the failed chunk was unaccepted if its earlier attempts
also meet that boundary. A generic/proxy 503, a missing or different header,
or a transport failure remains uncertain:
CLI retains `status=failed` and MCP reports `recording=unknown`. Always retry
attribution with the original `call_ids` and attempt indices.

Budget and quota periods use Core's server receive time (`received_at`), while
cost analytics use the stored event `timestamp`. A backdated `record --batch`
event counts against the budget and quota period in which Core receives it,
but appears on its original day in `costs` if that timestamp is within Core's
accepted clock bounds. Core clamps excessive future skew and, when a past
horizon is configured, older timestamps; retention can also hide old analytics.
Backdating records does not move spend out of the current budget period.

Repeat `--tag key=value` to override environment tags. Keys and values preserve
whitespace and case; empty values and equals signs in values are legal, commas in
flag values are not. Tags allow at most 10 keys, keys of 1–64 characters, and values
up to 256 characters, with no NUL. Reusing `SOLWYN_AGENT_RUN_ID` groups a job's calls;
otherwise Core creates automatic runs from each invocation's ID.

Denied budget checks also record receipts unless `--no-record` is set. Supply
`--estimated-output-tokens` to attribute the upper savings bound. Tag submission
and enforcement work on every tier; discovery is account-scoped and requires Team.
`tags values KEY --prefix Q` pages through values; retiring a key frees an active
key slot, and later successful ingest may reactivate it.

## Batch budget checks

`solwyn budget check --batch checks.json --json` reads a nonempty JSON array of
check bodies; use `--batch -` for stdin. For example:

```json
[{"model":"gpt-4o","provider":"openai","estimated_input_tokens":1200,"agent_run_id":"job-42","tags":{"environment":"prod"}}]
```

All bodies validate before submission. Requests contain at most 32 checks, preserve
input order, and count every item against `checks_per_minute`. Each allowed item
normally holds its own reservation: confirm it after its provider call. A fail-open
item has a null reservation and must not be confirmed. Do not poll or repeat checks
to refresh reservations. Denied items record one receipt each unless `--no-record`
is set, sharing a fresh invocation ID. `--estimated-output-tokens` applies the same
savings upper bound to every denial. Put all other per-check options in the JSON;
missing tags and run IDs inherit `SOLWYN_TAGS` and `SOLWYN_AGENT_RUN_ID`. Explicit
nulls suppress these defaults. A run ID opts into termination directives.

The `solwyn.budget.check.batch.v1` payload has index-aligned `results`: each contains
either a nullable `result`, or `error_code` and `error_message`. Unknown models are
per-item errors. All allowed exits 0, any denial exits 4, and errors without a denial
exit 1. A request-level 404 or 503 fails the chunk. If an earlier chunk completed,
its results and reservation IDs remain in output, alongside `failed_chunk_start`
and `total_checks`; later chunks were not submitted. Every legacy batch 503
leaves reservation state within the failed chunk unknown, including
`Budget admission is busy; retry shortly` with `Retry-After: 1`. Core uses that
same response both before admission and after the legacy loop has committed
earlier item reservations, so the response cannot distinguish those cases.
This uncertainty applies even to the first chunk. Do not replay completed
checks, assume rollback, or blindly resubmit the failed chunk. The CLI does not
automatically replay budget checks.

For single `/budgets/check` and durable `/budgets/check/batches`, that fixed
busy detail proves no reservation was made by the request; retrying admission
in a moment is safe. This is a reservation guarantee: single checks can already
have folded advisory `uncounted_calls`/`uncounted_tokens` before lock contention.
A retry can overcount this advisory upper-bound telemetry. Preserve caller
values; the hint does not promise an absence of all side effects.
The MCP `budget_check_batch` tool submits at most 32 checks to Core’s durable
journal with a caller-retained UUIDv7. Recovery uses GET only; explicit denial
attribution retry preserves its exact event. See [bounded MCP work and recovery](mcp-bounded-work.md)
for limits, required Core rollout, indexed outcomes and recording uncertainty.


## Budget rules and per-run caps

List rule conditions, routing, and separate pause/snooze state:

```sh
solwyn --project proj_example budget rules list
solwyn --json --project proj_example budget rules list
```

Rule mutations require a user login with owner/admin permissions. Each rule may
reference notification channels with repeatable `--channel ID` options:

```sh
solwyn budget rule add --type budget_threshold --percent 80
solwyn budget rule add --type runaway_run --spend 5 --mode hard_deny
solwyn budget rule add --type scoped_budget --scope tag --match env --value prod --limit 25 --mode hard_deny
solwyn budget rule add --type forecast_overrun --spend 200
solwyn budget rule add --type daily_anomaly --multiplier 2 --lookback 7
solwyn budget rule add --type unit_cost_anomaly --multiplier 3
```

Runaway `--calls N` and `--window MIN` must be supplied together. Scoped caps also
accept `--scope model|provider|agent_run`; `--value` is valid only for tag scope.
The rule count limit includes all six types. Adding a rule preserves the complete
budget configuration and re-reads it before writing; `budget set` does the same.
A write that needs a browser approval can wait for minutes, so after the approval
the configuration is read once more, and the approved write is sent only if it is
unchanged; otherwise the command exits 6 with `budget_changed_during_approval`
and nothing is sent. Core has no concurrency token, so this detects changes before
the final write but cannot eliminate the remaining milliseconds-long race. A 503 after a write triggers a read to confirm the complete configuration;
an unconfirmed outcome requires reviewing the budget before retrying.

```sh
solwyn budget rule pause RULE_KEY
solwyn budget rule resume RULE_KEY
solwyn budget rule snooze RULE_KEY --until +2h
solwyn budget rule unsnooze RULE_KEY
solwyn budget rule remove RULE_KEY
solwyn budget threshold snooze 80 --until 2026-12-01T12:00:00Z
solwyn budget threshold unsnooze 80
solwyn budget restore-defaults
```

Snooze accepts timezone-aware ISO datetimes or `+30m`, `+2h`, and `+1d` durations,
which the CLI converts to absolute timestamps. **A snoozed hard-deny rule still
denies; pause releases enforcement.** Rule keys are content-addressed: editing
keyed condition fields changes the key. Restore-defaults preserves non-threshold
rules, and the CLI re-reads and prints the full rule state afterward.

Preview routing with an API key or user login, including an unsaved condition:

```sh
solwyn budget rule preview
solwyn budget rule preview RULE_KEY --channel CHANNEL_ID
solwyn budget rule preview --type runaway_run --spend 5 --mode hard_deny
```

During interactive project creation, `solwyn init --run-cap 5` adds a wildcard
`runaway_run` rule with `hard_deny` immediately after creation. It caps every run at
`min(caps)` when run identity is supplied; scoped run caps and wildcard caps share
the strictest limit. For an existing project, use `budget rule add` instead.
The default MCP surface includes the pure-read `list_budget_rules` tool; rule
mutations remain CLI operations.
