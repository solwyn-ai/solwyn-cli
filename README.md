# Solwyn CLI

The command-line client for Solwyn. This repository is private while the first release is prepared.

## Development

Solwyn CLI requires Python 3.11 or newer and uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```console
uv sync
uv run solwyn --version
uv run solwyn-cli --version
```

Both executable names run the same CLI. The `solwyn-cli` alias exists for compatibility with the package name.

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
