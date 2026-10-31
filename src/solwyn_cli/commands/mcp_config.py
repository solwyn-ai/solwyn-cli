"""Print an offline portable MCP command descriptor."""

from pathlib import Path
from typing import Annotated

import typer

from solwyn_cli import auth, credential_store
from solwyn_cli._mcp_launch import (
    forwarded_env_vars,
    launch_descriptor,
    resolve_executable,
)
from solwyn_cli.commands.mcp import McpProfile
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

MCP_CONFIG_HELP = help_contract(
    endpoint="none (offline)",
    auth="none; no credential lookup",
    exits="0 descriptor emitted; 1 executable unavailable; 2 usage",
    schema="solwyn.mcp-config.v1",
    guidance=(
        "Use --json for a portable command/args descriptor; adapt it to your host's configuration format.",
        "Defaults to this installation's absolute console executable and the read profile. No host files are written.",
        "Configure credentials separately with solwyn login or host environment variables; generated output never includes a resolved key.",
        "env_vars lists environment variable names (never values) the host must pass to the server: Solwyn's own variables and XDG_DATA_HOME/XDG_CONFIG_HOME, plus DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR when the Linux keyring is the credential store. For Codex, copy them into env_vars in its mcp_servers table.",
    ),
)


def mcp_config(
    context: typer.Context,
    executable: Annotated[
        Path | None, typer.Option(help="Explicit installed Solwyn executable.")
    ] = None,
    profile: Annotated[
        McpProfile, typer.Option(help="Tool profile to include in the descriptor.")
    ] = McpProfile.READ,
) -> None:
    """Print an offline command and argument array for an installed MCP server."""
    output = output_engine(context)
    try:
        command = resolve_executable(executable)
    except ValueError:
        output.write_error(
            OutputError(
                code="mcp_executable_unavailable",
                message="Solwyn executable is missing or not executable.",
                hint="Install the wheel or pass --executable with its absolute console executable.",
            )
        )
        raise typer.Exit(1) from None
    runtime = command_context(context)
    try:
        mode = auth.store_mode()
    except auth.AuthError:
        mode = "auto"
    keyring_on_linux = (
        credential_store.platform_name() == "linux"
        and auth.primary_kind(mode) == "keyring"
    )
    descriptor = launch_descriptor(
        command,
        runtime.project,
        runtime.api_url,
        profile.value,
        forwarded_env_vars(keyring_on_linux=keyring_on_linux),
    )
    output.write_success(
        schema="solwyn.mcp-config.v1",
        data=descriptor,
        table=TableData(
            columns=("Field", "Value"),
            rows=(
                ("command", descriptor.command),
                ("args (JSON)", descriptor.model_dump_json(include={"args"})),
                ("env_vars (JSON)", descriptor.model_dump_json(include={"env_vars"})),
            ),
        ),
    )
