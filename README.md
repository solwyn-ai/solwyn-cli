# Solwyn CLI

<!-- mcp-name: ai.solwyn/solwyn -->

Work with Solwyn from the command line: enforce and attribute LLM spend for calls the Solwyn SDK does not instrument, inspect budgets, costs, and provider health, and recover from leaked or expired project keys — from your terminal, your scripts, or your coding agent.

This README describes solwyn-cli 0.4.0. The package is distributed on PyPI as `solwyn-cli`;
run `solwyn --version` to see which version you have installed, and use `solwyn --help`
and MCP discovery to inspect its exact surface.

## Installation

Install the published package with uv:

```console
uv tool install solwyn-cli
solwyn --version
```

Or use `pipx install solwyn-cli`. For development or unreleased commands, install
from an authorized checkout with `uv tool install .`.

Requires Python 3.11 or newer. The package installs two identical executables: `solwyn` and `solwyn-cli` (an alias matching the package name).

## Getting started

```console
solwyn login          # approve this CLI in your browser; --with-key stores a project key instead
solwyn init           # configure Solwyn for the current repository
solwyn status         # project budget and provider health at a glance
```

`solwyn login` prints a URL and a code. Open the URL in any browser, sign in or sign up, and approve. It works over SSH and in agent sandboxes, and an agent can relay the code to you. The login is scoped: it can read, stop agent runs and lower caps. Raising a cap, changing enforcement mode, pausing rules, keys, billing and team changes ask for your approval in the browser each time, and the command waits for it (exit 6 if it is not given). `eval "$(solwyn keys env)"` hands the stored project key to the SDK in your shell. See [login](docs/commands/login.mdx) and [approvals](docs/commands/approvals.mdx).

`solwyn init` authenticates, selects a project, writes local configuration, detects the provider SDKs your code imports, prints the wiring for each, and verifies your budget end-to-end. Add `--agents` to also write guidance files for Claude, Cursor, and `AGENTS.md`.

## Three ways to use it

Calls made through the Solwyn Python SDK are enforced and attributed automatically. The CLI covers the three cases the SDK cannot reach.

### Give a coding agent a budget

`solwyn mcp` serves read, guardrail, and attribution tools to any MCP client over local stdio, resolving configuration and credentials fresh on every call. Standard MCP capability and schema discovery exposes the exact tools and input/output shapes in the running version. `solwyn agent-context` prints a compact capability contract suitable for pasting into an agent's context, and `solwyn init --agents` writes the equivalent guidance into the repository.

```console
solwyn --project proj_0123456789abcdef01234567 mcp --profile read   # 33 read tools
solwyn --project proj_0123456789abcdef01234567 mcp                  # default guardrails profile, all 37 tools
solwyn doctor --mcp                                                 # verify the wiring
```

Customer reads include account-wide alert activity and open deliveries, last firing,
advisory unpriced outage traffic, plan catalog/status/change preview, identity/email verification,
phone/SMS prerequisites, team roster/invitations, cost analysis/simulation, provider visibility and audit.
The 33 reads appear in both profiles; billing/team/member writes remain CLI-only.
See [alerts](docs/commands/alerts.mdx), [budget](docs/commands/budget.mdx),
[plan](docs/commands/plan.mdx), [account](docs/commands/account.mdx),
[team](docs/commands/team.mdx) and [invitation preview](docs/commands/invite.mdx).

Start with the [MCP quickstart](docs/mcp-quickstart.md). Profiles are fixed at launch and never grant permissions; see [MCP profiles and tool contracts](docs/mcp-profiles.md) and the [agent integration guide](https://docs.solwyn.ai/cli/agents).

### Gate CI on budget

One pipeline step, one exit code, no code change. `budget status` is a pure read, so a read-only key is enough.

```console
# Exit 4 when the project is at or over 80% of its monthly budget.
solwyn --json budget status --max-utilization 80
```

See [Gate CI on budget](https://docs.solwyn.ai/cli/ci-budget-gate).

### Meter a call the SDK does not wrap

For a `curl`, another language, or a one-off script, reserve budget before the call and settle it after. A denied check exits with code 4, so scripts stop before they spend.

```console
# 1. Reserve immediately before the call.
solwyn budget check --provider openai --model gpt-4.1 --estimated-input-tokens 1200

# 2. Make the provider call yourself.

# 3. Settle with what the call actually used. This also records attribution.
solwyn budget confirm --call-id 12345678-1234-1234-1234-123456789abc --provider openai --model gpt-4.1 \
  --reservation-id "$RESERVATION" --input-tokens 1180 --output-tokens 640
```

The CLI's caller-owned workflow follows the same reserve, settle and record loop as the Solwyn SDKs and covers enforcement and attribution. Use `record` for a completed call that had no reservation, such as a local model. The same loop prices image, video, audio, and speech calls by their billed units, declares fallback intent, scopes enforcement to a single agent run, and reserves for a batch of calls at once; see [Meter un-instrumented calls](https://docs.solwyn.ai/cli/metering-uninstrumented-calls) for the walkthrough and [caller-owned spend reference](docs/caller-owned-spend.md) for the full rules.

## Commands

Every command's `--help` states its exit codes, authentication requirements, and JSON output schema.

| Group | Commands |
| --- | --- |
| Setup and identity | `init`, `login`, `logout`, `whoami`, `credentials`, `approvals`, `doctor` |
| Monitoring | `status`, `costs`, `forecast`, `overview`, `providers` |
| Budget guardrails | `budget status` / `check` / `confirm` / `set`, `record`, `runs` |
| Agents | `mcp`, `mcp-config`, `agent-context` |
| Administration | `budget rules` / `rule` / `threshold`, `alerts`, `channels`, `notifications`, `webhooks`, `keys`, `projects`, `account`, `tags`, `audit`, `attest`, `plan`, `config` |

Account management includes subscription preview, change, cancellation/resume,
scheduled-change discard, hosted checkout/portal links and pending-intent dismissal.
Billing writes require an owner login, a browser approval, and confirmation or
`--yes`; preview is a login read at any role. See the [plan reference](docs/commands/plan.mdx). The [in-repo command reference](docs/commands/README.md) describes this checkout.
The published guide lives in the [CLI docs](https://docs.solwyn.ai/cli).

## Scripting

API and setup commands support global `--json` for schema-bearing JSON output (each payload names its schema, e.g. `solwyn.costs.v1`), and exit codes are stable and documented per command: `0` success, `2` usage, with distinct codes for API, auth, budget-denial, not-found, and approval (`6`) outcomes. The local `config` commands emit plain text. Global flags select the project (`--project`), API base URL (`--api-url`), and output format, and go before the subcommand.

## Trust posture

Solwyn CLI:

- sends only your own spend events; `sdk_instance_id` is an invocation ID (the retained operation UUID for recoverable MCP batches); no telemetry;
- never captures, logs, or transmits prompts or responses;
- never collects, stores, or proxies provider API keys;
- has no telemetry, update checks, or phone-home behavior; and
- stores Solwyn credentials only in the operating system keychain, never in configuration files, command-line arguments, or output.

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

Further in-repo references: [MCP release checklist](docs/mcp-release.md), [bounded MCP work and recovery](docs/mcp-bounded-work.md), [MCP diagnostics](docs/mcp-diagnostics.md), [media confirmation](docs/media-confirmation.md), and [staging smoke tests](docs/staging-smoke.md).

## Security

Please report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
