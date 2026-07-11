"""Safe end-to-end onboarding for a repository."""

import ast
import math
import os
import sys
from pathlib import Path
from typing import Literal

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from solwyn_cli._vendor.wire import ProjectCreateRequest
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    JwtCredential,
    ProjectKeyCredential,
    ResolvedCredential,
    resolve_credential,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.commands.budget import BudgetStatusData, fetch_budget_status
from solwyn_cli.commands.login import (
    authenticate_with_password,
    authenticate_with_project_key,
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
    load_config,
    save_config,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


_DEFAULT_API_URL = Config().api_url
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


def _authentication_required(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="interactive_auth_required",
            message="Authentication is required to initialize Solwyn.",
            hint="Set SOLWYN_API_KEY or rerun init in an interactive terminal.",
        ),
        semantic=ErrorSemantic.AUTH,
    )


def _prompt_for_credential(context: typer.Context) -> ResolvedCredential:
    if not _stdin_is_tty():
        _authentication_required(context)

    method = (
        typer.prompt(
            "Authentication method (key/login)",
            default="key",
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
                message="Authentication method must be key or login.",
            ),
            semantic=ErrorSemantic.USAGE,
        )

    if method == "key":
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

    email = typer.prompt("Email", err=True)
    password = typer.prompt("Password", hide_input=True, err=True)
    runtime = command_context(context)
    try:
        return authenticate_with_password(
            api_url=runtime.api_url,
            email=email,
            password=password,
        )
    except ApiError as error:
        exit_with_error(context, error, semantic=ErrorSemantic.AUTH)
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
                code="invalid_auth_response",
                message="The authentication response was invalid.",
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
) -> tuple[str, ResolvedCredential | None]:
    name = typer.prompt("Project name", err=True).strip()
    budget = typer.prompt("Initial monthly budget", type=float, default=100.0, err=True)
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


def _resolve_jwt_project(
    context: typer.Context,
    credential: JwtCredential,
    explicit_project: str | None,
) -> tuple[str, ResolvedCredential]:
    runtime = command_context(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            projects = fetch_projects(client=client)
            by_id = {project.id: project for project in projects}
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
                return explicit_project, credential
            if len(projects) == 1:
                return projects[0].id, credential
            if not _stdin_is_tty():
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
                project_id, stored = _create_project(context, client)
                return project_id, stored or credential

            project_ids = ", ".join(project.id for project in projects)
            selection = typer.prompt(
                f"Project ({project_ids}, or create)",
                err=True,
            ).strip()
            if selection == "create":
                project_id, stored = _create_project(context, client)
                return project_id, stored or credential
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
            return selection, credential
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


def _resolve_project(
    context: typer.Context,
    credential: ResolvedCredential,
) -> tuple[str, ResolvedCredential]:
    selected = command_context(context).project
    if isinstance(credential, ProjectKeyCredential):
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
    return _resolve_jwt_project(context, credential, selected)


def _write_config(api_url: str, project_id: str) -> Path:
    current = load_config()
    updated = current.model_copy(
        update={
            "default_project": project_id,
            "api_url": api_url if api_url != _DEFAULT_API_URL else _DEFAULT_API_URL,
        }
    )
    save_config(
        Config.model_validate(updated.model_dump()),
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
    detected: set[Provider] = set()
    module_imports: set[Provider] = set()
    for directory, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(
            name for name in directory_names if name not in _IGNORED_DIRECTORIES
        )
        for file_name in sorted(file_names):
            if not file_name.endswith(".py"):
                continue
            path = Path(directory) / file_name
            try:
                if path.is_symlink():
                    continue
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


def _snippets(
    providers: list[Provider], module_imports: set[Provider]
) -> list[_Snippet]:
    code = {
        "openai": (
            "import openai; from solwyn import Solwyn; "
            'client = Solwyn(openai.OpenAI(api_key="<OPENAI_API_KEY>"))'
            if "openai" in module_imports
            else "from openai import OpenAI; from solwyn import Solwyn; "
            'client = Solwyn(OpenAI(api_key="<OPENAI_API_KEY>"))'
        ),
        "anthropic": (
            "import anthropic; from solwyn import Solwyn; "
            'client = Solwyn(anthropic.Anthropic(api_key="<ANTHROPIC_API_KEY>"))'
            if "anthropic" in module_imports
            else "from anthropic import Anthropic; from solwyn import Solwyn; "
            'client = Solwyn(Anthropic(api_key="<ANTHROPIC_API_KEY>"))'
        ),
        "google-genai": (
            "from google import genai; from solwyn import Solwyn; "
            'client = Solwyn(genai.Client(api_key="<GOOGLE_API_KEY>"))'
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


def init(context: typer.Context) -> None:
    """Configure Solwyn safely for the current repository."""
    credential = _resolve_auth(context)
    project_id, credential = _resolve_project(context, credential)
    runtime = command_context(context)
    config_path = _persist_config(context, runtime.api_url, project_id)
    providers, module_imports = _detect_providers(Path.cwd())
    snippets = _snippets(providers, module_imports)
    budget = _verify_budget(context, credential, project_id)
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
