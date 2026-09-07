# Connect a harness to Solwyn MCP

Solwyn 0.2.0rc1 is **pre-release, private and not published to PyPI**. Start from an
authorized source checkout or a wheel built from it. This guide uses local stdio;
there is no hosted MCP endpoint. Account/project access is a separate prerequisite.

## Install and make one read

From the authorized CLI checkout:

```sh
uv sync --locked --all-groups
uv build --wheel
uv tool install --python 3.11 dist/solwyn_cli-0.2.0rc1-py3-none-any.whl
uv tool dir --bin
solwyn --version
solwyn login --with-key
```

The login command prompts for a project key and stores it in the OS keychain.
Use a `read_only` key for first setup. A desktop host must run under that same OS
account with keychain access. For CI, use the runner's secret input mechanism to
set `SOLWYN_API_KEY` and explicitly forward that variable to the server. Keys do
not belong in argv, repository JSON or generated descriptors.

`uv tool dir --bin` shows the executable directory. Add it to PATH if necessary,
or run its `solwyn` (`solwyn.exe` on Windows) by absolute path. The alias
`solwyn-cli` invokes the same application. Python 3.11 or newer is required.
Tool installation resolves dependencies; the fully locked wheel acceptance
procedure below separately installs the production versions from `uv.lock`.

Replace this synthetic project ID with your actual project ID:

```sh
solwyn --json --project proj_0123456789abcdef01234567 mcp-config
solwyn --json --project proj_0123456789abcdef01234567 doctor --mcp
solwyn --json --project proj_0123456789abcdef01234567 budget status
```

`mcp-config` runs offline and resolves the absolute executable for its installed
interpreter. It defaults to the 13-tool `read` profile. Extract only `command`
and `args` from its `solwyn.mcp-config.v1` result into your host's documented
configuration container. For example, the descriptor portion on POSIX is:

```json
{
  "command": "/absolute/path/to/solwyn",
  "args": ["--project", "proj_0123456789abcdef01234567", "mcp", "--profile", "read"]
}
```

Keep array entries separate, including executable paths containing spaces.
This is a host-neutral descriptor, not a universal host config file. Solwyn does
not edit a host configuration. No `${SECRET}` interpolation convention is
assumed: configure the host's environment or keychain access explicitly.

For a different trusted executable or authorized Core environment, global
options precede the command; command options follow it:

```sh
solwyn --json --project proj_0123456789abcdef01234567 --api-url https://api.solwyn.ai mcp-config --executable /absolute/path/to/solwyn --profile read
solwyn --json --project proj_0123456789abcdef01234567 --api-url https://api.solwyn.ai doctor --mcp --executable /absolute/path/to/solwyn --profile read
```

`doctor --mcp` checks discovery plus one authenticated MCP `budget_status` read
when it has a project key and selected project. No reservation, settlement,
recording, provider call, JWT refresh or host edit occurs. Its exit codes are
0 for all selected-key checks passing, 1 for degraded checks, and 2 for usage.

## Read identity and permission separately

| Diagnostic or setting | Meaning |
| --- | --- |
| Selected read project | Explicit global `--project`, then `SOLWYN_PROJECT_ID`, then configured default. A read tool's `project_id` can override it. |
| Authenticated key project | Core's `GET /api/v1/auth/key` identifies the supplied key. Key-bound writes use this project; a read selector cannot redirect them. |
| Credential kind/source | Environment project key, keychain project key, then keychain JWT, in that order. Empty/invalid environment key presence prevents fallback. A stored key can mask a JWT. |
| Key scope | `full`, `read_only`, or unknown when it cannot be established. A successful read does not prove write scope. `full` does not promise a future allowed decision. |
| Profile and annotations | Discovery aids. `read` exposes 13 tools; `guardrails` exposes all 17. Core still authorizes every request. Plain `solwyn mcp` defaults to `guardrails`; setup/doctor default to `read`. |

The [tool/auth matrix](mcp-profiles.md) lists exceptions: `list_projects` and
`get_overview` need JWT, account alert history needs JWT, and tag discovery needs
Team. Project-key writes need write scope, except batch GET recovery accepts a
read-only key. Profile choice cannot grant authorization.

JWT-only diagnostics perform a parent HTTP read with the captured JWT, but skip
the authenticated **MCP** read: the server has no supported environment interface
for forwarding that captured JWT. Discovery still runs and the result is
`degraded`/exit 1. It must not be interpreted as verified MCP authentication.
See [diagnostic fields and lifecycle](mcp-diagnostics.md).

| Symptom | Next step |
| --- | --- |
| Missing/non-executable launch target | Use the executable from the intended installed environment; an explicit bad target never falls back. |
| Missing project | Select the project explicitly. Doctor can still show the supplied key's identity. |
| Locked/unavailable keychain | Unlock the same OS account's keychain, or deliberately provide the intended key in the host environment. |
| Read/key project mismatch | Correct the read selector or choose the intended key. Do not test identity by reserving budget. |
| Identity endpoint unsupported/404 | Deploy the dependent Core release. Scope stays unknown on older Core. |
| Read-only key or JWT cannot write | Use the appropriate authorized project key for the caller-owned workflow; changing profiles does not change scope. |
| Parent HTTP fails but helper discovery/read works | Check proxy/certificate settings. Invalid proxy or unavailable certificate settings safely degrade parent checks; the isolated helper has its own environment. |

## Run the actual clients

The checked-in [Python client](../examples/mcp/python/client.py) uses public MCP
2.1.1 and its own `uv.lock`, with Python 3.11+. From the CLI checkout, substitute
your installed executable and project:

```sh
uv sync --locked --project examples/mcp/python --python 3.11
uv run --locked --project examples/mcp/python python examples/mcp/python/client.py --executable /absolute/path/to/solwyn --project proj_0123456789abcdef01234567
```

The [TypeScript client](../examples/mcp/typescript/client.ts) uses public scoped
`@modelcontextprotocol/client` 2.0.0 and `@modelcontextprotocol/core` 2.0.0, with an
exact npm lock. Use Node 24 LTS:

```sh
cd examples/mcp/typescript
npm ci --ignore-scripts --no-audit --no-fund
npm run build
npm test
npm start -- --executable /absolute/path/to/solwyn --project proj_0123456789abcdef01234567
```

Both programs discover the read catalog, make one successful read, validate the
Solwyn envelope/project, and print only typed selected fields as JSON. They
check both MCP `isError` and envelope `ok`. Execution failures return a fixed
allowlisted code and exit 1. Add `--validation-error` to deliberately demonstrate
`invalid_arguments` without HTTP. Add `--api-url` for a selected authorized API.
These are example-client options, not Solwyn global options.

The default `--mode modern` requires MCP **2026-07-28**: Python selects automatic
negotiation and verifies the result; TypeScript explicitly pins the modern
revision because its SDK default is legacy. `--mode legacy` exercises
**2025-11-25** with the same server. Protocol dates are separate from SDK/package
versions and the Registry schema date. TypeScript's modern negotiation can
launch a disposable sibling discovery probe before its main server. Startup
must have no spend/provider effects. The kit's legacy fixture also tests an
actual separate MCP **1.28.1** Python client against the installed server.

The clients inherit only the SDK's documented OS environment baseline and the
intended `SOLWYN_API_KEY`, including empty presence to prevent identity fallback.
They omit unrelated provider keys, Solwyn tags, Python/Node bootstrap settings,
proxy credentials and OTel configuration. The SDK adds its OS baseline even when
`env` is explicit; this is not a keychain sandbox. Independently instrumented
external clients/hosts remain separate from Solwyn's no-telemetry policy.

`--timeout` bounds client useful work (10 seconds by default, configurable from
0.1 to 30). TypeScript discovery uses this same timeout, including slow process
startup on Windows. Timeout/cancellation unwinds the transport explicitly. SDK cleanup
is additional: Python's inspected waits total up to 6.5 seconds plus scheduling;
TypeScript closes stdin, waits up to 2 seconds, then allows up to 2 seconds after
termination before killing, and the example observes direct-child exit for up
to 5 more seconds. These are controlled-server checks, not guarantees about
unkillable OS calls or arbitrary descendants. Child stderr is drained/discarded;
raw SDK exceptions and server free text are not printed.

## Own the provider boundary

These read clients do not intercept or instrument provider calls. If the Solwyn
SDK already owns a call, use MCP for insight and avoid duplicate check/confirm
or recording. For caller-owned calls, keep the gate in deterministic harness
code immediately before the provider request:

1. Persist a stable call identity and run ID. Make one fresh budget check.
2. Treat `ok=true, allowed=false` as normal denial; do not call the provider.
   Honor run-stop directives. An execution error or read result cannot authorize
   spend. The clients' tested `provider_gate`/`providerGate` helpers illustrate
   the distinction; callers still own freshness and run state.
3. On allowed spend, call the provider yourself and retain observed usage. If
   `reservation_id=null` (fail-open), skip confirmation; record once if wanted.
4. Confirm a non-null reservation using the original call ID/attempt and actual
   served model/provider, region/tier and usage. Track settlement and recording
   independently; recording failure does not undo settlement.

For MCP batch admission, persist a fresh **caller-owned UUIDv7** `operation_id`
before sending at most 32 checks. A completely lost response cannot return a
server-generated identity. Recover with the same ID and `recover_only=true`,
omitting checks: this is GET only, never a resumed suffix or renewed authority.
Retain indexed completed/failed/unknown/unattempted outcomes and reservation IDs.
Historical authority expires conservatively 300 seconds after `checked_at`;
the journal lasts 24 hours from UUIDv7 time and expired identities return 410.
Changed payload reuse returns 409. Never automatically rerun an ambiguous check
or provider call, or generate a new identity to evade unresolved effects.
Ordinary CLI `budget check --batch` still uses its legacy chunk behavior.
See [batch recovery](mcp-bounded-work.md).

For media settlement, MCP takes observed `media_usage`: integer `image_count`,
`generation_count`, `input_characters`, and numeric `audio_seconds`/`video_seconds`,
each from 0 through 100,000,000 when observed. `resolution`/`quality` have at most
32 characters and must match Core's pricing card. Null/omitted is unobservable;
zero is an observation of no billed units. Never substitute the estimate for
missing observations. Image/audio token buckets are subsets of token totals,
not media counts/seconds. CLI uses corresponding hyphenated flags; MCP's
`media_usage.is_estimated` is separate from token `is_estimated`. See
[observed media and correction limits](media-confirmation.md).

After a lost confirmation response, preserve the **FIRST MCP error's exact
`recording_retry` event** and original confirmation inputs. Within Core's
3,600-second settlement receipt window, replay identical settlement inputs with
`no_record=true`. After known settlement success, call `budget_confirm` with
only `record_only` set to that **FIRST event**. The settlement-only replay
prepares a new unsent event: never replace the first event with it. Record-only
reports `settlement=not_submitted`; keep the known settlement result separately.

The receipt does not compare payloads, correct successful settlement or rebuild
attribution. Ordinary CLI `ApiError` stderr does not expose its prepared event;
original flags cannot reconstruct that timestamp/SDK instance. A wholly lost
MCP reply or compact error that omits the event has the same limit. Do not
promise recovery from an unseen event. Core's legacy timestamp/SDK-instance
collision lane can return `legacy_key` even for an exact retry; this does not
prove the requested call-ID receipt. Keep recording `unknown`, preserve every
event field, and inspect attribution separately. Never change its timestamp or
rerun provider/reservation work to manufacture a positive receipt.

## Work limits and reproducible verification

MCP useful work has a fixed 30-second deadline, 10 fetched pages, 1 MiB aggregate
HTTP response bodies, 4 MiB encoded request bodies, 2 MiB complete MCP results,
and 4,096-character cursors. HTTP cleanup allows 250 ms. Cancellation blocks
subsequent requests but cannot undo one already admitted. OS/keychain calls may
finish later or delay exit. Doctor separately uses an 8 KiB input/16 KiB output
pipe, a 10-second helper deadline and a 10-second parent cleanup grace; its
parent HTTP probes use fixed HTTP timeouts with auth/rate-limit retry disabled,
not one universal total wall-clock bound. See the detailed
[work limits](mcp-bounded-work.md) and [diagnostic limits](mcp-diagnostics.md).

Run this acceptance command from the CLI checkout with uv and Node24 installed:

```sh
uv run --locked python scripts/mcp_wheel_smoke.py --python 3.11 --report /absolute/path/to/evidence.json
```

It builds one wheel, inspects it, exports hashed production dependencies from
`uv.lock`, installs outside the checkout into paths with spaces, and verifies
installed module provenance and absence of OTel SDK/exporters. It copies and
executes these exact clients with their own locks, both console entrypoints,
both protocol eras, auth/validation errors, cancellation and observed child
exit, actual legacy SDK1.28.1, and the packaged doctor. All API traffic is
synthetic loopback HTTP; it never uses real keys, keychain or providers. Temporary
environments and processes are cleaned up. Dependency downloads may be needed.

Local evidence is macOS, Python 3.11.15 and Node 24.11.1. CI defines actual
Linux/macOS/Windows wheel jobs, including Windows Python3.11 doctor execution;
Linux/Windows results remain pending until those jobs run. The TypeScript build
checks the example strictly but uses `skipLibCheck` for dependency declarations.
No clean-machine onboarding timing or particular desktop-host version is claimed.
Package release and Registry submission remain [preparation only](mcp-release.md).
