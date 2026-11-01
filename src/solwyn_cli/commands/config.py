"""Commands for inspecting and updating local non-secret configuration."""

import os
from contextlib import nullcontext
from typing import Annotated

import typer
from pydantic import ValidationError

from solwyn_cli import auth
from solwyn_cli.config import (
    CONFIG_KEYS,
    Config,
    ConfigLoadError,
    ConfigWriteError,
    load_config,
    update_config_value,
)
from solwyn_cli.help import help_contract
from solwyn_cli.output import _terminal_safe
from solwyn_cli.runtime import API_URL_SOURCE_LABELS, resolve_api_url


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
        exits="0 success; 2 usage; 3 credential lock (credential_store only)",
        schema="none (human confirmation on stderr)",
    ),
)
def set_config_value(
    key: Annotated[str, typer.Argument(help="Configuration key to update.")],
    value: Annotated[str, typer.Argument(help="New configuration value.")],
) -> None:
    """Validate and persist one configuration value."""
    validated_key = _validate_key(key)
    # The store selector changes under the exclusive credential lock, like
    # solwyn login --store, so no credential read or write straddles it.
    lock = (
        auth.credential_selector_lock()
        if validated_key == "credential_store"
        else nullcontext()
    )
    try:
        with lock:
            update_config_value(validated_key, value)
    except auth.CredentialStorageError as error:
        # config commands run without the command context (so a broken config
        # stays fixable): plain stderr, exit 3 like other storage errors.
        typer.echo(_terminal_safe(f"Error: {error}"), err=True)
        if error.hint:
            typer.echo(_terminal_safe(f"Hint: {error.hint}"), err=True)
        raise typer.Exit(code=3) from None
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
def list_config_values(context: typer.Context) -> None:
    """Print every allowlisted value; api_url shows the effective URL and source."""
    config = _load_config_for_command()
    try:
        api_url, source = resolve_api_url(
            flag=context.find_root().params.get("api_url"),
            environ=os.environ,
            config=config,
        )
    except ValidationError:
        raise typer.BadParameter("Invalid API URL.", param_hint="api-url") from None
    for key in sorted(CONFIG_KEYS):
        value = getattr(config, key)
        if key == "api_url":
            value = f"{api_url} ({API_URL_SOURCE_LABELS[source]})"
        typer.echo(f"{key}={'' if value is None else value}")
