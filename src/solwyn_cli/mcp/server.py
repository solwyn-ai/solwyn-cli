"""FastMCP stdio server and the frozen eight-tool Solwyn surface."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Annotated, Generic, Literal, TypeVar

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from solwyn_cli import auth, runtime
from solwyn_cli._vendor.wire import (
    BudgetCheckRequest,
    BudgetConfirmRequest,
    MediaUsage,
    ProviderName,
    ServiceTier,
    TokenDetails,
)
from solwyn_cli.artifacts import agent_guidance
from solwyn_cli.auth import AuthError, JwtCredential, ProjectKeyCredential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands.budget import (
    BudgetCheckData,
    BudgetConfirmData,
    BudgetStatusData,
    request_budget_check,
    request_budget_confirm,
    request_budget_status,
)
from solwyn_cli.commands.costs import (
    CostGrouping,
    CostRange,
    CostsData,
    fetch_costs,
)
from solwyn_cli.commands.projects import ProjectsListData, fetch_projects
from solwyn_cli.commands.runs import (
    AgentRunAmbiguousError,
    AgentRunEntry,
    AgentRunListData,
    AgentRunNotFoundError,
    RunRange,
    RunSort,
    _cursor_sort,
    _known_allowed_ranges,
    _validate_custom_window,
    fetch_agent_runs,
    get_agent_run,
    resolve_agent_run_id,
)
from solwyn_cli.commands.status import ProjectStatusData, fetch_project_status
from solwyn_cli.errors import ApiError


DataT = TypeVar("DataT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound="_ToolResult[BaseModel]")
BudgetCheckModel = Annotated[str, Field(max_length=2048)]
BudgetFallbackProviders = Annotated[list[ProviderName], Field(max_length=8)]
BudgetFallbackModels = Annotated[list[BudgetCheckModel], Field(max_length=8)]
BudgetAgentRunId = Annotated[str, Field(max_length=256)]
TOOL_NAMES = (
    "budget_status",
    "budget_check",
    "budget_confirm",
    "get_project_status",
    "get_costs",
    "list_projects",
    "list_agent_runs",
    "get_agent_run",
)
_SAFE_REMOTE_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "authentication_required",
        "budget_denied",
        "email_not_verified",
        "email_provider_unavailable",
        "forbidden",
        "invalid_project",
        "invalid_token",
        "not_found",
        "project_limit_reached",
        "project_not_found",
        "quota_exceeded",
        "rate_limited",
        "read_only_key",
        "reservation_not_found",
        "token_expired",
        "unauthorized",
        "unknown_model",
        "unknown_notification_channel",
        "upgrade_required",
    }
)
_SAFE_REMOTE_HINTS = {
    "authentication_failed": "Run solwyn login again, then retry.",
    "authentication_required": "Run solwyn login, then retry.",
    "forbidden": "Check account access for this operation, then retry.",
    "invalid_token": "Run solwyn login again, then retry.",
    # Rotation is JWT-only and destructive, so the agent-facing hint stops at the
    # remedy an agent can actually apply and at a read-only way to inspect scope.
    "read_only_key": (
        "The configured project key is read-only; set SOLWYN_API_KEY to a "
        "write-scoped key, then retry. Confirm a key's scope with "
        "solwyn keys show."
    ),
    "reservation_not_found": (
        "The reservation expired or was consumed; run a fresh check before spend."
    ),
    "token_expired": "Run solwyn login again, then retry.",
    "unauthorized": "Run solwyn login again, then retry.",
}
_ENTITLEMENT_HINT_PREFIXES = {
    "project_limit_reached": "Manage project limits in the dashboard",
    "quota_exceeded": "Review usage and plan limits in the dashboard",
    "upgrade_required": "Review upgrade options in the dashboard",
}
_SAFE_CTA_PATHS = frozenset({"/billing"})
_DEFAULT_CTA_PATH = "/billing"


class _ToolOnlyFastMCP(FastMCP):
    """FastMCP 1.28.1 server that registers only the supported tool handlers."""

    def _setup_handlers(self) -> None:
        # Capabilities derive from the low-level handler registry. Registering only
        # these two handlers prevents unsupported prompt/resource advertisement.
        self._mcp_server.list_tools()(self.list_tools)
        self._mcp_server.call_tool(validate_input=False)(self.call_tool)


class McpLaunchOptions(BaseModel):
    """Immutable non-secret CLI overrides retained for fresh per-call resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str | None = None
    project_id: str | None = None


class _ToolError(BaseModel):
    """Safe structured MCP error with no raw server or credential material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    hint: str | None = None
    status_code: int | None = None


class _ToolResult(BaseModel, Generic[DataT]):
    """Typed success/error envelope used by every MCP tool."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: str = Field(alias="schema")
    ok: bool
    data: DataT | None = None
    error: _ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "_ToolResult[DataT]":
        if self.ok != (self.data is not None and self.error is None):
            raise ValueError("Tool result must contain exactly one outcome")
        return self


class _BudgetStatusResult(_ToolResult[BudgetStatusData]):
    schema_name: Literal["solwyn.mcp.budget-status.v1"] = Field(
        default="solwyn.mcp.budget-status.v1",
        alias="schema",
    )


class _BudgetCheckResult(_ToolResult[BudgetCheckData]):
    schema_name: Literal["solwyn.mcp.budget-check.v1"] = Field(
        default="solwyn.mcp.budget-check.v1",
        alias="schema",
    )


class _BudgetConfirmResult(_ToolResult[BudgetConfirmData]):
    schema_name: Literal["solwyn.mcp.budget-confirm.v1"] = Field(
        default="solwyn.mcp.budget-confirm.v1",
        alias="schema",
    )


class _ProjectStatusResult(_ToolResult[ProjectStatusData]):
    schema_name: Literal["solwyn.mcp.project-status.v1"] = Field(
        default="solwyn.mcp.project-status.v1",
        alias="schema",
    )


class _CostsResult(_ToolResult[CostsData]):
    schema_name: Literal["solwyn.mcp.costs.v1"] = Field(
        default="solwyn.mcp.costs.v1",
        alias="schema",
    )


class _ProjectsResult(_ToolResult[ProjectsListData]):
    schema_name: Literal["solwyn.mcp.projects.v1"] = Field(
        default="solwyn.mcp.projects.v1",
        alias="schema",
    )


class _AgentRunsResult(_ToolResult[AgentRunListData]):
    schema_name: Literal["solwyn.mcp.agent-runs.v1"] = Field(
        default="solwyn.mcp.agent-runs.v1",
        alias="schema",
    )


class _AgentRunResult(_ToolResult[AgentRunEntry]):
    schema_name: Literal["solwyn.mcp.agent-run.v1"] = Field(
        default="solwyn.mcp.agent-run.v1",
        alias="schema",
    )


@contextmanager
def tool_call_client(
    launch_options: McpLaunchOptions | None = None,
) -> Iterator[tuple[runtime.CommandContext, SolwynClient]]:
    """Resolve fresh runtime, credential, and client state for one MCP tool call."""
    launch = launch_options or McpLaunchOptions()
    command_runtime = runtime.resolve_command_context(
        api_url=launch.api_url,
        project=launch.project_id,
        output_format=None,
        json_flag=False,
        no_color=True,
    )
    credential = auth.resolve_credential()
    if credential is None:
        raise AuthError("Authentication required. Configure credentials and retry.")
    with SolwynClient(
        api_url=command_runtime.api_url,
        credential=credential,
    ) as client:
        yield command_runtime, client


def _canonical_guidance_line(fragment: str) -> str:
    """Select one whole semantic line from the P5.2 single source."""
    matches = [
        line.removeprefix("- ")
        for line in agent_guidance().splitlines()
        if fragment in line
    ]
    if len(matches) != 1:
        raise RuntimeError("Canonical agent guidance is missing or ambiguous.")
    return matches[0]


PROJECT_STATUS_DESCRIPTION = "Read the configured project's budget and provider health without authorizing spend."
COSTS_DESCRIPTION = (
    "Read aggregated project costs for one frozen time range and grouping."
)
PROJECTS_DESCRIPTION = (
    "List projects visible to the logged-in user. Requires login/JWT authentication."
)
AGENT_RUNS_DESCRIPTION = (
    "List project agent runs. Costs are lifetime totals; the window only selects "
    "which runs qualify. Accepts a project key or login."
)
AGENT_RUN_DESCRIPTION = (
    "Read one agent run by storage ID or exact raw run name. Accepts a project "
    "key or login."
)


def _budget_tool_descriptions() -> tuple[str, str, str]:
    """Build all guardrail descriptions from the live canonical guidance source."""
    status = _canonical_guidance_line("for polling")
    check = _canonical_guidance_line("specific un-instrumented")
    sdk = _canonical_guidance_line("Never wrap SDK-instrumented")
    fail_open = _canonical_guidance_line("reservation_id=null")
    unconfirmed = _canonical_guidance_line("repeated unconfirmed checks")
    shared_sharp_edges = " ".join((check, sdk, fail_open, unconfirmed))
    return (
        status,
        "Reserve budget only immediately before the caller-made spend. "
        f"{shared_sharp_edges}",
        "Settle only a non-null reservation after the caller-made spend. "
        f"{shared_sharp_edges}",
    )


def _project_id(
    command_runtime: runtime.CommandContext,
    requested_project_id: str | None,
) -> str:
    project_id = requested_project_id or command_runtime.project
    if project_id is None or not project_id.strip():
        raise ValueError("project_required")
    return project_id


def _success(result_type: type[ResultT], data: BaseModel) -> ResultT:
    return result_type(ok=True, data=data)


def _error(
    result_type: type[ResultT],
    *,
    code: str,
    message: str,
    hint: str | None = None,
    status_code: int | None = None,
) -> ResultT:
    return result_type(
        ok=False,
        error=_ToolError(
            code=code,
            message=message,
            hint=hint,
            status_code=status_code,
        ),
    )


def _operation_error(
    result_type: type[ResultT],
    error: Exception,
    *,
    invalid_response_code: str,
) -> ResultT:
    if isinstance(error, AgentRunAmbiguousError):
        return _error(
            result_type,
            code="ambiguous_agent_run",
            message="More than one run has that exact name.",
            hint="Candidates: " + ", ".join(error.candidate_ids),
        )
    if isinstance(error, AgentRunNotFoundError):
        return _error(
            result_type,
            code="agent_run_not_found",
            message="Agent run not found.",
            hint="Call list_agent_runs to inspect visible run names and IDs.",
            status_code=404,
        )
    if isinstance(error, ApiError):
        code = _normalized_remote_error_code(error)
        return _error(
            result_type,
            code=code,
            message=(
                "The Solwyn API rejected the request."
                if error.status_code is not None
                else "Could not connect to the Solwyn API."
            ),
            hint=_safe_remote_error_hint(error, code),
            status_code=error.status_code,
        )
    if isinstance(error, AuthError):
        return _error(
            result_type,
            code="authentication_required",
            message="Solwyn authentication is required.",
            hint="Configure a project key or run solwyn login, then retry.",
        )
    if isinstance(error, ValueError) and str(error) == "project_required":
        return _error(
            result_type,
            code="project_required",
            message="A Solwyn project is required.",
            hint=(
                "Set SOLWYN_PROJECT_ID, configure default_project, or pass project_id."
            ),
        )
    return _error(
        result_type,
        code=invalid_response_code,
        message="The Solwyn API response was invalid.",
    )


def _normalized_remote_error_code(error: ApiError) -> str:
    """Return only fixed semantic codes; never reflect arbitrary server values."""
    if error.status_code == 429:
        return "rate_limited"
    if error.code in _SAFE_REMOTE_ERROR_CODES:
        return error.code
    if error.status_code == 401:
        return "authentication_failed"
    if error.status_code == 403:
        return "forbidden"
    return "api_error"


def _safe_remote_error_hint(error: ApiError, code: str) -> str | None:
    """Keep only local allowlisted CTA paths from shared sanitized hints."""
    prefix = _ENTITLEMENT_HINT_PREFIXES.get(code)
    if prefix is None:
        return _SAFE_REMOTE_HINTS.get(code)
    cta_path = next(
        (
            path
            for path in _SAFE_CTA_PATHS
            if error.hint is not None and error.hint.endswith(f": {path}")
        ),
        _DEFAULT_CTA_PATH,
    )
    return f"{prefix}: {cta_path}"


def _require_project_key(
    result_type: type[ResultT],
    credential: JwtCredential | ProjectKeyCredential,
    operation: str,
) -> ResultT | None:
    if isinstance(credential, ProjectKeyCredential):
        return None
    return _error(
        result_type,
        code="project_key_required",
        message=(
            "Budget checks require a project API key."
            if operation == "checks"
            else "Budget confirmation requires a project API key."
        ),
        hint="Set SOLWYN_API_KEY or store a project key with solwyn login --with-key.",
    )


def _budget_status_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
) -> _BudgetStatusResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = request_budget_status(
                client=client,
                project_id=resolved_project,
            )
        return _success(_BudgetStatusResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetStatusResult,
            error,
            invalid_response_code="invalid_budget_status_response",
        )


def _budget_check_operation(
    *,
    launch_options: McpLaunchOptions,
    estimated_input_tokens: int,
    model: str,
    provider: ProviderName,
    fallback_providers: list[ProviderName],
    fallback_models: list[str],
    agent_run_id: str | None,
    image_count: int | None,
    generation_count: int | None,
    video_seconds: float | None,
    audio_seconds: float | None,
    input_characters: int | None,
    resolution: str | None,
    quality: str | None,
    is_estimated: bool,
) -> _BudgetCheckResult:
    if len(fallback_providers) != len(fallback_models):
        return _error(
            _BudgetCheckResult,
            code="fallback_alignment_required",
            message="Fallback providers and models must have aligned counts.",
        )
    quantities = (
        image_count,
        generation_count,
        video_seconds,
        audio_seconds,
        input_characters,
        resolution,
        quality,
    )
    try:
        # No media argument at all means a text-only call: the request omits
        # estimated_media entirely rather than posting an all-null object.
        estimated_media = (
            MediaUsage(
                image_count=image_count,
                generation_count=generation_count,
                video_seconds=video_seconds,
                audio_seconds=audio_seconds,
                input_characters=input_characters,
                resolution=resolution,
                quality=quality,
                is_estimated=is_estimated,
            )
            if is_estimated or any(value is not None for value in quantities)
            else None
        )
        request = BudgetCheckRequest(
            price_hints_version="1",
            run_directive_version="1" if agent_run_id is not None else None,
            estimated_input_tokens=estimated_input_tokens,
            model=model,
            provider=provider,
            estimated_media=estimated_media,
            fallback_providers=fallback_providers,
            fallback_models=fallback_models,
            agent_run_id=agent_run_id,
        )
    except ValidationError:
        return _error(
            _BudgetCheckResult,
            code="invalid_budget_check_request",
            message="The budget check request is invalid.",
        )
    try:
        with tool_call_client(launch_options) as (_, client):
            auth_error = _require_project_key(
                _BudgetCheckResult, client.current_credential, "checks"
            )
            if auth_error is not None:
                return auth_error
            data = request_budget_check(client=client, request=request)
        return _success(_BudgetCheckResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetCheckResult,
            error,
            invalid_response_code="invalid_budget_check_response",
        )


def _budget_confirm_operation(
    *,
    launch_options: McpLaunchOptions,
    reservation_id: str,
    call_id: str,
    model: str,
    provider: ProviderName,
    is_provider_fallback: bool,
    provider_region: str | None,
    service_tier: ServiceTier | None,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cache_creation_5m_tokens: int,
    cache_creation_1h_tokens: int,
    reasoning_tokens: int,
    audio_input_tokens: int,
    audio_output_tokens: int,
    image_input_tokens: int,
    image_output_tokens: int,
    accepted_prediction_tokens: int,
    rejected_prediction_tokens: int,
    tool_use_input_tokens: int,
    is_estimated: bool,
) -> _BudgetConfirmResult:
    try:
        request = BudgetConfirmRequest(
            reservation_id=reservation_id,
            call_id=call_id,
            model=model,
            provider=provider,
            is_provider_fallback=is_provider_fallback,
            provider_region=provider_region,
            service_tier=service_tier,
            token_details=TokenDetails(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_creation_5m_tokens=cache_creation_5m_tokens,
                cache_creation_1h_tokens=cache_creation_1h_tokens,
                reasoning_tokens=reasoning_tokens,
                audio_input_tokens=audio_input_tokens,
                audio_output_tokens=audio_output_tokens,
                image_input_tokens=image_input_tokens,
                image_output_tokens=image_output_tokens,
                accepted_prediction_tokens=accepted_prediction_tokens,
                rejected_prediction_tokens=rejected_prediction_tokens,
                tool_use_input_tokens=tool_use_input_tokens,
                is_estimated=is_estimated,
            ),
        )
    except ValidationError:
        return _error(
            _BudgetConfirmResult,
            code="invalid_budget_confirm_request",
            message="The budget confirmation request is invalid.",
        )
    try:
        with tool_call_client(launch_options) as (_, client):
            auth_error = _require_project_key(
                _BudgetConfirmResult, client.current_credential, "confirmation"
            )
            if auth_error is not None:
                return auth_error
            data = request_budget_confirm(client=client, request=request)
        return _success(
            _BudgetConfirmResult,
            data,
        )
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetConfirmResult,
            error,
            invalid_response_code="invalid_budget_confirm_response",
        )


def _project_status_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
) -> _ProjectStatusResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = fetch_project_status(
                client=client,
                project_id=resolved_project,
            )
        return _success(_ProjectStatusResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _ProjectStatusResult,
            error,
            invalid_response_code="invalid_status_response",
        )


def _costs_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    time_range: CostRange,
    group_by: CostGrouping,
) -> _CostsResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = fetch_costs(
                client=client,
                project_id=resolved_project,
                time_range=time_range,
                group_by=group_by,
            )
        return _success(_CostsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _CostsResult,
            error,
            invalid_response_code="invalid_costs_response",
        )


def _projects_operation(launch_options: McpLaunchOptions) -> _ProjectsResult:
    try:
        with tool_call_client(launch_options) as (_, client):
            if isinstance(client.current_credential, ProjectKeyCredential):
                return _error(
                    _ProjectsResult,
                    code="authentication_required",
                    message="User login is required to list projects.",
                    hint=(
                        "Unset SOLWYN_API_KEY and run solwyn login to establish JWT "
                        "authentication, then retry."
                    ),
                )
            data = ProjectsListData(projects=fetch_projects(client=client))
        return _success(_ProjectsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _ProjectsResult,
            error,
            invalid_response_code="invalid_projects_response",
        )


def _agent_runs_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    time_range: RunRange,
    from_time: str | None,
    to_time: str | None,
    query: str | None,
    parent: str | None,
    sort: RunSort,
    limit: int,
    cursor: str | None,
) -> _AgentRunsResult:
    try:
        _validate_custom_window(from_time, to_time)
    except ValueError as error:
        return _error(
            _AgentRunsResult,
            code=str(error),
            message="The custom run window is invalid.",
            hint="Provide both timezone-aware bounds with to_time later than from_time.",
        )
    if cursor is not None:
        embedded_sort = _cursor_sort(cursor)
        if embedded_sort is not None and embedded_sort != sort.value:
            return _error(
                _AgentRunsResult,
                code="cursor_sort_mismatch",
                message="The cursor was created with a different sort.",
                hint="Remove cursor when changing sort.",
            )
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = fetch_agent_runs(
                client=client,
                project_id=resolved_project,
                time_range=time_range,
                from_=from_time,
                to=to_time,
                query=query,
                parent=parent,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
        return _success(_AgentRunsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        if (
            isinstance(error, ApiError)
            and error.status_code == 402
            and error.code == "upgrade_required"
        ):
            allowed = _known_allowed_ranges(error)
            entitlement_hint = _safe_remote_error_hint(error, "upgrade_required")
            allowed_hint = "Allowed values: " + ", ".join(allowed) if allowed else None
            hint = (
                f"{allowed_hint}. {entitlement_hint}"
                if allowed_hint is not None and entitlement_hint is not None
                else allowed_hint or entitlement_hint
            )
            return _error(
                _AgentRunsResult,
                code="upgrade_required",
                message=f"Range '{time_range.value}' is not available on your tier.",
                hint=hint,
                status_code=402,
            )
        return _operation_error(
            _AgentRunsResult,
            error,
            invalid_response_code="invalid_agent_runs_response",
        )


def _agent_run_operation(
    *,
    launch_options: McpLaunchOptions,
    run: str,
    project_id: str | None,
) -> _AgentRunResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            storage_run_id = resolve_agent_run_id(
                client=client,
                project_id=resolved_project,
                run=run,
            )
            data = get_agent_run(
                client=client,
                project_id=resolved_project,
                storage_run_id=storage_run_id,
            )
        return _success(_AgentRunResult, data)
    except (
        AgentRunAmbiguousError,
        AgentRunNotFoundError,
        ApiError,
        AuthError,
        ValidationError,
        ValueError,
        TypeError,
    ) as error:
        return _operation_error(
            _AgentRunResult,
            error,
            invalid_response_code="invalid_agent_run_response",
        )


def create_server(*, launch_options: McpLaunchOptions | None = None) -> FastMCP:
    """Build the stateless local MCP server and register exactly eight tools."""
    launch = launch_options or McpLaunchOptions()
    server = _ToolOnlyFastMCP(
        name="solwyn",
        instructions=(
            "Use Solwyn tools for explicit project and budget operations. "
            "Budget status is a pure read and the only polling surface. "
            "Budget check creates a reservation only immediately before caller-made "
            "un-instrumented provider spend. "
            "Budget confirm settles a non-null reservation after that spend. "
            "Never wrap Solwyn SDK-instrumented calls with budget check or confirm. "
            "When check returns a null reservation, skip confirm."
        ),
        log_level="ERROR",
    )
    status_description, check_description, confirm_description = (
        _budget_tool_descriptions()
    )

    @server.tool(
        name=TOOL_NAMES[0],
        description=status_description,
        structured_output=True,
    )
    async def budget_status(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _BudgetStatusResult:
        return await anyio.to_thread.run_sync(
            partial(_budget_status_operation, project_id, launch)
        )

    @server.tool(
        name=TOOL_NAMES[1],
        description=check_description,
        structured_output=True,
    )
    async def budget_check(
        estimated_input_tokens: Annotated[int, Field(ge=0)],
        model: BudgetCheckModel,
        provider: ProviderName,
        fallback_providers: BudgetFallbackProviders | None = None,
        fallback_models: BudgetFallbackModels | None = None,
        agent_run_id: BudgetAgentRunId | None = None,
        image_count: Annotated[int, Field(ge=0)] | None = None,
        generation_count: Annotated[int, Field(ge=0)] | None = None,
        video_seconds: Annotated[float, Field(ge=0)] | None = None,
        audio_seconds: Annotated[float, Field(ge=0)] | None = None,
        input_characters: Annotated[int, Field(ge=0)] | None = None,
        resolution: Annotated[str, Field(max_length=32)] | None = None,
        quality: Annotated[str, Field(max_length=32)] | None = None,
        is_estimated: bool = False,
    ) -> _BudgetCheckResult:
        return await anyio.to_thread.run_sync(
            partial(
                _budget_check_operation,
                launch_options=launch,
                estimated_input_tokens=estimated_input_tokens,
                model=model,
                provider=provider,
                fallback_providers=fallback_providers or [],
                fallback_models=fallback_models or [],
                agent_run_id=agent_run_id,
                image_count=image_count,
                generation_count=generation_count,
                video_seconds=video_seconds,
                audio_seconds=audio_seconds,
                input_characters=input_characters,
                resolution=resolution,
                quality=quality,
                is_estimated=is_estimated,
            )
        )

    @server.tool(
        name=TOOL_NAMES[2],
        description=confirm_description,
        structured_output=True,
    )
    async def budget_confirm(
        reservation_id: Annotated[str, Field(min_length=1)],
        call_id: Annotated[str, Field(min_length=1, max_length=36)],
        model: Annotated[str, Field(min_length=1, max_length=2048)],
        provider: ProviderName,
        is_provider_fallback: bool = False,
        provider_region: Annotated[
            str | None, Field(min_length=1, max_length=32)
        ] = None,
        service_tier: ServiceTier | None = None,
        input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        cached_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        cache_creation_5m_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        cache_creation_1h_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        reasoning_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        audio_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        audio_output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        image_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        image_output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        accepted_prediction_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        rejected_prediction_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        tool_use_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0,
        is_estimated: bool = False,
    ) -> _BudgetConfirmResult:
        return await anyio.to_thread.run_sync(
            partial(
                _budget_confirm_operation,
                launch_options=launch,
                reservation_id=reservation_id,
                call_id=call_id,
                model=model,
                provider=provider,
                is_provider_fallback=is_provider_fallback,
                provider_region=provider_region,
                service_tier=service_tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_creation_5m_tokens=cache_creation_5m_tokens,
                cache_creation_1h_tokens=cache_creation_1h_tokens,
                reasoning_tokens=reasoning_tokens,
                audio_input_tokens=audio_input_tokens,
                audio_output_tokens=audio_output_tokens,
                image_input_tokens=image_input_tokens,
                image_output_tokens=image_output_tokens,
                accepted_prediction_tokens=accepted_prediction_tokens,
                rejected_prediction_tokens=rejected_prediction_tokens,
                tool_use_input_tokens=tool_use_input_tokens,
                is_estimated=is_estimated,
            )
        )

    @server.tool(
        name=TOOL_NAMES[3],
        description=PROJECT_STATUS_DESCRIPTION,
        structured_output=True,
    )
    async def get_project_status(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _ProjectStatusResult:
        return await anyio.to_thread.run_sync(
            partial(_project_status_operation, project_id, launch)
        )

    @server.tool(
        name=TOOL_NAMES[4],
        description=COSTS_DESCRIPTION,
        structured_output=True,
    )
    async def get_costs(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        time_range: CostRange = CostRange.SEVEN_DAYS,
        group_by: CostGrouping = CostGrouping.MODEL,
    ) -> _CostsResult:
        return await anyio.to_thread.run_sync(
            partial(
                _costs_operation,
                launch_options=launch,
                project_id=project_id,
                time_range=time_range,
                group_by=group_by,
            )
        )

    @server.tool(
        name=TOOL_NAMES[5],
        description=PROJECTS_DESCRIPTION,
        structured_output=True,
    )
    async def list_projects() -> _ProjectsResult:
        return await anyio.to_thread.run_sync(partial(_projects_operation, launch))

    @server.tool(
        name=TOOL_NAMES[6],
        description=AGENT_RUNS_DESCRIPTION,
        structured_output=True,
    )
    async def list_agent_runs(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        time_range: RunRange = RunRange.SEVEN_DAYS,
        from_time: str | None = None,
        to_time: str | None = None,
        query: Annotated[str | None, Field(max_length=255)] = None,
        parent: Annotated[str | None, Field(min_length=1, max_length=256)] = None,
        sort: RunSort = RunSort.LAST_SEEN_AT,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> _AgentRunsResult:
        return await anyio.to_thread.run_sync(
            partial(
                _agent_runs_operation,
                launch_options=launch,
                project_id=project_id,
                time_range=time_range,
                from_time=from_time,
                to_time=to_time,
                query=query,
                parent=parent,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
        )

    @server.tool(
        name=TOOL_NAMES[7],
        description=AGENT_RUN_DESCRIPTION,
        structured_output=True,
    )
    async def get_agent_run_tool(
        run: Annotated[str, Field(min_length=1, max_length=255)],
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _AgentRunResult:
        return await anyio.to_thread.run_sync(
            partial(
                _agent_run_operation,
                launch_options=launch,
                run=run,
                project_id=project_id,
            )
        )

    return server
