# MCP release preparation

The Registry entry is [`server.json`](../server.json) at the repository root, named
`ai.solwyn/solwyn`. It is not published yet. The published 0.3.0 package predates
the README ownership marker. 0.4.0 is the first release that carries it. The
version in `server.json` (0.4.3) must be live on PyPI before the Registry publish. Neither local validation nor MCP
discovery proves Registry publication, ownership, safety certification or
availability of checkout-only features in a published wheel.

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
uv run --no-sync python scripts/inspect_wheel.py dist/solwyn_cli-0.4.3-py3-none-any.whl
```

Protected checks keep their existing names: `Quality`, `Tests (Python 3.11)`,
`Tests (Python 3.12)`, and `Tests (Python 3.13)`. New `Wheel MCP` jobs run on Linux
for every PR and main push, and on macOS and Windows for the weekly schedule,
manual CI runs and every release tag, with Python3.11/Node24 and execute the
packaged doctor's real pipe path. Local macOS checks do not substitute for the other OS job results.
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
   `server.json` and docs together. Validate the exact final wheel and both
   entrypoints from outside the checkout. Distinguish a prepared checkout from
   the published distribution; update install guidance only after the new release is verified.
   Write the release notes in `docs/releases/<version>.md`.
4. Only in an authorized release session, publish the reviewed package artifact,
   verify the published wheel's metadata/content and clean-install behavior,
   then publish the matching Registry entry as described below. Pushing an
   annotated `vX.Y.Z` tag on the merged release commit runs
   `.github/workflows/publish.yml`, which checks that the tag matches the package
   version, tests the built wheel and uploads it to PyPI by trusted publishing
   (the `pypi` environment accepts only `v*.*.*` tags). Then create the GitHub
   release from the same tag with `gh release create vX.Y.Z --verify-tag
   --notes-file docs/releases/X.Y.Z.md`.

## Publish to the MCP Registry

Run these steps only in an authorized release session, after the PyPI release
whose README carries the marker is live.

1. Keep versions in sync. `server.json` `version` and `packages[0].version` must
   equal `pyproject.toml`'s version. The validator and
   `tests/test_mcp_registry.py` fail otherwise. A published Registry version
   cannot change or be unpublished, so a metadata-only fix ships with the next
   package release. The validator does not accept Registry-only revisions such
   as `X.Y.Z-1`.
2. Prove ownership of `solwyn.ai` once. Generate an Ed25519 key and publish its
   public half as a TXT record on the apex domain:

   ```sh
   openssl genpkey -algorithm Ed25519 -out key.pem
   PUBLIC_KEY="$(openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
   echo "solwyn.ai. IN TXT \"v=MCPv1; k=ed25519; p=${PUBLIC_KEY}\""
   ```

   Keep `key.pem` in the release secret store, never in the repository. Wait for
   the record to propagate.
3. Log in with the DNS proof:

   ```sh
   PRIVATE_KEY="$(openssl pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
   mcp-publisher login dns --domain solwyn.ai --private-key "${PRIVATE_KEY}"
   ```

4. From the repository root, check and publish. `mcp-publisher` reads
   `./server.json` by default:

   ```sh
   uv run --no-sync python scripts/validate_mcp_registry.py
   mcp-publisher validate
   mcp-publisher publish
   ```

5. Confirm the listing appears in the Registry search for `ai.solwyn/solwyn`.

Install `mcp-publisher` with `brew install mcp-publisher` or from the Registry's
GitHub releases. See the official [package types](https://modelcontextprotocol.io/registry/package-types),
[authentication](https://modelcontextprotocol.io/registry/authentication) and
[versioning](https://modelcontextprotocol.io/registry/versioning) pages.

## Registry schema and ownership marker

The vendored [schema](registry/server.schema.2025-12-11.json) is the official
[stable Registry schema 2025-12-11](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json),
fetched on 2026-09-07 and rechecked as the newest released schema on 2026-10-30. Its SHA256 is
`3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0`.
The adjacent checksum file guards local changes. The validator uses this local
Draft7 schema and checks package/version/stdio/argument/secret semantics offline.
Registry schema date **2025-12-11** is unrelated to MCP protocol **2026-07-28**
or Python/TypeScript SDK package versions. No remote schema lookup is needed.

`server.json` declares the PyPI identifier `solwyn-cli`, whose same-name console
alias lets `uvx solwyn-cli` invoke it. `--project` is a required non-secret named
argument before `mcp`; `--profile guardrails` follows it, matching the default
`solwyn mcp` surface. The optional
secret `SOLWYN_API_KEY` has no value/default; omission uses the same OS account's
credential store. There is no hosted transport or public repository URL invented for
the private checkout.

The Registry verifies PyPI ownership by finding `mcp-name: <server name>` in the
package description, which PyPI renders from `README.md` (`readme` in
`pyproject.toml`). The README carries it as a hidden comment:

```html
<!-- mcp-name: ai.solwyn/solwyn -->
```

The offline validator requires this exact marker and fails if it drifts from
`server.json`'s `name`.

Remote HTTP hosting, MCP Apps, Tasks, Skills extensions and lease coordination
remain follow-up work. This release kit adds none of those capabilities.
