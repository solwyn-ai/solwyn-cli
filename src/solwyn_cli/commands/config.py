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


app = typer.Typer(help="Inspect and update local configuration.")


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


@app.command("get")
def get_config_value(
    key: Annotated[str, typer.Argument(help="Configuration key to read.")],
) -> None:
    """Print one configuration value."""
    validated_key = _validate_key(key)
    config = _load_config_for_command()
    value = getattr(config, validated_key)
    typer.echo("" if value is None else value)


@app.command("set")
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


@app.command("list")
def list_config_values() -> None:
    """Print every allowlisted configuration value in deterministic order."""
    config = _load_config_for_command()
    for key in sorted(CONFIG_KEYS):
        value = getattr(config, key)
        typer.echo(f"{key}={'' if value is None else value}")
