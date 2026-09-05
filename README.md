# Solwyn CLI

Work with Solwyn from the command line: enforce LLM spend guardrails on calls the Solwyn SDK does not instrument, inspect budgets, costs, and provider health, and recover from leaked or expired project keys — from your terminal, your scripts, or your coding agent.

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
| `costs` | Show aggregate project costs (`--range 7d\|30d\|90d\|365d`, `--group-by day\|model\|provider`). |
| `record` | Attribute one completed call, or a JSON batch, without a reservation. |
| `tags list`, `tags values` | Discover account-scoped keys and values (Team+). |
| `tags retire` | Retire a registered account tag key (JWT admin/owner). |
| `budget status` | Read current budget status without authorizing spend. |
| `budget check` / `budget confirm` | Pre-flight guardrail loop for un-instrumented provider calls (below). |
| `budget set` | Update selected budget settings while preserving all other configuration. |
| `projects create` / `list` / `show` | Manage projects; `create --scope` mints the first key with an explicit scope. |
| `keys show` / `rotate` / `revoke` | Inspect key metadata (including its scope), rotate with `--scope` to change it, or hard-revoke. |
| `mcp` | Serve Solwyn tools to agents over local MCP stdio. |
| `agent-context` | Print safe Solwyn budget guidance for coding agents. |
| `config` | Inspect and update local configuration. |

Every command's `--help` states its exit codes, authentication requirements, and JSON output schema.

## Budget guardrails for un-instrumented calls

Calls made through the Solwyn SDK are enforced automatically. For everything else — a `curl`, another language, a one-off script — the CLI provides the same guardrail as an explicit two-step loop:

```console
# 1. Reserve budget immediately before the call. A denied check exits with
#    code 4, so scripts and CI can gate on it directly.
solwyn budget check --provider openai --model gpt-4.1 --estimated-input-tokens 1200

# 2. Make the provider call yourself.

# 3. Settle the reservation with what the call actually used.
solwyn budget confirm --call-id call-01 --provider openai --model gpt-4.1 \
  --reservation-id "$RESERVATION" --input-tokens 1180 --output-tokens 640
```

The loop covers more than text tokens:

- **Media calls** — pass the billed quantities to `budget check` (`--image-count`, `--video-seconds`, `--audio-seconds`, `--input-characters`, `--generation-count`, `--resolution`, `--quality`) so unit-priced image, video, audio, and speech calls are priced and deniable before they run.
- **Image-token models** — `budget confirm` accepts `--image-input-tokens` and `--image-output-tokens` so image-rate spend settles at its real cost.
- **Fallbacks** — declare failover intent with repeated `--fallback-provider` / `--fallback-model` pairs.
- **Per-run caps** — scope enforcement to a single agent run with `--agent-run-id`.

Use `budget status` to poll; `check` is for the moment immediately before spend.

## For coding agents

`solwyn mcp` serves read, guardrail, and attribution tools over stdio — `budget_status`, `budget_check`, `budget_confirm`, `get_project_status`, `get_costs`, `list_projects`, `record_call`, `list_tags`, `list_tag_values` — resolving configuration and credentials fresh on every call. `solwyn agent-context` prints a compact capability contract suitable for pasting into an agent's context, and `solwyn init --agents` writes the equivalent guidance into the repository.

## Scripting

All commands support `--json` for schema-bearing JSON output (each payload names its schema, e.g. `solwyn.costs.v1`), and exit codes are stable and documented per command: `0` success, `2` usage, with distinct codes for API, auth, budget-denial, and not-found failures. Global flags select the project (`--project`), API base URL (`--api-url`), and output format.

## Development

The project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

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
