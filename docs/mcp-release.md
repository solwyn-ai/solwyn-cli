# MCP release preparation

**Unpublished Registry draft.** The `solwyn-cli` 0.2.0 package is published on
PyPI (verified 2026-10-14), but that September release predates later changes
in this checkout. The [Registry draft](registry/server.draft.json) proposes
`ai.solwyn/solwyn`; namespace ownership is **unverified**. Neither local validation
nor MCP discovery proves Registry publication, ownership, safety certification or
availability of checkout-only features in the published wheel.

## Validate locally

From an authorized CLI checkout:

```sh
uv sync --locked --all-groups
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
uv run --no-sync python scripts/validate_mcp_registry.py
uv lock --check --project examples/mcp/python
uv run --no-sync python scripts/mcp_wheel_smoke.py --python 3.11
uv build --clear
```

The normal full-suite legacy probes require `SOLWYN_TEST_LEGACY_PYTHON` pointing
to a **separate** actual MCP1.28.1 environment. CI creates it using the hashed
[`scripts/locks/mcp-legacy.txt`](../scripts/locks/mcp-legacy.txt); the standalone
wheel command always creates/runs it and does not skip legacy evidence.
The portable wheel runner exports `uv.lock` production requirements with hashes,
syncs a fresh environment, installs the exact built wheel with `--no-deps`, and
runs `uv pip check`. OTel SDK/exporters are not runtime dependencies. Build-backend
resolution remains separate; this is not a claim of byte-identical wheel builds.

Inspect the final distribution before uploading; for the current version:

```sh
uv run --no-sync python scripts/inspect_wheel.py dist/solwyn_cli-0.2.0-py3-none-any.whl
```

Protected checks keep their existing names: `Quality`, `Tests (Python 3.11)`,
`Tests (Python 3.12)`, and `Tests (Python 3.13)`. New `Wheel MCP` jobs run on Linux,
macOS and Windows with Python3.11/Node24 and execute the packaged doctor's real
pipe path. Local macOS checks do not substitute for the other OS job results.
Require the final repository/cross-repository and actual PR CI evidence before
releasing. The kit neither runs providers nor supplies production rollout proof.

## Release Core before its CLI consumers

1. Review and release the coordinated Core changes first: observed-media
   settlement, the durable batch journal and read-only key identity route.
   Core's journal migration is `0015_budget_check_batches`, following
   `0014_cost_event_lease_id`. In the team's authorized release environment,
   with its intended database configuration already selected, the Core API
   migration command is `uv run --package solwyn-api alembic upgrade head`
   from Core's `api` directory. Apply the migration before deploying the
   dependent Core API/workers; use Core's operational deployment procedure.
2. Verify Core's batch POST/GET endpoints, historical expiry, settlement/media
   pricing and `/api/v1/auth/key` against that deployment. The CLI has no
   fallback from the durable MCP batch journal to the ambiguous legacy route.
   A successful source test is not evidence the deployment contains these paths.
3. Choose the actual package version and update package, locks where needed,
   draft metadata and docs together. Validate the exact final wheel and both
   entrypoints from outside the checkout. Distinguish a prepared checkout from
   the published distribution; update install guidance only after the new release is verified.
4. Verify namespace ownership before adding any package README ownership marker.
   A domain namespace such as `ai.solwyn/*` needs the Registry's domain proof;
   package-index ownership is a separate prerequisite. If the namespace changes,
   update the draft and corresponding validator expectation before release.
5. Only in an authorized release session, publish the reviewed package artifact,
   verify the published wheel's metadata/content and clean-install behavior,
   then submit the matching Registry metadata. None of these steps is performed
   by the adoption-kit task.

The eventual publication commands are `uv publish` for the reviewed `dist`
artifacts and the Registry publisher's `mcp-publisher publish` from the directory
containing the final `server.json`. Obtain credentials through the release
system, and follow the publisher's current authentication/namespace verification
instructions. Do not run these commands for `server.draft.json` or infer
that the draft is an approved Registry listing or that the published 0.2.0
wheel contains this checkout's later additions.

## Registry schema and ownership marker

The vendored [schema](registry/server.schema.2025-12-11.json) is the official
[stable Registry schema 2025-12-11](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json),
fetched on 2026-09-07. Its SHA256 is
`3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0`.
The adjacent checksum file guards local changes. The validator uses this local
Draft7 schema and checks package/version/stdio/argument/secret semantics offline.
Registry schema date **2025-12-11** is unrelated to MCP protocol **2026-07-28**
or Python/TypeScript SDK package versions. No remote schema lookup is needed.

The draft declares the PyPI identifier `solwyn-cli`, whose same-name console
alias allows a Registry installer to invoke it. `--project` is a required
non-secret named argument before `mcp`; `--profile read` follows it. The optional
secret `SOLWYN_API_KEY` has no value/default; omission uses the same OS account's
keychain. There is no hosted transport or public repository URL invented for
the private checkout.

For the proposed namespace **only after ownership is verified**, the eventual
package README marker would be:

```html
<!-- mcp-name: ai.solwyn/solwyn -->
```

That marker is intentionally absent from the package README today. The offline
validator enforces this explicit unpublished Registry checkpoint and the absent
README ownership marker, independently of PyPI package availability. Transition
its tests and checks deliberately when preparing the verified release, rather than treating
this draft as a ready-to-submit listing. The final marker must exactly match
the verified Registry name. See official [Registry package types](https://modelcontextprotocol.io/registry/package-types),
[authentication](https://modelcontextprotocol.io/registry/authentication), and
[publisher quickstart](https://modelcontextprotocol.io/registry/quickstart).

Remote HTTP hosting, MCP Apps, Tasks, Skills extensions and lease coordination
remain follow-up work. This release kit adds none of those capabilities.
