"""Resolved global command context and output construction."""

import os
import sys
from collections.abc import Mapping
from typing import Literal

import typer
from pydantic import BaseModel, ConfigDict

from solwyn_cli.config import Config, ConfigLoadError, load_config
from solwyn_cli.output import OutputEngine, OutputFormat, resolve_output_format


class CommandContext(BaseModel):
    """Non-secret global settings resolved once for a command invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str
    api_url_source: Literal["cli", "environment", "config", "fallback"] = "config"
    config_load_failed: bool = False
    project: str | None
    output_format: OutputFormat
    no_color: bool
    environment_no_color: bool


def resolve_command_context(
    *,
    api_url: str | None,
    project: str | None,
    output_format: str | None,
    json_flag: bool,
    no_color: bool,
    allow_config_error: bool = False,
    environ: Mapping[str, str] | None = None,
) -> CommandContext:
    """Resolve CLI, environment, config, and default precedence."""
    environment = os.environ if environ is None else environ
    config_load_failed = False
    try:
        config = load_config()
    except ConfigLoadError:
        if not allow_config_error:
            raise
        config_load_failed = True
        config = Config()
    environment_api_url = environment.get("SOLWYN_API_URL")
    if api_url:
        resolved_api_url = api_url
        api_url_source: Literal["cli", "environment", "config", "fallback"] = "cli"
    elif environment_api_url:
        resolved_api_url = environment_api_url
        api_url_source = "environment"
    else:
        resolved_api_url = config.api_url
        api_url_source = "fallback" if config_load_failed else "config"
    validated_url = Config(api_url=resolved_api_url).api_url
    return CommandContext(
        api_url=validated_url,
        api_url_source=api_url_source,
        config_load_failed=config_load_failed,
        project=(
            project or environment.get("SOLWYN_PROJECT_ID") or config.default_project
        ),
        output_format=resolve_output_format(
            explicit_format=output_format,
            json_flag=json_flag,
            environ=environment,
            config_format=config.format,
        ),
        no_color=no_color,
        environment_no_color="NO_COLOR" in environment,
    )


def validate_output_intent(
    *,
    output_format: str | None,
    json_flag: bool,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Validate explicit and environment output intent without loading config."""
    environment = os.environ if environ is None else environ
    resolve_output_format(
        explicit_format=output_format,
        json_flag=json_flag,
        environ=environment,
        config_format=None,
    )


def command_context(context: typer.Context) -> CommandContext:
    """Return the callback-populated context for a registered command."""
    if not isinstance(context.obj, CommandContext):
        raise RuntimeError("Command context was not initialized.")
    return context.obj


def output_engine(context: typer.Context) -> OutputEngine:
    """Build an output engine over the invocation's isolated streams."""
    resolved = command_context(context)
    return OutputEngine(
        output_format=resolved.output_format,
        stdout=sys.stdout,
        stderr=sys.stderr,
        stdout_is_tty=sys.stdout.isatty(),
        stderr_is_tty=sys.stderr.isatty(),
        no_color=resolved.no_color,
        environ={"NO_COLOR": ""} if resolved.environment_no_color else {},
    )


def bootstrap_output_engine(
    *,
    output_format: str | None,
    json_flag: bool,
    no_color: bool,
    environ: Mapping[str, str] | None = None,
) -> OutputEngine:
    """Build output before configuration can be safely loaded."""
    environment = os.environ if environ is None else environ
    return OutputEngine(
        output_format=resolve_output_format(
            explicit_format=output_format,
            json_flag=json_flag,
            environ=environment,
            config_format=None,
        ),
        stdout=sys.stdout,
        stderr=sys.stderr,
        stdout_is_tty=sys.stdout.isatty(),
        stderr_is_tty=sys.stderr.isatty(),
        no_color=no_color,
        environ={"NO_COLOR": ""} if "NO_COLOR" in environment else {},
    )
