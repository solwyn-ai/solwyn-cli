"""Commands for inspecting and updating local non-secret configuration."""

from typing import Annotated

import typer
from pydantic import ValidationError

from solwyn_cli.config import (
    CONFIG_KEYS,
    Config,
    ConfigLoadError,
    ConfigWriteError,
    load_config,
    update_config_value,
)
from solwyn_cli.help import help_contract


app = typer.Typer(
    help="Inspect and update local configuration.",
    epilog=help_contract(
        endpoint="local configuration router",
        auth="local (none)",
        exits="0 success; 2 usage",
        schema="none (subcommands emit plain text)",
    ),
)


def _validate_key(key: str) -> str:
    if key not in CONFIG_KEYS:
        raise typer.BadParameter(f"Invalid configuration key: {key}", param_hint="key")
    return key


def _load_config_for_command() -> Config:
    try:
        return load_config()
    except ConfigLoadError:
        raise typer.BadParameter(
            "Could not load configuration.", param_hint="config"
        ) from None


@app.command(
    "get",
    epilog=help_contract(
        endpoint="local configuration read",
        auth="local (none)",
        exits="0 success; 2 usage",
        schema="none (plain text value)",
    ),
)
def get_config_value(
    key: Annotated[str, typer.Argument(help="Configuration key to read.")],
) -> None:
    """Print one configuration value."""
    validated_key = _validate_key(key)
    config = _load_config_for_command()
    value = getattr(config, validated_key)
    typer.echo("" if value is None else value)


@app.command(
    "set",
    epilog=help_contract(
        endpoint="local configuration write",
        auth="local (none)",
        exits="0 success; 2 usage",
        schema="none (human confirmation on stderr)",
    ),
)
def set_config_value(
    key: Annotated[str, typer.Argument(help="Configuration key to update.")],
    value: Annotated[str, typer.Argument(help="New configuration value.")],
) -> None:
    """Validate and persist one configuration value."""
    validated_key = _validate_key(key)
    try:
        update_config_value(validated_key, value)
    except ValidationError:
        raise typer.BadParameter(
            f"Invalid value for {validated_key}.", param_hint="value"
        ) from None
    except ConfigLoadError:
        raise typer.BadParameter(
            "Could not load configuration.", param_hint="config"
        ) from None
    except ConfigWriteError:
        raise typer.BadParameter(
            "Could not write configuration.", param_hint="config"
        ) from None
    typer.echo(f"Set {validated_key}.", err=True)


@app.command(
    "list",
    epilog=help_contract(
        endpoint="local configuration read",
        auth="local (none)",
        exits="0 success; 2 usage",
        schema="none (plain text key/value list)",
    ),
)
def list_config_values() -> None:
    """Print every allowlisted configuration value in deterministic order."""
    config = _load_config_for_command()
    for key in sorted(CONFIG_KEYS):
        value = getattr(config, key)
        typer.echo(f"{key}={'' if value is None else value}")
