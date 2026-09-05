# Core contract follow-ups

This document records the process work that accompanies the CLI's vendored Core contract. The
findings were re-verified on 2026-09-05 against clean Core `main` at
`690d61c0de1f11c6653f7a8a17800356f5009a8c`. Detailed source evidence and implementation
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
review even during quiet periods. The content monitor runs privately and detects actual contract
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

The Core/infra maintainers own the workflow, schedule, and GitHub App credential. The CLI
maintainers own triage of the generated CLI issue and the follow-up snapshot/adoption pull
request. The GitHub App should have only the cross-repository permissions needed for this job:
read Core and CLI contents, and read/write CLI issues. Do not use a personal access token.

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
7. Treat checkout, authentication, parsing, and issue-update failures as monitor failures. Open or
   update a separate failure issue in Core so an infrastructure failure can never be reported as
   "no drift."

The Core repository already proves that its own `shared/openapi.json` matches a fresh export in
`scripts/check-openapi.sh:8-16`. The proposed job starts from that checked-in customer contract
and uses the CLI's `scripts/sync_spec.py:73-94` for the cross-repository candidate. It does not
duplicate API schema generation.

Implementation of this monitor is tracked in
[Core issue #403](https://github.com/solwyn-ai/core/issues/403).

### Acceptance criteria

- A path, parameter, security declaration, required field, enum member, or schema type change in
  Core opens or updates the deduplicated CLI issue on the next scheduled run.
- Changing only `x-solwyn-snapshot-date`, `x-solwyn-core-ref`, whitespace, or JSON key order does
  not report content drift.
- A broken Core checkout or GitHub App token makes the workflow fail and creates a visible Core
  monitor-failure issue.
- The workflow cannot run with secrets on an untrusted CLI pull request.
- The CLI's existing 30-day age test and request/response drift tests remain unchanged.

## Verified Core issue register

The ten wave-0 findings are still current at Core `690d61c0`. Exact-symbol searches across open
and closed `solwyn-ai/core` issues found no earlier matches on 2026-09-05. They are now tracked as:

| # | Status | Core issue |
| --- | --- | --- |
| 1 | Still current | [#394 — Declare project-key auth for agent-run and provider GET operations in OpenAPI](https://github.com/solwyn-ai/core/issues/394) |
| 2 | Still current | [#395 — Allow the single agent-run GET to resolve a raw customer run id](https://github.com/solwyn-ai/core/issues/395) |
| 3 | Still current | [#396 — Type `AgentRunEntry.velocity_flags` with the shared `VelocityFlag` contract](https://github.com/solwyn-ai/core/issues/396) |
| 4 | Still current | [#397 — Make `analytics.tags` upgrade responses explicit about allowed alternatives](https://github.com/solwyn-ai/core/issues/397) |
| 5 | Still current | [#398 — Validate project notification and webhook event filters against one registry](https://github.com/solwyn-ai/core/issues/398) |
| 6 | Still current | [#399 — Use one request counter for the free-tier quota gate and overview](https://github.com/solwyn-ai/core/issues/399) |
| 7 | Still current | [#400 — Expose build identity on health and token lifetime on auth responses](https://github.com/solwyn-ai/core/issues/400) |
| 8 | Still current | [#401 — Report retention clamping on untracked-surface reads](https://github.com/solwyn-ai/core/issues/401) |
| 9 | Still current | [#402 — Return provider fallback attribution on `CostEventResponse`](https://github.com/solwyn-ai/core/issues/402) |
| 10 | Still current | [#403 — Add a private scheduled Core-to-CLI OpenAPI content drift monitor](https://github.com/solwyn-ai/core/issues/403) |

No finding from the wave-0 list was obsolete at the verified commit. When Core resolves one,
move it to a resolved section with the fixing issue or commit instead of leaving a stale current
finding on this page.
