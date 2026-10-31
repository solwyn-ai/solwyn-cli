"""Safe end-to-end onboarding for a repository."""

import ast
import math
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from solwyn_cli._vendor.wire import ProjectCreateRequest
from solwyn_cli.artifacts import ArtifactRecoveryError, write_agent_artifacts
from solwyn_cli.approvals import add_retry_note, continue_with
from solwyn_cli.auth import (
    AuthError,
    auth_selection,
    CredentialStorageError,
    LoginCredential,
    ProjectKeyCredential,
    ResolvedCredential,
    resolve_credential,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import credential_saved_note, exit_with_error
from solwyn_cli.commands.budget import BudgetStatusData, fetch_budget_status
from solwyn_cli.commands.budget_rules import build_rule, request_rule_add
from solwyn_cli.commands.login import (
    authenticate_with_project_key,
    device_login,
)
from solwyn_cli.commands.projects import (
    fetch_projects,
    request_project_creation,
)
from solwyn_cli.config import (
    Config,
    ConfigLoadError,
    ConfigWriteError,
    get_config_path,
    update_config_values,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, _terminal_safe
from solwyn_cli.runtime import command_context, output_engine


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
    auth="any, or a browser login when none is stored",
    exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found; 6 approval or login pending",
    schema="solwyn.init.v1",
    guidance=(
        "Remote operations: conditional browser login (POST /oauth/device_authorization, "
        "POST /oauth/token); "
        "GET /api/v1/projects; optional POST /api/v1/projects; "
        "final GET /api/v1/projects/{project}/budget; --run-cap adds a rule with "
        "GET/GET/PUT /api/v1/projects/{project}/budget after creation.",
        "Local operations: write local configuration; scan Python imports; "
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
    config_path: str
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
                    message="SOLWYN_AUTH=project_key needs a project key.",
                    hint="Set SOLWYN_API_KEY, or pipe a key to solwyn login --with-key.",
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
    project_key = typer.prompt("Project key", hide_input=True, err=True)
    try:
        return authenticate_with_project_key(project_key)
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(code="credential_storage_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except AuthError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_key",
                message="The project key is empty or malformed.",
            ),
            semantic=ErrorSemantic.AUTH,
        )


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


def _create_project(
    context: typer.Context,
    client: SolwynClient,
    run_cap: float | None = None,
) -> tuple[str, ResolvedCredential | None]:
    name = typer.prompt("Project name", err=True).strip()
    budget = typer.prompt("Initial monthly budget", type=float, default=100.0, err=True)
    add_retry_note("Enter the same project name and budget again.")
    if not name or not math.isfinite(budget) or budget <= 0:
        exit_with_error(
            context,
            OutputError(
                code="invalid_project_create_request",
                message="Project name and budget are invalid.",
                hint="Provide a nonblank name and a finite positive budget.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        request = ProjectCreateRequest(
            name=name,
            budget_limit=budget,
            budget_period="monthly",
            budget_mode="alert_only",
        )
        created = request_project_creation(
            client=client,
            request=request,
        )
        stored_credential = store_project_key(created.key.get_secret_value())
        if isinstance(stored_credential, ProjectKeyCredential):
            if note := credential_saved_note(stored_credential):
                typer.echo(_terminal_safe(note), err=True)
        if run_cap is not None:
            # Re-running init cannot add a cap to an existing project, so a
            # pending approval here continues with the rule add itself.
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
                project=created.id,
                note=f"Project {created.id} was created; this command adds only its run cap.",
            )
            try:
                request_rule_add(
                    client=client,
                    project_id=created.id,
                    rule=build_rule(
                        kind="runaway_run", spend=run_cap, mode="hard_deny"
                    ),
                )
            except (ApiError, AuthError, ValueError, TypeError):
                typer.echo(
                    _terminal_safe(
                        f"Project {created.id} was created; review its budget rules before retrying the run-cap setup."
                    ),
                    err=True,
                )
                raise
            typer.echo(
                "Per-run hard cap configured: caps every run at min(caps).", err=True
            )
    except ApiError as error:
        exit_with_error(context, error)
    except CredentialStorageError as error:
        exit_with_error(
            context,
            OutputError(code="credential_storage_failed", message=str(error)),
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
    return created.id, (
        stored_credential
        if isinstance(stored_credential, ProjectKeyCredential)
        else None
    )


def _resolve_login_project(
    context: typer.Context,
    credential: LoginCredential,
    explicit_project: str | None,
    run_cap: float | None = None,
) -> tuple[str, ResolvedCredential]:
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            projects = fetch_projects(client=client)
            by_id = {project.id: project for project in projects}
            if run_cap is not None and (
                explicit_project is not None or len(projects) == 1
            ):
                _run_cap_requires_creation(context)
            if explicit_project is not None:
                if explicit_project not in by_id:
                    exit_with_error(
                        context,
                        OutputError(
                            code="project_not_found",
                            message="The selected project is not available.",
                            hint="Choose a project returned by solwyn projects list.",
                        ),
                        semantic=ErrorSemantic.USAGE,
                    )
                return explicit_project, client.current_credential
            if len(projects) == 1:
                return projects[0].id, client.current_credential
            if not _stdin_is_tty():
                if not projects:
                    _project_creation_required(context)
                exit_with_error(
                    context,
                    OutputError(
                        code="project_required",
                        message="A project must be selected for initialization.",
                        hint="Choose one with --project or SOLWYN_PROJECT_ID.",
                    ),
                    semantic=ErrorSemantic.USAGE,
                )
            if not projects:
                # The new key is stored for the SDK; init keeps using the login.
                project_id, _ = _create_project(context, client, run_cap)
                return project_id, client.current_credential

            project_ids = _terminal_safe(", ".join(project.id for project in projects))
            selection = typer.prompt(
                f"Project ({project_ids}, or create)",
                err=True,
            ).strip()
            if selection == "create":
                # The new key is stored for the SDK; init keeps using the login.
                project_id, _ = _create_project(context, client, run_cap)
                return project_id, client.current_credential
            if selection not in by_id:
                exit_with_error(
                    context,
                    OutputError(
                        code="project_not_found",
                        message="The selected project is not available.",
                        hint="Choose a listed project ID or enter create.",
                    ),
                    semantic=ErrorSemantic.USAGE,
                )
            if run_cap is not None:
                _run_cap_requires_creation(context)
            return selection, client.current_credential
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
                code="invalid_projects_response",
                message="The projects response was invalid.",
            ),
        )


def _project_creation_required(context: typer.Context) -> None:
    create = "solwyn projects create --name <name> --store-key"
    rerun = "solwyn --project <project_id> init"
    if context.params.get("agents"):
        rerun += " --agents"
    exit_with_error(
        context,
        OutputError(
            code="project_required",
            message="This account has no project to initialize.",
            hint=f"Create one with: {create}. Then run: {rerun}",
            detail={"next_commands": [create, rerun]},
        ),
        semantic=ErrorSemantic.USAGE,
    )


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


def _resolve_project(
    context: typer.Context,
    credential: ResolvedCredential,
    run_cap: float | None = None,
) -> tuple[str, ResolvedCredential]:
    selected = command_context(context).project
    if isinstance(credential, ProjectKeyCredential):
        if run_cap is not None:
            _run_cap_requires_creation(context)
        if selected is None:
            exit_with_error(
                context,
                OutputError(
                    code="project_required",
                    message="A project key cannot discover its project yet.",
                    hint="Choose the key's project with --project.",
                ),
                semantic=ErrorSemantic.USAGE,
            )
        return selected, credential
    return _resolve_login_project(context, credential, selected, run_cap)


def _write_config(api_url: str, project_id: str) -> Path:
    update_config_values(
        {
            "default_project": project_id,
            "api_url": api_url if api_url != _DEFAULT_API_URL else _DEFAULT_API_URL,
        },
        omit_default_api_url=True,
    )
    return get_config_path()


def _persist_config(context: typer.Context, api_url: str, project_id: str) -> Path:
    try:
        return _write_config(api_url, project_id)
    except ConfigLoadError:
        exit_with_error(
            context,
            OutputError(
                code="config_load_failed",
                message="The existing configuration could not be loaded.",
                hint="Repair or remove the configuration file, then retry init.",
            ),
        )
    except ConfigWriteError:
        exit_with_error(
            context,
            OutputError(
                code="config_write_failed",
                message="The configuration could not be written.",
                hint="Check configuration directory permissions, then retry init.",
            ),
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
            ("Configuration", "written"),
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
    credential = _resolve_auth(context)
    project_id, credential = _resolve_project(context, credential, run_cap)
    runtime = command_context(context)
    config_path = _persist_config(context, runtime.api_url, project_id)
    providers, module_imports = _detect_providers(Path.cwd())
    snippets = _snippets(providers, module_imports)
    budget = _verify_budget(context, credential, project_id)
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
    data = _InitData(
        project_id=project_id,
        config_path=str(config_path),
        api_url=runtime.api_url,
        detected_providers=providers,
        snippets=snippets,
        verification=_Verification(
            budget_limit=budget.budget_limit,
            current_usage=budget.current_usage,
            remaining_budget=budget.remaining_budget,
            utilization_pct=budget.utilization_pct,
            budget_mode=budget.budget_mode.value,
            budget_period=budget.budget_period.value,
        ),
    )
    output_engine(context).write_success(
        schema="solwyn.init.v1",
        data=data,
        table=_table(data),
    )
