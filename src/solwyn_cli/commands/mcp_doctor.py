"""Read-only MCP diagnostics using a captured credential and isolated SDK."""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import anyio
import typer
from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli import __version__, auth
from solwyn_cli._mcp_doctor_ipc import HelperResult, PrivateRequest
from solwyn_cli._mcp_doctor_process import run_helper
from solwyn_cli._mcp_launch import Profile, resolve_executable
from solwyn_cli.auth import (
    AuthError,
    LoginCredential,
    ApiKeyCredential,
    ResolvedCredential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import ApiError
from solwyn_cli.mcp.server import get_tool_catalog
from solwyn_cli.output import TableData
from solwyn_cli.runtime import CommandContext, command_context, output_engine


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["ok", "failed", "skipped", "unsupported"]
    detail: str


class CredentialCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal[
        "available", "missing", "unavailable", "incomplete", "invalid", "empty"
    ]
    kind: Literal["project_key", "login"] | None = None
    source: Literal["environment", "keyring", "file"] | None = None


class KeyContext(Check):
    project_id: str | None = None
    scope: Literal["full", "read_only"] | None = None


class ToolConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    discovered: bool
    credential_requirement: Literal["login", "project_key", "either"]
    detail: str


class McpDoctorData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["ok", "degraded"]
    cli_version: str
    config: Check
    executable: Check
    selected_project: str | None
    project_source: Literal[
        "cli", "environment", "repo", "legacy_environment", "config", "none"
    ]
    credential: CredentialCheck
    key_context: KeyContext
    http_read: Check
    profile: Profile
    mcp: HelperResult
    mcp_read: Check
    tools: list[ToolConstraint]
    authority_note: str = "Profiles expose tools only. Full key scope does not promise that a future budget decision will allow spend."


class _KeyResponse(BaseModel):
    """The fields of ``GET /api/v1/auth/key`` this diagnostic reports."""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    project_id: str = Field(pattern=r"^proj_[a-f0-9]{24}$")
    scope: Literal["full", "read_only"]


class _ReadResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    project_id: str


class _AccountResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    user_id: str = Field(min_length=1)


def capture_credential(
    project: str | None = None,
) -> tuple[ResolvedCredential | None, CredentialCheck]:
    """Resolve once; availability reads after absence never select another identity.

    With no login and no SOLWYN_API_KEY, the key stored for the selected
    project is chosen locally (exact id, else its stored name).
    """
    try:
        credential = auth.resolve_credential()
        if credential is None and project is not None:
            from solwyn_cli.project_resolver import (
                ProjectResolutionError,
                _local_match,
            )

            stored = auth.stored_api_keys()
            try:
                entry = _local_match(stored, project) if stored else None
            except ProjectResolutionError:
                entry = None
            if entry is not None:
                credential = auth.stored_key_for_project(entry.project_id)
    except auth.MalformedCredentialFileError:
        # Present but not a valid credential: never silently skipped.
        return None, CredentialCheck(status="incomplete", source=_stored_kind())
    except auth.CredentialStorageError:
        return None, CredentialCheck(status="unavailable", source=_stored_kind())
    except AuthError:
        source: Literal["environment", "keyring", "file"] = (
            "environment" if "SOLWYN_API_KEY" in os.environ else _stored_kind()
        )
        status: Literal["empty", "invalid"] = (
            "empty"
            if source == "environment" and os.environ["SOLWYN_API_KEY"] == ""
            else "invalid"
        )
        return None, CredentialCheck(status=status, kind="project_key", source=source)
    if isinstance(credential, ApiKeyCredential):
        return credential, CredentialCheck(
            status="available", kind="project_key", source=credential.source
        )
    if isinstance(credential, LoginCredential):
        return credential, CredentialCheck(
            status="available", kind="login", source=credential.source
        )
    status_now = auth.stored_entry_status()
    return None, CredentialCheck(
        status="incomplete" if status_now in ("found", "incomplete") else status_now,
        source=_stored_kind(),
    )


def _stored_kind() -> Literal["keyring", "file"]:
    try:
        return auth.where_stored().kind
    except AuthError:
        return "file"


def key_context(client: SolwynClient, credential: ResolvedCredential) -> KeyContext:
    if isinstance(credential, LoginCredential):
        return KeyContext(
            status="skipped",
            detail="A login has no project-key identity or write scope.",
        )
    try:
        response = client.request("GET", "/api/v1/auth/key", retry_rate_limit=False)
        body = _KeyResponse.model_validate(response.json())
        return KeyContext(
            status="ok",
            detail="Identity of the supplied project key.",
            project_id=body.project_id,
            scope=body.scope,
        )
    except ApiError as error:
        if error.status_code == 404:
            return KeyContext(
                status="unsupported",
                detail="Key identity is unavailable on this Core version. Roll out Core's read-only key identity endpoint; scope remains unknown.",
            )
        return KeyContext(
            status="failed",
            detail="Key identity request failed; check credentials and API availability.",
        )
    except Exception:
        return KeyContext(
            status="failed", detail="Key identity response could not be verified."
        )


def http_read(
    client: SolwynClient, credential: ResolvedCredential, project: str | None
) -> Check:
    if project is None and not isinstance(credential, LoginCredential):
        return Check(
            status="skipped",
            detail="Select a project with --project, SOLWYN_PROJECT_ID or config to verify project reads.",
        )
    try:
        endpoint = (
            f"/api/v1/projects/{quote(project, safe='')}/budget"
            if project
            else "/api/v1/account/me"
        )
        response = client.request("GET", endpoint, retry_rate_limit=False)
        if project:
            if _ReadResponse.model_validate(response.json()).project_id != project:
                raise ValueError
        else:
            _AccountResponse.model_validate(response.json())
        return Check(
            status="ok",
            detail="Selected project HTTP read verified."
            if project
            else "Captured login account HTTP read verified; no project selected.",
        )
    except Exception:
        return Check(
            status="failed",
            detail="HTTP read failed; check the selected project, credential and API availability.",
        )


def _read_project_id(
    runtime: CommandContext, credential: ResolvedCredential, context: KeyContext
) -> str | None:
    """The proj_ id the project read uses: URLs carry ids, never names.

    A key's id is the one /auth/key just answered for the selector; a login
    turns a name into an id through its projects. Unresolvable names stay as
    given, so the read fails and says so.
    """
    from solwyn_cli._vendor.validation import canonical_project_id, is_project_id
    from solwyn_cli import project_resolver

    selected = runtime.project
    if selected is None or is_project_id(selected):
        return None if selected is None else canonical_project_id(selected)
    if isinstance(credential, ApiKeyCredential):
        return context.project_id or selected
    try:
        return project_resolver.resolve(
            project_resolver.Candidates(selected=selected, source="cli", repo=None),
            purpose="read",
            api_url=runtime.api_url,
            credential=credential,
        ).project_id
    except Exception:  # noqa: BLE001 - a diagnostic reports, never raises.
        return selected


def diagnose(
    runtime: CommandContext, executable: Path | None, profile: Profile
) -> McpDoctorData:
    credential, credential_check = capture_credential(runtime.project)
    context = KeyContext(
        status="skipped", detail="No valid captured credential or API URL is available."
    )
    read = Check(
        status="skipped", detail="No valid captured credential or API URL is available."
    )
    if (
        isinstance(credential, ApiKeyCredential)
        and credential.project is None
        and runtime.project is not None
    ):
        # Every key request names its project: the selector as given.
        credential = credential.model_copy(update={"project": runtime.project})
    if credential is not None and runtime.api_url_source != "fallback":
        context = KeyContext(
            status="skipped" if isinstance(credential, LoginCredential) else "failed",
            detail="A login has no project-key identity or write scope."
            if isinstance(credential, LoginCredential)
            else "Key identity could not be checked; HTTP client setup failed.",
        )
        try:
            with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
                context = key_context(client, credential)
                read = http_read(
                    client,
                    credential,
                    _read_project_id(runtime, credential, context),
                )
        except Exception:
            read = Check(
                status="failed",
                detail="HTTP client setup or cleanup failed; check proxy and certificate configuration.",
            )
    declarations = get_tool_catalog(profile=profile)
    helper = HelperResult()
    executable_check = Check(status="ok", detail="Installed executable is available.")
    try:
        command = resolve_executable(executable)
    except ValueError:
        executable_check = Check(
            status="failed",
            detail="Solwyn executable is missing or not executable; install the wheel or pass --executable.",
        )
    else:
        try:
            request = PrivateRequest(
                command=command,
                project=runtime.project,
                api_url=runtime.api_url,
                profile=profile,
                expected_tools=[tool.definition.name for tool in declarations],
                expected_version=__version__,
                api_key=credential.api_key
                if isinstance(credential, ApiKeyCredential)
                and runtime.api_url_source != "fallback"
                else None,
            )
            helper = anyio.run(run_helper, request)
        except Exception:
            helper = HelperResult(status="failed")
    if isinstance(credential, LoginCredential):
        mcp_read = Check(
            status="skipped",
            detail="A captured login cannot be forwarded through the supported MCP child environment. Discovery only; HTTP read is reported separately.",
        )
    elif helper.read == "ok":
        mcp_read = Check(
            status="ok",
            detail="Read-only MCP budget_status verified with the captured project key.",
        )
    elif helper.read == "failed":
        mcp_read = Check(
            status="failed", detail="Read-only MCP budget_status could not be verified."
        )
    else:
        mcp_read = Check(
            status="skipped",
            detail="MCP read requires a captured project key, selected project, valid API URL and matching server discovery.",
        )
    config = Check(
        status="failed" if runtime.config_load_failed else "ok",
        detail="Configuration could not be loaded."
        if runtime.config_load_failed
        else "Configuration resolved.",
    )
    complete = (
        config.status == "ok"
        and executable_check.status == "ok"
        and credential_check.status == "available"
        and read.status == "ok"
        and helper.status == "ok"
        and (context.status == "ok" and mcp_read.status == "ok")
    )
    return McpDoctorData(
        status="ok" if complete else "degraded",
        cli_version=__version__,
        config=config,
        executable=executable_check,
        selected_project=runtime.project,
        project_source=runtime.project_source,
        credential=credential_check,
        key_context=context,
        http_read=read,
        profile=profile,
        mcp=helper,
        mcp_read=mcp_read,
        tools=[
            ToolConstraint(
                name=tool.definition.name,
                discovered=tool.definition.name in helper.tools,
                credential_requirement=tool.credential_requirement,
                detail=tool.credential_note,
            )
            for tool in declarations
        ],
    )


def doctor_mcp(
    context: typer.Context, executable: Path | None, profile: Profile
) -> None:
    data = diagnose(command_context(context), executable, profile)
    rows = [
        ("Overall", data.status),
        ("Profile / inventory", f"{data.profile} / {data.mcp.inventory}"),
        (
            "Server / version / protocol",
            f"{data.mcp.identity} / {data.mcp.version} / {data.mcp.protocol or 'unknown'}",
        ),
        (
            "Selected read project / source",
            f"{data.selected_project or 'none'} / {data.project_source}",
        ),
        (
            "Credential",
            f"{data.credential.status} / {data.credential.kind or 'none'} / {data.credential.source or 'none'}",
        ),
        (
            "Supplied key project / scope",
            f"{data.key_context.project_id or 'unknown'} / {data.key_context.scope or 'unknown'}",
        ),
        ("Key context", data.key_context.detail),
        ("HTTP read", f"{data.http_read.status}: {data.http_read.detail}"),
        ("MCP discovery", data.mcp.status),
        ("MCP read", f"{data.mcp_read.status}: {data.mcp_read.detail}"),
        ("Config", data.config.detail),
        ("Executable", data.executable.detail),
        ("Authority", data.authority_note),
    ]
    rows.extend(
        (tool.name, f"{'discovered' if tool.discovered else 'missing'}: {tool.detail}")
        for tool in data.tools
    )
    output_engine(context).write_success(
        schema="solwyn.doctor.mcp.v1",
        data=data,
        table=TableData(columns=("Check", "Result"), rows=tuple(rows), fold_cells=True),
    )
    if data.status != "ok":
        raise typer.Exit(1)
