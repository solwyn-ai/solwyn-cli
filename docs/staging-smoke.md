# Staging budget smoke tests

Run the customer budget workflow against a deployed API, using either real CLI
subprocesses or a real MCP stdio client/server session. These are the counterparts
of Core's `scripts/budget-plane-sdk-smoke.py`; they do not require a Core checkout
or either Solwyn language SDK.

```sh
uv sync --locked --all-groups
# Export a short-lived account JWT obtained from the staging auth/login endpoint.
# Keep the JWT in SOLWYN_JWT; do not put credentials in command arguments or files.
make smoke-cli
make smoke-mcp
```

Both commands default to `https://api-staging.solwyn.ai`. The account must be able
to create projects and update budgets, with Production or higher access to
paginated cost events. An existing `SOLWYN_API_KEY` is ignored: every invocation
creates its own project and obtains a fresh write-scoped key. Existing projects,
CLI configuration, and stored credentials are not modified.

The shared fixture uses `SOLWYN_JWT` only for `POST /projects`, `PUT
/projects/{id}/budget`, and `DELETE /projects/{id}`. These management operations
need an account login; the CLI intentionally has no JWT environment override.
All admission, settlement, metadata recording, and read-back assertions go
through the CLI or MCP. The script never calls an LLM provider.

## Coverage

Each full run records seven synthetic OpenAI `gpt-4.1` calls with 1,000 input and
500 output tokens each, plus two denied attempts. It verifies:

- A real reservation is returned; fail-open without a reservation is a failure.
- Confirmation settles and records attribution with the caller's call ID.
- Replaying settlement with recording disabled does not charge twice.
- Replaying the first exact recording receipt does not settle or insert again.
- Two batch reservations are distinct and can both be settled.
- Standalone ingest and call-ID retries leave one durable event per call.
- A scoped run cap denies its run while a fresh run can still proceed.
- A project hard cap denies, the CLI utilization gate exits 4, and `alert_only`
  permits a call above the project's cap.
- Scoped and project threshold alerts each fire once, with the installed dollar
  cap and the correct capped run in the scoped event.
- Cost totals, event rows, tokens, enforcement counters, and run rollups agree.
  Denied attempts produce zero-cost receipts and are excluded from paid totals.
- Event tags are visible or redacted according to the account's advertised plan.

CLI execution checks real exit codes and versioned JSON output, including exit 4
for budget denial. A warning about failed recording cannot pass as successful
durable ingest.

MCP execution additionally negotiates the modern protocol with the actual stdio server,
checks tool discovery and annotations, and proves the `read` profile rejects a
guardrail tool. Writes use `guardrails` in one persistent session. Its batch
probe retains a UUIDv7 operation ID, performs `recover_only` GET recovery, and
checks that results and reservation deadlines remain unchanged. MCP denial must
be a successful tool result with `allowed=false`; unrelated `isError` results
fail. MCP cost totals, grouped costs, budget status, run lists/details, and alert
history are also checked.

The MCP batch workflow requires the durable `POST /api/v1/budgets/check/batches`
route and its GET recovery endpoint. If intentionally testing the remaining
workflows against an older deployment, use
`make smoke-mcp SMOKE_ARGS='--skip-batch'`. This explicit option reports
`skipped_phases: ["batch"]` and exercises five successful calls instead of seven;
it does not claim batch recovery passed. Both commands test batches by default.

MCP has no cost-events tool or process exit-code gate. Its smoke uses the CLI for
those checks and as an independent detailed ledger oracle. `record_call` accepts
one event at a time; the CLI version exercises `record --batch`. Neither surface
exposes the SDK's lease lifecycle, so these tests do not cover lease grants,
renewals, surrender, or SDK-specific caches and reporter timing.

## Durable effects and cleanup

Synthetic usage is real **staging accounting data**: it consumes request quota,
raises the durable budget floor, and can send real threshold/scoped budget
notifications to the test account. It does not incur an OpenAI provider charge.
At staging prices observed during development, a full run added $0.042 to its
project's ledger. Pricing remains server-owned.

Projects are archived and their keys revoked in `finally`, including ordinary
test failures and interrupts. Archival does not erase the durable account-level
history or reverse quota. A lost project-creation response or forced process kill
can leave a project behind; use the reported `smoke-cli-*` / `smoke-mcp-*` name to
find it in the dashboard. Cleanup failure makes the command fail.

Keep a fixture for dashboard inspection explicitly:

```sh
make smoke-cli SMOKE_ARGS='--keep-project'
make smoke-mcp SMOKE_ARGS='--keep-project'
```

The script reports the project ID but never prints its key. Manage the retained
project through the dashboard and archive it when finished. No test credentials
are stored in this repository.

## Replay acknowledgements

Core checks the committed `(timestamp, sdk_instance_id)` uniqueness key before
the call-ID ledger. An exact receipt replay can therefore return a `legacy_key`
duplicate. The CLI reports that conservatively as `recorded.status=failed`; MCP
reports `recording_unknown` with `isError=true` and retained data. The smoke
expects this precise ambiguity only for an intentional exact replay: zero rows
ingested, no rejection, and the matching duplicate call ID/index. It then checks
the ledger independently. It does not reinterpret a general recording failure
as success. A `call_id` duplicate must instead report `deduplicated`.

## Output and troubleshooting

Progress and safe failure messages go to stderr. Stdout contains one JSON summary
(`solwyn.smoke.cli.v1` or `solwyn.smoke.mcp.v1`) with the outcome, project ID,
and cleanup status. Successful runs also include completed/skipped phases, call
counts, and durable cost. Scripts exit 0
on success and 1 on failure; invalid command-line arguments exit 2. Make may
return its own nonzero status when a script fails.

CLI output and server stderr are captured without being echoed or saved, so a
regression cannot leak credentials into smoke logs. Failed mutations are never
automatically retried by the harness. Convergence polling uses read commands,
never admission checks. The deployed API must support the checked contracts;
missing routes or mismatched response schemas are failures.

```sh
make smoke-cli SMOKE_ARGS='--wait-seconds 90 --timeout 45'
uv run python scripts/budget-plane-mcp-smoke.py --help
SOLWYN_API_URL=http://127.0.0.1:8080 make smoke-cli
```

`--wait-seconds` bounds each read-convergence phase (default 60 seconds), and
`--timeout` bounds each subprocess, MCP call, or fixture HTTP request (default 30
seconds). Only the exact staging origin and loopback HTTP(S) APIs are accepted;
a trailing `/api/v1` is normalized. These opt-in live tests are not run by public
CI. Their offline failure-boundary and protocol-launch tests run with `uv run
pytest`.
