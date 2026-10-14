# Core contract follow-ups

This document records the process work that accompanies the CLI's vendored Core contract. The
findings were re-verified on 2026-10-14 against clean Core `main` at
`4925784d42e18bb6b680e5a87609d1829f1f64a9`. Detailed source evidence and implementation
discussion live in the linked Core issues; this page keeps the cross-repository process and
ownership visible to CLI contributors.

## Private OpenAPI content drift monitor

### Why two signals are required

The CLI's `tests/test_snapshot_age.py` rejects a snapshot more than 30 days after its recorded
sync date. That is an **age** signal: it answers "when did a maintainer last refresh this copy?"
It fails even when Core's OpenAPI content is byte-for-byte unchanged, and it can remain green
for as long as 30 days after a breaking contract change.

A **content drift** signal answers a different question: "does the current Core OpenAPI document
differ from the document the CLI vendors?" It should fire on the first scheduled run after a
meaningful path, parameter, schema, or security change, regardless of the snapshot's age. It
must ignore the CLI-only `x-solwyn-snapshot-date` and `x-solwyn-core-ref` stamps; otherwise every
daily run would report a false diff.

Both signals remain useful. The age gate stays in public CLI CI and forces periodic conscious
review even during quiet periods. The proposed content monitor would run privately and detect actual contract
changes promptly. A successful no-drift run must not rewrite the snapshot date, because doing so
would hide how long it has been since a maintainer reviewed and committed a sync.

The wave-0 audit records two occasions when contract content drifted before the age check forced
a review. At the research snapshot, examples included missing tag-discovery and run-stop paths,
missing budget batch/lease paths, and missing alert-event query parameters. Those are content
differences; the number of days since the last sync could neither detect nor describe them.

### Placement and ownership

Place the scheduled workflow in `solwyn-ai/core` or the private infrastructure repository. It
must never run from a CLI `pull_request` or any other public CLI workflow. Public CLI CI continues
to use only the vendored JSON and needs no Core checkout, Core token, or cross-repository secret.

The Core/infra maintainers own the workflow, schedule, and credentials. The CLI maintainers own
triage of the generated CLI issue and the follow-up snapshot/adoption pull request. Keep the
credential split specific to the workflow's host:

- **Core-hosted workflow:** grant that job's repository `GITHUB_TOKEN` `contents: read` and
  `issues: write`, and use it only for the Core monitor-failure issue. Use a GitHub App
  installation token for cross-repository CLI access, limited to CLI contents read and CLI issues
  read/write.
- **Infrastructure-hosted workflow:** use a GitHub App installation limited to the Core and CLI
  repositories. It needs contents read in both repositories, CLI issues read/write for the drift
  issue, and narrowly scoped Core issues read/write for the monitor-failure issue.

Do not use a personal access token. Neither placement grants a Core credential to public CLI CI.

### Proposed mechanism

Run daily and allow `workflow_dispatch` for repair and testing:

1. Check out clean Core `main` at the scheduled commit.
2. Check out the CLI default branch into a sibling directory with the private GitHub App token.
3. Install the CLI's pinned uv version and run its existing `scripts/sync_spec.py`, pointing it at
   the Core checkout and writing the candidate snapshot to a temporary path. This exercises the
   same clean-checkout and Core-ref validation used by maintainers.
4. Parse the candidate and checked-in snapshots, remove only `x-solwyn-snapshot-date` and
   `x-solwyn-core-ref`, and compare canonical JSON with sorted keys. Do not compare raw bytes:
   formatting and the two provenance fields are not contract content.
5. If content differs, save a private diff artifact and open or update one deduplicated CLI issue
   titled `Core OpenAPI content differs from the vendored CLI snapshot`. Include the current Core
   commit, the snapshot's pinned Core ref, first/last detected timestamps, and the changed path,
   operation, and component-schema names. Do not automatically commit or open a sync pull request;
   field adoption requires a maintainer to review the request/response drift guards.
6. If content matches, close an open drift issue only after recording the matching Core commit in
   a final comment. Do not alter the checked-in snapshot or its age metadata.
7. Treat checkout, authentication, parsing, comparison, and issue-update failures as monitor
   failures and preserve a failing workflow conclusion. After capturing the failure, make a
   best-effort attempt to open or update a separate failure issue in Core with the placement's
   Core issue credential. If that publication also fails, log the reporting failure without
   replacing or masking the original non-zero conclusion. Missing issue-write authorization can
   prevent the issue from being created; it can never turn the run into "no drift."

The Core repository already proves that its own `shared/openapi.json` matches a fresh export in
`scripts/check-openapi.sh:42-46`. The proposed job starts from that checked-in customer contract
and uses the CLI's `scripts/sync_spec.py:73-94` for the cross-repository candidate. It does not
duplicate API schema generation.

No matching content-drift workflow exists in Core at the verified commit.
Implementation remains Core/private-infrastructure work and is tracked in
[Core issue #403](https://github.com/solwyn-ai/core/issues/403).

### Acceptance criteria

- A path, parameter, security declaration, required field, enum member, or schema type change in
  Core opens or updates the deduplicated CLI issue on the next scheduled run.
- Changing only `x-solwyn-snapshot-date`, `x-solwyn-core-ref`, whitespace, or JSON key order does
  not report content drift.
- A broken checkout, credential, comparison, or issue update makes the workflow conclude failure.
- An authorized run attempts to create or update the Core monitor-failure issue; failure to publish
  that issue is reported in logs and the workflow still concludes failure.
- The workflow cannot run with secrets on an untrusted CLI pull request.
- The CLI's existing 30-day age test and request/response drift tests remain unchanged.

## Verified Core issue register

Nine wave-0 findings remain pending in source at the verified Core commit. This
register describes implementation evidence, not the current GitHub issue state.
The historical issue search was performed on 2026-09-05; no new issue search or
issue closure was performed during this verification.

| Core issue | Status | Evidence at `4925784d` |
| --- | --- | --- |
| [#394 — Declare project-key auth on agent-run and provider GETs](https://github.com/solwyn-ai/core/issues/394) | Pending | `shared/openapi.json` still declares only `HTTPBearer` for single agent-run, providers and provider-failover GETs, although runtime auth accepts project keys. |
| [#395 — Resolve raw customer run ids on single agent-run GET](https://github.com/solwyn-ai/core/issues/395) | Pending | `routers/agent_runs.py:get_project_agent_run` compares `AgentRun.id == run_id`; it does not resolve a raw name. The CLI resolves names through listing before GET. |
| [#396 — Type velocity flags with VelocityFlag](https://github.com/solwyn-ai/core/issues/396) | Pending | `AgentRunEntry.velocity_flags` remains nullable `list[str]` in source and OpenAPI. |
| [#397 — Explicit analytics.tags upgrade alternatives](https://github.com/solwyn-ai/core/issues/397) | Pending | `routers/tags.py:_require_tags_entitlement` raises upgrade-required without `allowed_values`. The CLI renders alternatives when Core supplies them. |
| [#398 — One event-filter registry](https://github.com/solwyn-ai/core/issues/398) | Pending | `NotificationConfigUpdate.enabled_event_types` remains `set[str]`; `WebhookCreateRequest.events` remains a comma-separated `str`. No shared validation registry is used by these request models. |
| [#400 — Health build identity and token lifetime](https://github.com/solwyn-ai/core/issues/400) | Pending | `HealthResponse` contains only `status` and `service`; `TokenResponse` contains access/refresh tokens and token type, without `expires_in`. |
| [#401 — Untracked-surface retention metadata](https://github.com/solwyn-ai/core/issues/401) | Pending | `UntrackedSurfaceResponse` has no clamp/window fields. The CLI's wrapper estimates the effective window from billing retention when available and reports unknown otherwise. |
| [#402 — Cost-event provider fallback attribution](https://github.com/solwyn-ai/core/issues/402) | Pending | `CostEventResponse` has `is_model_fallback`, but no provider-fallback or failover-reason field. Filtering support does not imply those fields are returned. |
| [#403 — Private scheduled content-drift monitor](https://github.com/solwyn-ai/core/issues/403) | Pending; Core/private infra | No corresponding workflow under Core `.github/workflows`; the mechanism above is a proposal. Public CLI age/content guards remain unchanged. |

### Resolved in Core

[#399 — One request counter for the free-tier gate and overview](https://github.com/solwyn-ai/core/issues/399)
was resolved by Core commit `67e3b60f` (PR #393). At the verified HEAD,
`routers/overview.py:17,172-186` uses the gate's `_free_tier_quota_state` from
`routers/costs.py` and returns its `current_requests`, quota and window fields.
The resolver's LIMIT-bounded scan reports the exact request count below the
quota, but reports the included-request cap once exceeded. `current_requests`
is a quota signal, **not a usage figure** above the cap. `total_requests` is a
separate analytics figure over its retention-clamped window. Sharing the gate's
resolver does not make the two figures interchangeable.

## Reliability audit at Core `4925784d` (2026-10-14)

- Declare the accounting-rebuild 503 from `GET /api/v1/account/costs` in OpenAPI.
  `_load_account_project_metadata` already refuses to publish a partial gauge
  when durable accounting is rebuilding; the route's response declaration
  omits this status. This requires a Core change, not a CLI snapshot edit.
- Give pre-auth budget-capacity shedding a typed machine code in OpenAPI,
  distinct from a mid-batch backend failure. At this Core HEAD, the middleware
  and global `BudgetProjectBusyError` handler both return
  `Budget admission is busy; retry shortly` with `Retry-After: 1`. The legacy
  `/budgets/check/batch` loop can commit earlier item reservations before that
  handler fires: the identical response does not prove no reservation for any
  failed legacy chunk. The CLI preserves that uncertainty, completed results,
  and later unsubmitted chunks. Single `/budgets/check` lock/floor errors precede
  reservation; durable `/budgets/check/batches` converts route failures to
  structured `outcome_unavailable`, requiring same-ID GET recovery. Only these
  two paths receive the fixed-detail no-reservation hint. Advisory uncounted
  tallies can already be folded before a single-check lock failure; retries may
  overcount their upper bound. Do not broaden reservation absence to all effects.
- Give explicitly unaccepted ingest capacity responses stable machine codes
  and declare their rollback guarantee. The CLI currently requires status 503,
  `Retry-After: 1`, and one of the exact budget-busy, tag-registry-busy, or
  accounting-maintenance details from Core's three handlers. Unrecognized proxy
  responses or missing/different headers retain recording uncertainty. Identical
  call-ID replay remains separately safe under ingest deduplication; it cannot
  establish that the earlier request recorded nothing. The CLI retains this
  evidence across all attempts and JWT refresh: a recognized final rejection
  alone cannot prove the whole logical batch call was unaccepted. Core's auth
  dependencies and IP/project 429 gates reject before ingest starts.
