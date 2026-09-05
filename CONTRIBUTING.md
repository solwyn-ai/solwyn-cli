# Contributing to Solwyn CLI

Solwyn CLI uses [uv](https://docs.astral.sh/uv/) for dependency management and command
execution. Set up a checkout and run the standard verification with:

```console
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Command and output contracts

Treat command names, flags, help text, exit codes, stdout/stderr behavior, and JSON schemas as
public interfaces. When one changes, regenerate the machine-readable CLI contract:

```console
uv run python scripts/generate_contract.py
```

Response parsers stay fail-closed. Adopt new Core response fields in the response registry,
and register every request mirror and every intentionally unadopted optional request field.
Do not relax a drift guard to make a new Core contract pass.

## OpenAPI snapshot updates

The CLI vendors Core's customer OpenAPI document so normal development, releases, and public
CI do not need access to the private Core repository. An authorized maintainer updates it from
a clean Core `main` checkout:

```console
uv run python scripts/sync_spec.py ../core
```

The sync records both the UTC date and the exact Core commit. Review the resulting request and
response drift failures, adopt or explicitly decline every field, regenerate the CLI contract
when the surface changes, and run the full test suite before committing the snapshot.

Snapshot age and snapshot content answer different questions. The public age check tells us
how long it has been since a maintainer performed a sync. It cannot determine whether Core's
contract changed during that time. Content comparison requires a current private Core checkout,
so it belongs in Core or private infrastructure and must never be added to public CLI CI. See
[Core contract follow-ups](docs/core-followups.md#private-openapi-content-drift-monitor) for the
proposed monitor and ownership; implementation is tracked in
[Core issue #403](https://github.com/solwyn-ai/core/issues/403).

## Pull requests

Keep pull requests focused and include the verification commands and results in the description.
If the change affects CLI behavior, name the corresponding `solwyn-docs` pages that need an
update. Do not include credentials, provider payloads, prompts, or responses in fixtures or logs.
