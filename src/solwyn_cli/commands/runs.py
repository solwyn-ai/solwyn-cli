"""Project agent-run inspection and control commands."""

import base64
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    TypeAdapter,
    ValidationError,
)

from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli import _mcp_operation as operation
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


_STORAGE_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_RFC3339_QUERY = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_AWARE_DATETIME = TypeAdapter(AwareDatetime)
_RANGE_ORDER = ("7d", "30d", "90d", "365d")


class RunRange(StrEnum):
    """Preset agent-run listing windows."""

    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    NINETY_DAYS = "90d"
    YEAR = "365d"


class RunSort(StrEnum):
    """Server-supported descending run sort keys."""

    LAST_SEEN_AT = "last_seen_at"
    TOTAL_COST = "total_cost"
    TOTAL_CALLS = "total_calls"


class _CostWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    requested_from: AwareDatetime
    requested_to: AwareDatetime
    effective_from: AwareDatetime
    effective_to: AwareDatetime
    retention_clamped: bool


class _AgentRunEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: Annotated[str, Field(pattern=r"^run_[0-9a-f]{32}$")]
    name: str
    parent_run_id: str | None
    project_id: str
    started_at: AwareDatetime
    last_seen_at: AwareDatetime
    total_calls: int
    total_cost: FiniteFloat
    total_denied_cost: FiniteFloat
    total_input_tokens: int
    total_output_tokens: int
    velocity_flags: list[str] | None
    stopped_at: AwareDatetime | None
    stopped_by_user_id: str | None
    stoppable: bool


class _AgentRunListData(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    runs: list[_AgentRunEntry]
    next_cursor: str | None
    has_more: bool
    window: _CostWindow


class _AgentRunStopData(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    project_id: str
    run_id: Annotated[str, Field(pattern=r"^run_[0-9a-f]{32}$")]
    run_name: str
    stopped_at: AwareDatetime
    already_stopped: bool
    leases_revoked: int
    enforcement_deadline: AwareDatetime | None


AgentRunEntry = _AgentRunEntry
AgentRunListData = _AgentRunListData
AgentRunStopData = _AgentRunStopData


class AgentRunNotFoundError(ValueError):
    """The raw run name did not resolve to a visible run."""


class AgentRunAmbiguousError(ValueError):
    """The raw run name resolved to more than one storage ID."""

    def __init__(self, candidate_ids: tuple[str, ...]) -> None:
        super().__init__("Agent run name is ambiguous")
        self.candidate_ids = candidate_ids


app = typer.Typer(
    help="Inspect and control project agent runs.",
    epilog=help_contract(
        endpoint="composite router for agent-run subcommands",
        auth="varies by subcommand",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
        guidance=(
            "List and show accept a project key or login; stop requires login with budget.write.",
        ),
    ),
)


def _date_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _project_id(context: typer.Context) -> str:
    project_id = command_context(context).project
    if project_id is None:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="A project is required.",
                hint=(
                    "Choose a project with --project, SOLWYN_PROJECT_ID, "
                    "or config default_project."
                ),
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return project_id


def _cursor_sort(cursor: str) -> str | None:
    """Read only the sort marker from Core's opaque cursor."""
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(f"{cursor}{padding}", altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    sort = payload.get("sort")
    return sort if isinstance(sort, str) else None


def _validate_custom_window(from_: str | None, to: str | None) -> None:
    if (from_ is None) != (to is None):
        raise ValueError("custom_window_pair_required")
    if from_ is None or to is None:
        return
    if _RFC3339_QUERY.fullmatch(from_) is None or _RFC3339_QUERY.fullmatch(to) is None:
        raise ValueError("custom_window_invalid")
    try:
        parsed_from = _AWARE_DATETIME.validate_python(from_)
        parsed_to = _AWARE_DATETIME.validate_python(to)
    except ValidationError:
        raise ValueError("custom_window_invalid") from None
    if parsed_to <= parsed_from:
        raise ValueError("custom_window_invalid")


def fetch_agent_runs(
    *,
    client: SolwynClient,
    project_id: str,
    time_range: RunRange,
    from_: str | None = None,
    to: str | None = None,
    query: str | None = None,
    parent: str | None = None,
    sort: RunSort = RunSort.LAST_SEEN_AT,
    limit: int = 25,
    cursor: str | None = None,
) -> AgentRunListData:
    """Read and validate one page of project agent runs."""
    operation.page(cursor)
    params: dict[str, str | int] = {
        "range": time_range.value,
        "sort": sort.value,
        "limit": limit,
    }
    if from_ is not None:
        params["from"] = from_
    if to is not None:
        params["to"] = to
    if query is not None:
        params["q"] = query
    if parent is not None:
        params["parent"] = parent
    if cursor is not None:
        params["cursor"] = cursor
    project = quote(project_id, safe="")
    response = client.request(
        "GET",
        f"/api/v1/projects/{project}/agent-runs",
        params=params,
    )
    data = _AgentRunListData.model_validate(response.json())
    if any(run.project_id != project_id for run in data.runs):
        raise ValueError("Run response project did not match the request")
    if data.has_more != (data.next_cursor is not None):
        raise ValueError("Run pagination state was inconsistent")
    return data


def get_agent_run(
    *, client: SolwynClient, project_id: str, storage_run_id: str
) -> AgentRunEntry:
    """Read and validate one storage-ID-addressed agent run."""
    project = quote(project_id, safe="")
    run = quote(storage_run_id, safe="")
    response = client.request("GET", f"/api/v1/projects/{project}/agent-runs/{run}")
    data = _AgentRunEntry.model_validate(response.json())
    if data.project_id != project_id or data.id != storage_run_id:
        raise ValueError("Agent run response did not match the request")
    return data


def stop_agent_run(
    *, client: SolwynClient, project_id: str, storage_run_id: str
) -> AgentRunStopData:
    """Stop one storage-ID-addressed run without replaying the mutation on 429."""
    project = quote(project_id, safe="")
    run = quote(storage_run_id, safe="")
    response = client.request(
        "POST",
        f"/api/v1/projects/{project}/agent-runs/{run}/stop",
        retry_rate_limit=False,
    )
    data = _AgentRunStopData.model_validate(response.json())
    if data.project_id != project_id or data.run_id != storage_run_id:
        raise ValueError("Agent run stop response did not match the request")
    return data


def _known_allowed_ranges(error: ApiError) -> tuple[str, ...]:
    if error.status_code != 402 or not isinstance(error.detail, dict):
        return ()
    allowed = error.detail.get("allowed_values")
    if not isinstance(allowed, list):
        return ()
    return tuple(value for value in _RANGE_ORDER if value in allowed)


def _largest_allowed_range(error: ApiError) -> RunRange | None:
    known = _known_allowed_ranges(error)
    return RunRange(known[-1]) if known else None


def _exact_named_runs(
    *,
    client: SolwynClient,
    project_id: str,
    raw_name: str,
    time_range: RunRange,
) -> list[AgentRunEntry]:
    matches: dict[str, AgentRunEntry] = {}
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        if cursor is not None:
            if cursor in seen:
                raise ValueError(
                    "Run search repeated a cursor; use an exact storage ID."
                )
            seen.add(cursor)
        page = fetch_agent_runs(
            client=client,
            project_id=project_id,
            time_range=time_range,
            query=raw_name,
            sort=RunSort.LAST_SEEN_AT,
            limit=100,
            cursor=cursor,
        )
        for run in page.runs:
            if run.name == raw_name:
                matches[run.id] = run
        if not page.has_more:
            return list(matches.values())
        cursor = page.next_cursor


def resolve_agent_run_id(*, client: SolwynClient, project_id: str, run: str) -> str:
    """Return a storage ID directly or resolve an exact raw run name."""
    if _STORAGE_RUN_ID.fullmatch(run):
        return run
    if not run or len(run) > 255:
        raise AgentRunNotFoundError(run)
    try:
        matches = _exact_named_runs(
            client=client,
            project_id=project_id,
            raw_name=run,
            time_range=RunRange.YEAR,
        )
    except ApiError as error:
        fallback = _largest_allowed_range(error)
        if fallback is None:
            raise
        matches = _exact_named_runs(
            client=client,
            project_id=project_id,
            raw_name=run,
            time_range=fallback,
        )
    if not matches:
        raise AgentRunNotFoundError(run)
    if len(matches) > 1:
        raise AgentRunAmbiguousError(tuple(sorted(item.id for item in matches)))
    return matches[0].id


def _list_table(data: _AgentRunListData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(
            row
            for run in data.runs
            for row in (
                ("Name", run.name),
                ("ID", run.id),
                ("Cost (lifetime)", f"{run.total_cost:.6f}"),
                ("Denied cost", f"{run.total_denied_cost:.6f}"),
                ("Calls", str(run.total_calls)),
                ("Velocity flags", ", ".join(run.velocity_flags or []) or "-"),
                (
                    "Stopped",
                    _date_time(run.stopped_at) if run.stopped_at is not None else "-",
                ),
                ("Stoppable", "yes" if run.stoppable else "no"),
            )
        ),
        right_align=("Value",),
    )


def _show_table(run: _AgentRunEntry) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Name", run.name),
            ("ID", run.id),
            ("Project", run.project_id),
            ("Parent", run.parent_run_id or "-"),
            ("Started", _date_time(run.started_at)),
            ("Last seen", _date_time(run.last_seen_at)),
            ("Lifetime cost", f"{run.total_cost:.6f}"),
            ("Lifetime denied", f"{run.total_denied_cost:.6f}"),
            ("Calls", str(run.total_calls)),
            ("Input tokens", str(run.total_input_tokens)),
            ("Output tokens", str(run.total_output_tokens)),
            ("Velocity flags", ", ".join(run.velocity_flags or []) or "-"),
            ("Stopped", _date_time(run.stopped_at) if run.stopped_at else "-"),
            ("Stopped by", run.stopped_by_user_id or "-"),
            ("Stoppable", "yes" if run.stoppable else "no"),
        ),
        right_align=("Value",),
    )


def _stop_table(data: _AgentRunStopData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Run ID", data.run_id),
            ("Run name", data.run_name),
            ("Stopped at", _date_time(data.stopped_at)),
            ("Already stopped", "yes" if data.already_stopped else "no"),
            ("Leases revoked", str(data.leases_revoked)),
            (
                "Enforcement deadline",
                _date_time(data.enforcement_deadline)
                if data.enforcement_deadline is not None
                else "-",
            ),
        ),
        right_align=("Value",),
    )


def _write_window(data: _AgentRunListData) -> None:
    window = data.window
    typer.echo(
        f"window: {_date_time(window.effective_from)} to "
        f"{_date_time(window.effective_to)}",
        err=True,
    )
    if window.retention_clamped:
        typer.echo(
            f"retention clamp applied: requested {_date_time(window.requested_from)} "
            f"to {_date_time(window.requested_to)}",
            err=True,
        )


def _command_error(context: typer.Context, error: Exception, *, response: str) -> None:
    if isinstance(error, AgentRunAmbiguousError):
        exit_with_error(
            context,
            OutputError(
                code="ambiguous_agent_run",
                message="More than one run has that exact name.",
                hint="Candidates: " + ", ".join(error.candidate_ids),
            ),
        )
    if isinstance(error, AgentRunNotFoundError):
        exit_with_error(
            context,
            OutputError(
                code="agent_run_not_found",
                message="Agent run not found.",
                hint="Use solwyn runs list to inspect visible run names and IDs.",
            ),
            semantic=ErrorSemantic.NOT_FOUND,
        )
    if isinstance(error, ApiError):
        exit_with_error(context, error)
    if isinstance(error, AuthError):
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    exit_with_error(
        context,
        OutputError(
            code=f"invalid_{response}_response",
            message="The agent-run response was invalid.",
        ),
    )


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/agent-runs",
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.runs.v1",
        guidance=(
            "Costs are lifetime totals; the window selects qualifying runs only.",
            "Custom --from/--to bounds override --range and must be timezone-aware.",
            "A cursor can only be reused with the sort that created it.",
        ),
    ),
)
def list_runs(
    context: typer.Context,
    time_range: Annotated[
        RunRange, typer.Option("--range", help="Listing window.")
    ] = RunRange.SEVEN_DAYS,
    from_: Annotated[
        str | None,
        typer.Option("--from", help="Lower-inclusive RFC 3339 custom bound."),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Upper-exclusive RFC 3339 custom bound."),
    ] = None,
    query: Annotated[
        str | None,
        typer.Option("--q", help="Case-insensitive literal name substring."),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option("--parent", help="Raw or storage parent run ID."),
    ] = None,
    sort: Annotated[
        RunSort, typer.Option("--sort", help="Descending sort key.")
    ] = RunSort.LAST_SEEN_AT,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=100, help="Runs per page.")
    ] = 25,
    cursor: Annotated[
        str | None, typer.Option("--cursor", help="Opaque next-page cursor.")
    ] = None,
) -> None:
    """List recent project agent runs."""
    try:
        _validate_custom_window(from_, to)
    except ValueError as error:
        code = str(error)
        exit_with_error(
            context,
            OutputError(
                code=code,
                message=(
                    "Both --from and --to are required for a custom window."
                    if code == "custom_window_pair_required"
                    else "The custom run window is invalid."
                ),
                hint="Use timezone-aware RFC 3339 values with --to later than --from.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if query is not None and len(query) > 255:
        exit_with_error(
            context,
            OutputError(
                code="invalid_run_query",
                message="Run name queries must be at most 255 characters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if parent is not None and not (1 <= len(parent) <= 256):
        exit_with_error(
            context,
            OutputError(
                code="invalid_parent_run",
                message="Parent run IDs must be between 1 and 256 characters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    if cursor is not None:
        embedded_sort = _cursor_sort(cursor)
        if embedded_sort is not None and embedded_sort != sort.value:
            exit_with_error(
                context,
                OutputError(
                    code="cursor_sort_mismatch",
                    message="The cursor was created with a different sort.",
                    hint="Remove --cursor when changing --sort.",
                ),
                semantic=ErrorSemantic.USAGE,
            )

    runtime = command_context(context)
    project_id = _project_id(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            data = fetch_agent_runs(
                client=client,
                project_id=project_id,
                time_range=time_range,
                from_=from_,
                to=to,
                query=query,
                parent=parent,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        _command_error(context, error, response="runs")
    _write_window(data)
    output_engine(context).write_success(
        schema="solwyn.runs.v1", data=data, table=_list_table(data)
    )


@app.command(
    "show",
    epilog=help_contract(
        endpoint=(
            "GET /api/v1/projects/{project}/agent-runs then "
            "GET /api/v1/projects/{project}/agent-runs/{run_id}"
        ),
        auth="any",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.runs.v1",
        guidance=(
            "Storage IDs are read directly; exact raw run names are resolved through the list endpoint.",
        ),
    ),
)
def show_run(
    context: typer.Context,
    run: Annotated[str, typer.Argument(help="Storage ID or exact run name.")],
) -> None:
    """Show one project agent run."""
    runtime = command_context(context)
    project_id = _project_id(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            storage_run_id = resolve_agent_run_id(
                client=client, project_id=project_id, run=run
            )
            data = get_agent_run(
                client=client,
                project_id=project_id,
                storage_run_id=storage_run_id,
            )
    except (
        AgentRunAmbiguousError,
        AgentRunNotFoundError,
        ApiError,
        AuthError,
        ValidationError,
        ValueError,
        TypeError,
    ) as error:
        _command_error(context, error, response="run")
    output_engine(context).write_success(
        schema="solwyn.runs.v1", data=data, table=_show_table(data)
    )


def _jwt_credential(context: typer.Context) -> JwtCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            "Unset SOLWYN_API_KEY, run solwyn login, then retry runs stop."
            if credential.source == "environment"
            else "Run solwyn logout, then solwyn login, and retry runs stop."
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Stopping a run requires login with budget.write permission.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


@app.command(
    "stop",
    epilog=help_contract(
        endpoint=(
            "optional GET /api/v1/projects/{project}/agent-runs then "
            "POST /api/v1/projects/{project}/agent-runs/{run_id}/stop"
        ),
        auth="JWT + budget.write",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.runs.stop.v1",
        guidance=(
            "Stops future checks and lease renewals; in-flight requests finish.",
            "Auto-grouped explorer runs are not stoppable.",
        ),
    ),
)
def stop_run(
    context: typer.Context,
    run: Annotated[str, typer.Argument(help="Storage ID or exact run name.")],
) -> None:
    """Stop future work for one project agent run."""
    runtime = command_context(context)
    project_id = _project_id(context)
    credential = _jwt_credential(context)
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            storage_run_id = resolve_agent_run_id(
                client=client, project_id=project_id, run=run
            )
            data = stop_agent_run(
                client=client,
                project_id=project_id,
                storage_run_id=storage_run_id,
            )
    except (
        AgentRunAmbiguousError,
        AgentRunNotFoundError,
        ApiError,
        AuthError,
        ValidationError,
        ValueError,
        TypeError,
    ) as error:
        _command_error(context, error, response="run_stop")
    output_engine(context).write_success(
        schema="solwyn.runs.stop.v1", data=data, table=_stop_table(data)
    )
    effective_by = data.enforcement_deadline or data.stopped_at
    typer.echo(
        "stops future checks and lease renewals; in-flight requests finish; "
        f"fully effective by {_date_time(effective_by)}",
        err=True,
    )
