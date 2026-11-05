"""Safe end-to-end onboarding for a repository."""

import ast
import math
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from solwyn_cli import auth, repo_file
from solwyn_cli._vendor.wire import BudgetMode, BudgetPeriod
from solwyn_cli.approvals import continue_with
from solwyn_cli.artifacts import ArtifactRecoveryError, write_agent_artifacts
from solwyn_cli.auth import (
    ApiKeyCredential,
    AuthError,
    CredentialStorageError,
    LoginCredential,
    ResolvedCredential,
    auth_selection,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import (
    credential_saved_note,
    exit_for_resolution,
    exit_project_required,
    refuse_retired_project_id,
    exit_with_error,
)
from solwyn_cli.commands.budget import BudgetStatusData, fetch_budget_status
from solwyn_cli.commands.budget_rules import build_rule, request_rule_add
from solwyn_cli.commands.login import device_login, store_supplied_key
from solwyn_cli.commands.projects import create_named_project, write_repo_file
from solwyn_cli.config import Config
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, _terminal_safe
from solwyn_cli.project_resolver import ProjectResolutionError
from solwyn_cli.runtime import (
    check_setup,
    command_context,
    output_engine,
    resolve_project,
)

_DEFAULT_API_URL = Config().api_url
_MAX_PROVIDER_SOURCE_FILES = 2_000
_MAX_PROVIDER_SOURCE_BYTES = 1_000_000
_MAX_PROVIDER_TOTAL_BYTES = 10_000_000
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
Provider = Literal["openai", "anthropic", "google-genai", "boto3"]


INIT_HELP = help_contract(
    endpoint="composite onboarding workflow",
    auth="a login (a browser login when none is stored); an API key only for an existing project",
    exits="0 success; 1 generic/API; 2 usage or project mismatch; 3 auth; 5 not found; 6 approval or login pending",
    schema="solwyn.init.v2",
    guidance=(
        "Project: the repo's .solwyn.env, else --project or SOLWYN_PROJECT (the "
        "project must exist), else a new project named after the git repo (-2, "
        "-3, ... when taken) whose key is stored on this computer.",
        "A --project or SOLWYN_PROJECT that disagrees with .solwyn.env exits 2 "
        "before anything is created or written.",
        "Writes .solwyn.env (SOLWYN_PROJECT, no secret, commit it) when there is "
        "none; never overwrites one. No computer-wide configuration is written.",
        "Remote operations: conditional browser login (POST /oauth/device_authorization, "
        "POST /oauth/token); "
        "GET /api/v1/projects; optional POST /api/v1/projects; "
        "final GET /api/v1/projects/{project}/budget; --run-cap adds a rule with "
        "GET/GET/PUT /api/v1/projects/{project}/budget after creation.",
        "Local operations: write .solwyn.env when missing; scan Python imports; "
        "render SDK snippets; optionally write agent artifacts with --agents.",
        "Provider credentials are never collected; --agents writes safe agent guidance.",
    ),
)
_PROVIDER_ORDER: tuple[Provider, ...] = (
    "openai",
    "anthropic",
    "google-genai",
    "boto3",
)


class _Snippet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Provider | Literal["generic"]
    code: str


class _Verification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["verified"] = "verified"
    budget_limit: float = Field(allow_inf_nan=False)
    current_usage: float = Field(allow_inf_nan=False)
    remaining_budget: float = Field(allow_inf_nan=False)
    utilization_pct: float = Field(allow_inf_nan=False)
    budget_mode: str
    budget_period: str


class _InitData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    project_name: str | None
    project_created: bool
    key_stored: bool
    # The repo's .solwyn.env (null outside a git repo) and whether init wrote it.
    repo_file: str | None
    repo_file_written: bool
    api_url: str
    detected_providers: list[Provider]
    snippets: list[_Snippet]
    verification: _Verification


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


_INIT_CONTINUE = "solwyn login --continue, then run solwyn init again"


def _prompt_for_credential(context: typer.Context) -> ResolvedCredential:
    try:
        selection = auth_selection()
    except ValueError:
        selection = None
    if selection == "project_key":
        if not _stdin_is_tty():
            exit_with_error(
                context,
                OutputError(
                    code="authentication_required",
                    message="SOLWYN_AUTH=project_key needs an API key.",
                    hint=(
                        "Set SOLWYN_API_KEY and SOLWYN_PROJECT, or pipe a key to "
                        "solwyn --project PROJECT login --with-key."
                    ),
                ),
                semantic=ErrorSemantic.AUTH,
            )
        return _prompt_for_key(context)
    if selection == "login" or not _stdin_is_tty():
        return device_login(context, continue_command=_INIT_CONTINUE)

    method = (
        typer.prompt(
            "Authentication method (login/key)",
            default="login",
            err=True,
        )
        .strip()
        .lower()
    )
    if method not in {"key", "login"}:
        exit_with_error(
            context,
            OutputError(
                code="invalid_auth_method",
                message="Authentication method must be login or key.",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    if method == "key":
        return _prompt_for_key(context)
    return device_login(context, continue_command=_INIT_CONTINUE)


def _prompt_for_key(context: typer.Context) -> ResolvedCredential:
    api_key = typer.prompt("API key", hide_input=True, err=True)
    return store_supplied_key(context, api_key, None)


def _resolve_auth(context: typer.Context) -> ResolvedCredential:
    try:
        credential = resolve_credential()
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is not None:
        return credential
    return _prompt_for_credential(context)


def _run_cap_requires_creation(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="run_cap_requires_creation",
            message="init --run-cap requires creating a new project.",
            hint="For an existing project, use budget rule add --type runaway_run --spend USD --mode hard_deny.",
        ),
        semantic=ErrorSemantic.USAGE,
    )


def _add_run_cap(
    context: typer.Context, client: SolwynClient, project_id: str, run_cap: float
) -> None:
    # Re-running init cannot add a cap to an existing project, so a pending
    # approval here continues with the rule add itself.
    continue_with(
        [
            "budget",
            "rule",
            "add",
            "--type",
            "runaway_run",
            "--spend",
            str(run_cap),
            "--mode",
            "hard_deny",
        ],
        project=project_id,
        note=f"Project {project_id} was created; this command adds only its run cap.",
    )
    try:
        request_rule_add(
            client=client,
            project_id=project_id,
            rule=build_rule(kind="runaway_run", spend=run_cap, mode="hard_deny"),
        )
    except (ApiError, AuthError, ValueError, TypeError):
        typer.echo(
            _terminal_safe(
                f"Project {project_id} was created; review its budget rules before retrying the run-cap setup."
            ),
            err=True,
        )
        raise
    typer.echo("Per-run hard cap configured: caps every run at min(caps).", err=True)


class _Chosen(BaseModel):
    """The project init uses, and what it did to get there."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_id: str
    project_name: str | None
    created: bool
    key_stored: bool
    credential: Any


def _use_selected(context: typer.Context, credential: ResolvedCredential) -> _Chosen:
    """A selected project must exist; init never creates one from a value."""
    try:
        selection = resolve_project(context, purpose="read", credential=credential)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    if selection.project_id is None:
        exit_project_required(context)
    bound = (
        selection.credential if isinstance(credential, ApiKeyCredential) else credential
    )
    return _Chosen(
        project_id=selection.project_id,
        project_name=selection.project_name,
        created=False,
        key_stored=False,
        credential=bound,
    )


def _create_for_repo(
    context: typer.Context,
    credential: LoginCredential,
    account_id: str,
    fields: dict[str, Any],
    run_cap: float | None,
) -> _Chosen:
    """Nothing selects a project: create one named after the repo, store its key."""
    name = repo_file.default_project_name()
    if name is None:
        exit_project_required(
            context,
            message="Outside a git repo, init needs a project.",
            hint=(
                "Run solwyn init in the repo the project is for, or select an "
                "existing project with --project or SOLWYN_PROJECT."
            ),
        )
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            created = create_named_project(
                client=client, name=name or "", fields=fields, derived=True
            )
            stored = auth.store_api_key(
                project_id=created.id,
                key=created.key.get_secret_value(),
                key_id=created.key_id,
                account_id=account_id,
                project_name=created.name,
                api_url=runtime.api_url,
            )
            if note := credential_saved_note(stored):
                typer.echo(_terminal_safe(note), err=True)
            if run_cap is not None:
                _add_run_cap(context, client, created.id, run_cap)
    except ProjectResolutionError as error:
        exit_for_resolution(context, error)
    except ApiError as error:
        exit_with_error(context, error)
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(
                code="credential_storage_failed",
                message=str(error),
                hint=error.hint or "Retry after fixing the credential store.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_projects_response",
                message="The project creation response was invalid.",
            ),
        )
    return _Chosen(
        project_id=created.id,
        project_name=created.name,
        created=True,
        key_stored=True,
        credential=credential,
    )


def _imports_in(tree: ast.AST) -> tuple[set[Provider], set[Provider]]:
    detected: set[Provider] = set()
    module_imports: set[Provider] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
            if "openai" in names:
                detected.add("openai")
                module_imports.add("openai")
            if "anthropic" in names:
                detected.add("anthropic")
                module_imports.add("anthropic")
            if "boto3" in names:
                detected.add("boto3")
                module_imports.add("boto3")
            if "google.genai" in names:
                detected.add("google-genai")
                module_imports.add("google-genai")
        elif isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            if node.module == "openai":
                detected.add("openai")
            if node.module == "anthropic":
                detected.add("anthropic")
            if node.module == "google" and "genai" in imported:
                detected.add("google-genai")
            if node.module == "google.genai":
                detected.add("google-genai")
            if node.module == "boto3":
                detected.add("boto3")
    return detected, module_imports


def _detect_providers(root: Path) -> tuple[list[Provider], set[Provider]]:
    scan_root = _provider_scan_root(root)
    detected: set[Provider] = set()
    module_imports: set[Provider] = set()
    inspected_files = 0
    inspected_bytes = 0
    for directory, directory_names, file_names in os.walk(scan_root):
        if (Path(directory) / "pyvenv.cfg").is_file():
            directory_names.clear()
            continue
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DIRECTORIES
            and not (Path(directory) / name / ".git").exists()
        )
        for file_name in sorted(file_names):
            if not file_name.endswith(".py"):
                continue
            if inspected_files >= _MAX_PROVIDER_SOURCE_FILES:
                return (
                    [provider for provider in _PROVIDER_ORDER if provider in detected],
                    module_imports,
                )
            inspected_files += 1
            path = Path(directory) / file_name
            try:
                if path.is_symlink():
                    continue
                file_size = path.stat(follow_symlinks=False).st_size
                if file_size > _MAX_PROVIDER_SOURCE_BYTES:
                    continue
                if inspected_bytes + file_size > _MAX_PROVIDER_TOTAL_BYTES:
                    return (
                        [
                            provider
                            for provider in _PROVIDER_ORDER
                            if provider in detected
                        ],
                        module_imports,
                    )
                inspected_bytes += file_size
                source = path.read_bytes()
                if b"\0" in source:
                    continue
                tree = ast.parse(source.decode("utf-8"), filename=str(path))
            except (OSError, UnicodeError, SyntaxError):
                continue
            file_providers, file_module_imports = _imports_in(tree)
            detected.update(file_providers)
            module_imports.update(file_module_imports)
    return (
        [provider for provider in _PROVIDER_ORDER if provider in detected],
        module_imports,
    )


def _provider_scan_root(start: Path) -> Path:
    """Resolve the nearest containing Git root or a bounded cwd fallback."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _snippets(
    providers: list[Provider], module_imports: set[Provider]
) -> list[_Snippet]:
    # Provider keys come from the environment; never invite pasting one into code.
    code = {
        "openai": (
            "import os, openai; from solwyn import Solwyn; "
            'client = Solwyn(openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"]))'
            if "openai" in module_imports
            else "import os; from openai import OpenAI; from solwyn import Solwyn; "
            'client = Solwyn(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))'
        ),
        "anthropic": (
            "import os, anthropic; from solwyn import Solwyn; "
            "client = Solwyn(anthropic.Anthropic("
            'api_key=os.environ["ANTHROPIC_API_KEY"]))'
            if "anthropic" in module_imports
            else "import os; from anthropic import Anthropic; from solwyn import Solwyn; "
            'client = Solwyn(Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]))'
        ),
        "google-genai": (
            "import os; from google import genai; from solwyn import Solwyn; "
            'client = Solwyn(genai.Client(api_key=os.environ["GOOGLE_API_KEY"]))'
        ),
        "boto3": (
            "import boto3; from solwyn import Solwyn; bedrock = boto3.client("
            '"bedrock-runtime", region_name="<AWS_REGION>"); client = Solwyn(bedrock)'
        ),
    }
    if not providers:
        return [
            _Snippet(
                provider="generic",
                code=(
                    "from solwyn import Solwyn; client = Solwyn(<YOUR_PROVIDER_CLIENT>)"
                ),
            )
        ]
    return [_Snippet(provider=provider, code=code[provider]) for provider in providers]


def _record_repo_project(value: str) -> tuple[Path | None, bool]:
    """Write .solwyn.env naming ``value`` when there is none: (path, written)."""
    path = write_repo_file(value)
    if path is not None:
        return path, True
    return repo_file.find_repo_file(), False


def _verify_budget(
    context: typer.Context,
    credential: ResolvedCredential,
    project_id: str,
) -> BudgetStatusData:
    runtime = command_context(context)
    try:
        return fetch_budget_status(
            api_url=runtime.api_url,
            credential=credential,
            project_id=project_id,
        )
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_response",
                message="The budget verification response was invalid.",
            ),
        )


def _table(data: _InitData) -> TableData:
    snippet_text = " | ".join(snippet.code for snippet in data.snippets)
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Project name", data.project_name or "-"),
            ("Created", "yes" if data.project_created else "no"),
            ("Key stored", "yes" if data.key_stored else "no"),
            (
                "Repo file",
                "-"
                if data.repo_file is None
                else f"{data.repo_file} ({'written' if data.repo_file_written else 'kept'})",
            ),
            ("Providers", ", ".join(data.detected_providers) or "none detected"),
            ("SDK wiring", snippet_text),
            ("Budget verification", data.verification.status),
            ("Budget remaining", f"{data.verification.remaining_budget:.2f}"),
            ("Utilization", f"{data.verification.utilization_pct:.2f}%"),
        ),
        right_align=("Value",),
    )


def init(
    context: typer.Context,
    agents: Annotated[
        bool,
        typer.Option(
            "--agents",
            help="Write Claude, Cursor, and AGENTS.md guidance.",
        ),
    ] = False,
    run_cap: Annotated[
        float | None,
        typer.Option(
            "--run-cap",
            help="Create a wildcard per-run hard cap after project creation; caps every run at min(caps). ",
        ),
    ] = None,
    budget: Annotated[
        float,
        typer.Option("--budget", help="Budget in USD when init creates the project."),
    ] = 100.0,
    period: Annotated[
        BudgetPeriod,
        typer.Option("--period", help="Budget period when init creates the project."),
    ] = BudgetPeriod.MONTHLY,
    mode: Annotated[
        BudgetMode,
        typer.Option("--mode", help="Enforcement mode when init creates the project."),
    ] = BudgetMode.ALERT_ONLY,
) -> None:
    """Configure Solwyn safely for the current repository."""
    if run_cap is not None:
        try:
            build_rule(kind="runaway_run", spend=run_cap, mode="hard_deny")
        except ValueError:
            exit_with_error(
                context,
                OutputError(
                    code="invalid_run_cap",
                    message="Run cap must be a positive finite amount in whole cents, at most 1999999.99.",
                ),
                semantic=ErrorSemantic.USAGE,
            )
    if not math.isfinite(budget) or budget <= 0:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_create_request",
                message="The budget must be a finite positive amount.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    fields: dict[str, Any] = {
        "budget_limit": budget,
        "budget_period": period.value,
        "budget_mode": mode.value,
    }
    # A leftover SOLWYN_PROJECT_ID names a project the user meant; never
    # create one in its place.
    refuse_retired_project_id(context)
    credential = _resolve_auth(context)
    # Before any project, key or file is created: the repo file agrees with
    # --project and SOLWYN_PROJECT, and a login's account is known.
    try:
        account_id = check_setup(context, credential)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    runtime = command_context(context)
    if runtime.project_selector is not None:
        if run_cap is not None:
            _run_cap_requires_creation(context)
        chosen = _use_selected(context, credential)
    elif isinstance(credential, LoginCredential):
        chosen = _create_for_repo(
            context, credential, account_id or "", fields, run_cap
        )
    else:
        exit_project_required(
            context,
            message="An API key cannot create a project.",
            hint=(
                "Select the key's project with --project or SOLWYN_PROJECT, or run "
                "solwyn login so init can create one."
            ),
        )
    # The repo file: one that selected the project is already right; a flag or
    # env value (as given) or a new project's name is written when there is
    # none, and an existing file is never overwritten. A new project's name is
    # written at once, so a rerun finds it instead of creating another; a
    # selected project's is written last, after the project answered and
    # every other step succeeded, so a project that could not be used is never
    # recorded.
    repo_path: Path | None = None
    written = False
    if runtime.project_source == "repo":
        repo_path = repo_file.find_repo_file()
    elif chosen.created:
        repo_path, written = _record_repo_project(
            chosen.project_name or chosen.project_id
        )
    providers, module_imports = _detect_providers(Path.cwd())
    snippets = _snippets(providers, module_imports)
    status = _verify_budget(context, chosen.credential, chosen.project_id)
    if agents:
        try:
            write_agent_artifacts(_provider_scan_root(Path.cwd()))
        except ArtifactRecoveryError as error:
            details: list[str] = []
            if error.recovery_paths:
                details.append(
                    "retained backup(s): "
                    + ", ".join(str(path) for path in error.recovery_paths)
                )
            if error.conflict_paths:
                details.append(
                    "preserved concurrent target(s): "
                    + ", ".join(str(path) for path in error.conflict_paths)
                )
            exit_with_error(
                context,
                OutputError(
                    code="agent_artifact_recovery_required",
                    message="Agent guidance could not be fully restored.",
                    hint=("Manual recovery is required; " + "; ".join(details)),
                ),
            )
        except (OSError, UnicodeError, ValueError):
            exit_with_error(
                context,
                OutputError(
                    code="agent_artifact_write_failed",
                    message="Agent guidance could not be written.",
                    hint="Check repository permissions and AGENTS.md markers.",
                ),
            )
    if runtime.project_source != "repo" and not chosen.created:
        repo_path, written = _record_repo_project(
            runtime.project_selector or chosen.project_id
        )
    data = _InitData(
        project_id=chosen.project_id,
        project_name=chosen.project_name,
        project_created=chosen.created,
        key_stored=chosen.key_stored,
        repo_file=None if repo_path is None else str(repo_path),
        repo_file_written=written,
        api_url=runtime.api_url,
        detected_providers=providers,
        snippets=snippets,
        verification=_Verification(
            budget_limit=status.budget_limit,
            current_usage=status.current_usage,
            remaining_budget=status.remaining_budget,
            utilization_pct=status.utilization_pct,
            budget_mode=status.budget_mode.value,
            budget_period=status.budget_period.value,
        ),
    )
    output_engine(context).write_success(
        schema="solwyn.init.v2",
        data=data,
        table=_table(data),
    )
