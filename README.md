# Solwyn CLI

Work with Solwyn from the command line: enforce and attribute LLM spend for calls the Solwyn SDK does not instrument, inspect budgets, costs, and provider health, and recover from leaked or expired project keys — from your terminal, your scripts, or your coding agent.

> **Status:** pre-release. This repository is private while the first release is prepared, and the package is not yet published to PyPI.

## Installation

Until the first release, install from a local checkout:

```console
uv tool install .
solwyn --version
```

Requires Python 3.11 or newer. The package installs two identical executables: `solwyn` and `solwyn-cli` (an alias matching the package name).

## Getting started

```console
solwyn login          # authenticate; use --with-key to store a project key instead
solwyn init           # configure Solwyn for the current repository
solwyn status         # project budget and provider health at a glance
```

`solwyn init` inspects the repository, writes local configuration, and renders SDK snippets for the providers it detects; add `--agents` to also write guidance files for Claude, Cursor, and `AGENTS.md`.

## Commands

| Command | What it does |
| --- | --- |
| `login` / `logout` / `whoami` | Manage credentials and show the current identity and project context. |
| `init` | Configure Solwyn safely for the current repository. |
| `status` | Show project budget and provider health. |
| `costs` | Show project costs across server-advertised dimensions, including agent runs and tags. |
| `record` | Attribute one completed call, or a JSON batch, without a reservation. |
| `tags list`, `tags values` | Discover account-scoped keys and values (Team+). |
| `tags retire` | Retire a registered account tag key (JWT admin/owner). |
| `budget status` | Read current budget status without authorizing spend. |
| `budget check` / `budget confirm` | Pre-flight guardrail loop for un-instrumented provider calls (below). |
| `budget set` | Update selected budget settings while preserving all other configuration. |
| `runs list` / `show` / `stop` | Inspect agent runs and stop their future checks and lease renewals. |
| `projects create` / `list` / `show` | Manage projects; `create --scope` mints the first key with an explicit scope. |
| `keys show` / `rotate` / `revoke` | Inspect key metadata (including its scope), rotate with `--scope` to change it, or hard-revoke. |
| `mcp` | Serve Solwyn tools to agents over local MCP stdio. |
| `agent-context` | Print safe Solwyn budget guidance for coding agents. |
| `config` | Inspect and update local configuration. |

Every command's `--help` states its exit codes, authentication requirements, and JSON output schema.

## Budget guardrails for un-instrumented calls

Calls made through the Solwyn SDK are enforced and attributed automatically. For everything else — a `curl`, another language, a one-off script — the CLI provides an explicit two-step loop that covers enforcement and attribution:

```console
# 1. Reserve budget immediately before the call. A denied check exits with
#    code 4, so scripts and CI can gate on it directly.
solwyn budget check --provider openai --model gpt-4.1 --estimated-input-tokens 1200

# 2. Make the provider call yourself.

# 3. Settle the reservation with what the call actually used.
solwyn budget confirm --call-id 12345678-1234-1234-1234-123456789abc --provider openai --model gpt-4.1 \
  --reservation-id "$RESERVATION" --input-tokens 1180 --output-tokens 640
```

The CLI's caller-owned workflow maintains parity with Solwyn Python SDK 0.6.0 semantics and covers enforcement and attribution. Confirmation records attribution after settlement by default, using the same canonical call ID; add `--no-record` only when attribution is intentionally handled elsewhere.

The loop covers more than text tokens:

- **Media calls** — pass the billed quantities to `budget check` (`--image-count`, `--video-seconds`, `--audio-seconds`, `--input-characters`, `--generation-count`, `--resolution`, `--quality`) so unit-priced image, video, audio, and speech calls are priced and deniable before they run.
- **Image-token models** — `budget confirm` accepts `--image-input-tokens` and `--image-output-tokens` so image-rate spend settles at its real cost.
- **Fallbacks** — declare failover intent with repeated `--fallback-provider` / `--fallback-model` pairs.
- **Per-run caps** — scope enforcement to a single agent run with `--agent-run-id`.

Use `budget status` to poll; `check` is for the moment immediately before spend.

## For coding agents

`solwyn mcp` serves read, guardrail, and attribution tools over stdio, resolving configuration and credentials fresh on every call. Standard MCP capability and schema discovery exposes the exact tools and input/output shapes in the running version. `solwyn agent-context` prints a compact SDK 0.6 capability contract suitable for pasting into an agent's context, and `solwyn init --agents` writes the equivalent guidance into the repository.

## Scripting

All commands support `--json` for schema-bearing JSON output (each payload names its schema, e.g. `solwyn.costs.v1`), and exit codes are stable and documented per command: `0` success, `2` usage, with distinct codes for API, auth, budget-denial, and not-found failures. Global flags select the project (`--project`), API base URL (`--api-url`), and output format.

## Development

The project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full local workflow and contract-update rules.

```console
uv sync --locked --all-groups
uv run solwyn --version
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Two test-suite properties worth knowing before contributing:

- Any change to the CLI surface (commands, flags, help text, exit codes, JSON output) requires regenerating the machine-readable contract snapshot with `scripts/generate_contract.py`; `tests/test_contract_snapshot.py` fails until you do.
- The vendored OpenAPI snapshot and its drift guards are self-invalidating by design — adopt fields properly or record a decline in the registry; never loosen a guard to make a test pass.

The vendored snapshot has two independent safeguards: public CLI CI checks its age and the
checked-in request/response contracts, while a proposed private monitor compares its content
with Core. The private monitor, ownership, and verified Core follow-ups are documented in
[Core contract follow-ups](docs/core-followups.md) and tracked in
[Core issue #403](https://github.com/solwyn-ai/core/issues/403).


## Recording caller-owned spend

`budget confirm` records attribution after successful settlement by default.
`--no-record` skips attribution; a recording failure warns without undoing settlement.
Use `record` for completed calls that have no reservation, including local providers:

```sh
export SOLWYN_AGENT_RUN_ID="job-42"
export SOLWYN_TAGS="environment=prod,team=ops"
solwyn record --provider ollama --model llama3 --input-tokens 120 --output-tokens 30
solwyn record --batch events.json --json
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
and `total_checks`; later chunks were not submitted. Reservation state inside a
503-failed chunk is unknown. Do not replay completed checks or assume rollback.
The matching MCP tool is `budget_check_batch`, including the same validation,
chunking, recording and partial-failure behavior.

## Trust posture

Solwyn CLI:

- sends only your own spend events; `sdk_instance_id` is a random per-invocation ID; no telemetry;

- never captures, logs, or transmits prompts or responses;
- never collects, stores, or proxies provider API keys;
- has no telemetry, update checks, or phone-home behavior; and
- stores Solwyn credentials only in the operating system keychain, never in configuration files, command-line arguments, or output.

## Security

Please report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).

### Budget rules and per-run caps

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
budget configuration and re-reads it before writing. Core has no concurrency token,
so this detects changes before the final write but cannot eliminate the remaining
race. A 503 after a write triggers a read to confirm the complete configuration;
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

Preview routing with a project key or user login, including an unsaved condition:

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
