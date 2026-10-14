"""Local and remote diagnostics for the configured Solwyn environment."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli import auth as auth_module
from solwyn_cli import config as config_module
from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    ResolvedCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands.mcp import McpProfile
from solwyn_cli.commands.mcp_doctor import doctor_mcp
from solwyn_cli.errors import ApiError
from solwyn_cli.help import help_contract
from solwyn_cli.output import TableData
from solwyn_cli.runtime import command_context, output_engine


DOCTOR_HELP = help_contract(
    endpoint="GET /health/ready plus one credential-specific authenticated probe",
    auth="optional; JWT or project key enables the authenticated probe",
    exits="0 all checks pass; 1 diagnostic failure; 2 usage",
    schema="solwyn.doctor.v1; with --mcp: solwyn.doctor.mcp.v1",
    guidance=(
        "Use --mcp for isolated discovery and captured-credential read diagnostics. No refresh or mutation. --profile defaults to read; --executable selects the installed server.",
    ),
)

_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "spec" / "openapi_snapshot.json"
_CORE_REF = re.compile(r"[0-9a-f]{40}")
_MAX_SNAPSHOT_AGE_DAYS = 30


class _Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "error", "skipped"]
    detail: str


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["ok", "degraded", "skipped"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    service: str
    checks: list[ReadinessCheck]


class HealthResponse(BaseModel):
    """Pinned liveness shape; Core intentionally exposes no version field."""

    model_config = ConfigDict(extra="forbid")

    status: str
    service: str


class _ReadinessCheckDiagnostic(ReadinessCheck):
    gating: bool


class _ReadinessDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    http_status: int | None
    service: str | None
    checks: list[_ReadinessCheckDiagnostic]


class _AuthenticatedProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "skipped"]
    credential_type: Literal["jwt", "project_key"] | None
    endpoint: str | None
    detail: str


class _SnapshotDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    synced_on: str | None
    age_days: int | None
    core_ref: str | None


class _DoctorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    config: _Diagnostic
    keychain: _Diagnostic
    readiness: _ReadinessDiagnostic
    authenticated_probe: _AuthenticatedProbe
    snapshot: _SnapshotDiagnostic


class _MeProbeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: str


class _BudgetProbeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str


def _today() -> date:
    return datetime.now(UTC).date()


def _load_snapshot() -> dict[str, object]:
    value = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("snapshot root must be an object")
    return value


def _config_diagnostic() -> _Diagnostic:
    path = config_module.get_config_path()
    if not path.exists():
        return _Diagnostic(
            status="ok",
            detail="No config file; using defaults.",
        )
    try:
        config_module.load_config()
    except config_module.ConfigLoadError:
        return _Diagnostic(
            status="error",
            detail="Configuration could not be loaded.",
        )
    return _Diagnostic(status="ok", detail="Configuration loaded.")


def _keychain_diagnostic() -> _Diagnostic:
    entries = (
        auth_module.API_KEY_ENTRY,
        auth_module.ACCESS_TOKEN_ENTRY,
        auth_module.REFRESH_TOKEN_ENTRY,
    )
    results = [auth_module._get_password(entry) for entry in entries]
    if not all(available for available, _value in results):
        return _Diagnostic(
            status="error",
            detail="Keychain is unavailable.",
        )
    project_key = results[0][1]
    access_token = results[1][1]
    refresh_token = results[2][1]
    if (access_token is None) != (refresh_token is None):
        return _Diagnostic(
            status="degraded",
            detail="Keychain contains an incomplete JWT credential.",
        )
    stored = project_key is not None or access_token is not None
    return _Diagnostic(
        status="ok",
        detail=(
            "Keychain available; stored credential found."
            if stored
            else "Keychain available; no stored credential found."
        ),
    )


def _readiness_diagnostic(client: SolwynClient) -> _ReadinessDiagnostic:
    try:
        response = client.request_unauthenticated(
            "GET",
            "/health/ready",
            accepted_statuses=frozenset({503}),
        )
        readiness = ReadinessResponse.model_validate(response.json())
    except ApiError as error:
        return _ReadinessDiagnostic(
            status="degraded",
            http_status=error.status_code,
            service=None,
            checks=[
                _ReadinessCheckDiagnostic(
                    name="readiness",
                    status="degraded",
                    detail=error.message,
                    gating=True,
                )
            ],
        )
    except (ValidationError, ValueError, TypeError):
        return _ReadinessDiagnostic(
            status="degraded",
            http_status=None,
            service=None,
            checks=[
                _ReadinessCheckDiagnostic(
                    name="readiness",
                    status="degraded",
                    detail="Readiness response was invalid.",
                    gating=True,
                )
            ],
        )

    checks = [
        _ReadinessCheckDiagnostic(
            name=check.name,
            status=check.status,
            detail=check.detail,
            gating=check.name not in {"redis", "stripe_catalog", "stripe_portal"},
        )
        for check in readiness.checks
    ]
    return _ReadinessDiagnostic(
        status=readiness.status,
        http_status=response.status_code,
        service=readiness.service,
        checks=checks,
    )


def _authenticated_probe(
    *,
    client: SolwynClient,
    credential: ResolvedCredential | None,
    credential_error: str | None,
    project_id: str | None,
    endpoint_available: bool,
) -> _AuthenticatedProbe:
    if not endpoint_available:
        credential_type: Literal["jwt", "project_key"] | None = None
        if isinstance(credential, JwtCredential):
            credential_type = "jwt"
        elif isinstance(credential, ProjectKeyCredential):
            credential_type = "project_key"
        return _AuthenticatedProbe(
            status="skipped",
            credential_type=credential_type,
            endpoint=None,
            detail=(
                "Authenticated probe skipped because the API endpoint could not be "
                "loaded; provide --api-url or SOLWYN_API_URL."
            ),
        )
    if credential_error is not None:
        return _AuthenticatedProbe(
            status="degraded",
            credential_type=None,
            endpoint=None,
            detail=credential_error,
        )
    if credential is None:
        return _AuthenticatedProbe(
            status="skipped",
            credential_type=None,
            endpoint=None,
            detail="No credential configured.",
        )
    if isinstance(credential, ProjectKeyCredential) and project_id is None:
        return _AuthenticatedProbe(
            status="skipped",
            credential_type="project_key",
            endpoint=None,
            detail="A default project is required to test the project key.",
        )

    credential_type: Literal["jwt", "project_key"]
    if isinstance(credential, JwtCredential):
        credential_type = "jwt"
        endpoint = "/api/v1/account/me"
    else:
        credential_type = "project_key"
        endpoint = f"/api/v1/projects/{project_id}/budget"

    try:
        response = client.request("GET", endpoint)
        if credential_type == "jwt":
            _MeProbeResponse.model_validate(response.json())
            detail = "JWT credential accepted."
        else:
            budget = _BudgetProbeResponse.model_validate(response.json())
            if budget.project_id != project_id:
                raise ValueError("project mismatch")
            detail = "Project budget readable."
    except (ApiError, AuthError) as error:
        return _AuthenticatedProbe(
            status="degraded",
            credential_type=credential_type,
            endpoint=endpoint,
            detail=(error.message if isinstance(error, ApiError) else str(error)),
        )
    except (ValidationError, ValueError, TypeError):
        return _AuthenticatedProbe(
            status="degraded",
            credential_type=credential_type,
            endpoint=endpoint,
            detail="Authenticated probe returned an invalid response.",
        )
    return _AuthenticatedProbe(
        status="ok",
        credential_type=credential_type,
        endpoint=endpoint,
        detail=detail,
    )


def _snapshot_diagnostic(
    snapshot: dict[str, object], *, today: date
) -> _SnapshotDiagnostic:
    raw_date = snapshot.get("x-solwyn-snapshot-date")
    synced_on: str | None = raw_date if isinstance(raw_date, str) else None
    age_days: int | None = None
    valid_date = False
    if synced_on is not None:
        try:
            parsed_date = date.fromisoformat(synced_on)
            valid_date = parsed_date.isoformat() == synced_on
            if valid_date:
                age_days = (today - parsed_date).days
        except ValueError:
            pass

    raw_ref = snapshot.get("x-solwyn-core-ref")
    core_ref = raw_ref if isinstance(raw_ref, str) else None
    valid_ref = core_ref is not None and _CORE_REF.fullmatch(core_ref) is not None
    fresh = (
        valid_date and age_days is not None and 0 <= age_days <= _MAX_SNAPSHOT_AGE_DAYS
    )
    return _SnapshotDiagnostic(
        status="ok" if fresh and valid_ref else "degraded",
        synced_on=synced_on,
        age_days=age_days,
        core_ref=core_ref,
    )


def _snapshot_or_degraded() -> _SnapshotDiagnostic:
    try:
        return _snapshot_diagnostic(_load_snapshot(), today=_today())
    except (OSError, UnicodeError, ValueError, TypeError):
        return _SnapshotDiagnostic(
            status="degraded",
            synced_on=None,
            age_days=None,
            core_ref=None,
        )


def _table(data: _DoctorData) -> TableData:
    readiness_detail = (
        f"HTTP {data.readiness.http_status}; service {data.readiness.service}"
        if data.readiness.http_status is not None and data.readiness.service is not None
        else "Readiness endpoint unavailable."
    )
    rows: list[tuple[str, str]] = [
        (
            "Overall — "
            + (
                "All diagnostics passed."
                if data.status == "ok"
                else "One or more diagnostics need attention."
            ),
            data.status,
        ),
        (
            f"Config — {data.config.detail}",
            data.config.status,
        ),
        (
            "Keychain — "
            + data.keychain.detail.removeprefix("Keychain available; ").capitalize(),
            data.keychain.status,
        ),
        (f"Readiness — {readiness_detail}", data.readiness.status),
    ]
    rows.extend(
        (
            f"Readiness {check.name} — {check.detail or '—'}"
            f"{' (non-gating)' if not check.gating else ''}",
            check.status,
        )
        for check in data.readiness.checks
    )
    probe_name = data.authenticated_probe.credential_type or "credential"
    rows.append(
        (
            f"Authenticated {probe_name} — {data.authenticated_probe.detail}",
            data.authenticated_probe.status,
        )
    )
    snapshot_detail = (
        f"{data.snapshot.synced_on} ({data.snapshot.age_days} days old)"
        if data.snapshot.synced_on is not None and data.snapshot.age_days is not None
        else "Snapshot metadata is incomplete."
    )
    rows.append((f"Snapshot — {snapshot_detail}", data.snapshot.status))
    if data.snapshot.core_ref is not None:
        rows.append((f"Core ref — {data.snapshot.core_ref}", data.snapshot.status))
    return TableData(
        columns=("Check", "Status"),
        rows=tuple(rows),
        right_align=("Status",),
    )


def doctor(
    context: typer.Context,
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp", help="Check MCP discovery and captured-credential reads."
        ),
    ] = False,
    executable: Annotated[
        Path | None, typer.Option(help="Installed Solwyn executable for --mcp.")
    ] = None,
    profile: Annotated[
        McpProfile, typer.Option(help="MCP tool profile for --mcp.")
    ] = McpProfile.READ,
) -> None:
    """Check local configuration, dependencies, credentials, and API metadata."""
    if mcp:
        doctor_mcp(context, executable, profile.value)
        return
    profile_source = context.get_parameter_source("profile")
    if executable is not None or (
        profile_source is not None and profile_source.name == "COMMANDLINE"
    ):
        raise typer.BadParameter("--executable and --profile require --mcp.")
    runtime = command_context(context)
    config = _config_diagnostic()
    keychain = _keychain_diagnostic()
    credential_error: str | None = None
    try:
        credential = resolve_credential()
    except AuthError as error:
        credential = None
        credential_error = str(error)

    endpoint_available = runtime.api_url_source != "fallback"
    with SolwynClient(
        api_url=runtime.api_url,
        credential=credential if endpoint_available else None,
    ) as client:
        readiness = _readiness_diagnostic(client)
        authenticated_probe = _authenticated_probe(
            client=client,
            credential=credential,
            credential_error=credential_error,
            project_id=runtime.project,
            endpoint_available=endpoint_available,
        )
    snapshot = _snapshot_or_degraded()
    status: Literal["ok", "degraded"] = (
        "ok"
        if config.status == "ok"
        and keychain.status == "ok"
        and readiness.status == "ready"
        and authenticated_probe.status == "ok"
        and snapshot.status == "ok"
        else "degraded"
    )
    data = _DoctorData(
        status=status,
        config=config,
        keychain=keychain,
        readiness=readiness,
        authenticated_probe=authenticated_probe,
        snapshot=snapshot,
    )
    output_engine(context).write_success(
        schema="solwyn.doctor.v1",
        data=data,
        table=_table(data),
    )
    if status == "degraded":
        raise typer.Exit(code=1)
