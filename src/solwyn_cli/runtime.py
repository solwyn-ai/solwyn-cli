"""Resolved global command context and output construction."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer
from pydantic import BaseModel, ConfigDict

from solwyn_cli.config import DEFAULT_API_URL, Config, ConfigLoadError, load_config
from solwyn_cli.output import OutputEngine, OutputFormat, resolve_output_format

if TYPE_CHECKING:
    from solwyn_cli.auth import ApiKeyCredential, ResolvedCredential
    from solwyn_cli.project_resolver import Candidates, ProjectSelection, Purpose


ApiUrlSource = Literal["cli", "environment", "config", "default", "fallback"]

# How each source is named to people (doctor, config list).
API_URL_SOURCE_LABELS: dict[str, str] = {
    "cli": "from --api-url",
    "environment": "from SOLWYN_API_URL",
    "config": "from config",
    "default": "default",
    "fallback": "default; config could not be loaded",
}


def resolve_api_url(
    *,
    flag: str | None,
    environ: Mapping[str, str],
    config: Config,
    config_load_failed: bool = False,
) -> tuple[str, ApiUrlSource]:
    """Return the effective API URL and its source: flag, environment, config.

    ``default`` means nothing chose a URL (or config holds the default one);
    ``fallback`` means config could not be loaded, so the default applies.
    """
    environment_api_url = environ.get("SOLWYN_API_URL")
    if flag:
        url, source = flag, "cli"
    elif environment_api_url:
        url, source = environment_api_url, "environment"
    elif config_load_failed:
        url, source = config.api_url, "fallback"
    else:
        url = config.api_url
        source = "default" if url == DEFAULT_API_URL else "config"
    return Config(api_url=url).api_url, source  # type: ignore[return-value]


class CommandContext(BaseModel):
    """Non-secret global settings resolved once for a command invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str
    api_url_source: ApiUrlSource = "config"
    config_load_failed: bool = False
    # The selected project: as given until the resolver turns it into the
    # proj_ id (then ``project_selector`` keeps the value as given).
    project: str | None
    project_source: Literal["cli", "environment", "repo", "none"] = "none"
    project_selector: str | None = None
    project_resolved: bool = False
    # SOLWYN_PROJECT from the repo's .solwyn.env, for the mismatch check.
    repo_project: str | None = None
    repo_error: str | None = None
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
    cwd: Path | None = None,
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
    validated_url, api_url_source = resolve_api_url(
        flag=api_url,
        environ=environment,
        config=config,
        config_load_failed=config_load_failed,
    )
    selected, source, repo_project, repo_error = project_candidates(
        flag=project, environ=environment, config=config, cwd=cwd
    )
    return CommandContext(
        api_url=validated_url,
        api_url_source=api_url_source,
        config_load_failed=config_load_failed,
        project=selected,
        project_source=source,
        project_selector=selected,
        repo_project=repo_project,
        repo_error=repo_error,
        output_format=resolve_output_format(
            explicit_format=output_format,
            json_flag=json_flag,
            environ=environment,
            config_format=config.format,
        ),
        no_color=no_color,
        environment_no_color="NO_COLOR" in environment,
    )


ProjectSource = Literal["cli", "environment", "repo", "none"]


def project_candidates(
    *,
    flag: str | None,
    environ: Mapping[str, str],
    config: Config,
    cwd: Path | None = None,
) -> tuple[str | None, ProjectSource, str | None, str | None]:
    """(selected, source, repo value, repo file error), first present wins.

    ``--project`` > ``SOLWYN_PROJECT`` > the repo's ``.solwyn.env``. There is
    no computer-wide default: ``SOLWYN_PROJECT_ID`` and config
    ``default_project`` are gone (a leftover SOLWYN_PROJECT_ID is named when a
    command needs a project and nothing selects one).
    """
    from solwyn_cli._vendor.validation import trim_selector
    from solwyn_cli.repo_file import read_repo_project

    def present(value: str | None) -> str | None:
        trimmed = trim_selector(value) if value is not None else ""
        return trimmed or None

    repo, repo_error = read_repo_project(cwd)
    for value, source in (
        (flag, "cli"),
        (environ.get("SOLWYN_PROJECT"), "environment"),
        (repo, "repo"),
    ):
        if (chosen := present(value)) is not None:
            return chosen, source, repo, repo_error  # type: ignore[return-value]
    return None, "none", repo, repo_error


def candidates_of(resolved: CommandContext) -> "Candidates":
    """The resolver's view of this invocation's project candidates."""
    from solwyn_cli.project_resolver import Candidates

    return Candidates(
        selected=resolved.project_selector,
        source=resolved.project_source,
        repo=resolved.repo_project,
        repo_error=resolved.repo_error,
    )


def resolve_project(
    context: typer.Context,
    *,
    purpose: "Purpose",
    credential: "ResolvedCredential | None" = None,
    supplied_key: "ApiKeyCredential | None" = None,
) -> "ProjectSelection":
    """Run the one project resolver for a command; record the resolved id.

    After it returns, ``command_context(context).project`` is the ``proj_``
    id (``project_selector`` keeps the value as given). A read prints a
    mismatch with the repo file once, as a warning; other purposes fail.
    """
    from solwyn_cli import project_resolver

    resolved = command_context(context)
    selection = project_resolver.resolve(
        candidates_of(resolved),
        purpose=purpose,
        api_url=resolved.api_url,
        credential=credential,
        supplied_key=supplied_key,
    )
    if selection.warning is not None and not resolved.project_resolved:
        from solwyn_cli.output import _terminal_safe

        typer.echo(_terminal_safe(f"Warning: {selection.warning}"), err=True)
    if selection.project_id is not None:
        context.obj = resolved.model_copy(
            update={"project": selection.project_id, "project_resolved": True}
        )
    return selection


def check_setup(context: typer.Context, credential: "ResolvedCredential") -> str | None:
    """Run the shared setup checks (repo file, mismatch, login account) for a
    command that creates projects or keys or writes files; returns the login's
    account id when a login is used."""
    from solwyn_cli import project_resolver

    resolved = command_context(context)
    return project_resolver.check_setup(
        candidates_of(resolved), api_url=resolved.api_url, credential=credential
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
