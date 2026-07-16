# Solwyn CLI

The command-line client for Solwyn. This repository is private; the CLI is distributed through the published `solwyn-cli` package.

## Installation

Install Solwyn CLI from PyPI with your preferred tool:

```console
uv tool install solwyn-cli
solwyn --version
```

```console
pipx install solwyn-cli
solwyn --version
```

You can also install it with plain `pip`:

```console
pip install solwyn-cli
solwyn --version
```

## Development

Solwyn CLI requires Python 3.11 or newer and uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```console
uv sync
uv run solwyn --version
uv run solwyn-cli --version
```

Both executable names run the same CLI. The `solwyn-cli` alias exists for compatibility with the package name.

To install the CLI from a local checkout for development, use:

```console
uv tool install .
solwyn --version
```

## Trust posture

Solwyn CLI:

- never captures, logs, or transmits prompts or responses;
- never collects, stores, or proxies provider API keys;
- has no telemetry, update checks, or phone-home behavior; and
- stores Solwyn credentials only in the operating system keychain, never in configuration files, command-line arguments, or output.

## Security

Please report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
