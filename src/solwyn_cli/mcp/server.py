"""FastMCP stdio server and the supported Solwyn tool surface."""

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any, Annotated, Generic, Literal, TypeVar

import anyio
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import server as sdk_server
from mcp.shared import session as sdk_session
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from solwyn_cli import auth, runtime
from solwyn_cli._vendor.wire import (
    CALL_ID_PATTERN,
    Modality,
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
from solwyn_cli.commands.alerts import (
    AlertEventListResponse,
    AlertQueryError,
    request_alert_events,
)
from solwyn_cli.commands.budget_rules import BudgetRulesData, request_budget_rules
from solwyn_cli.commands.budget import (
    BudgetCheckData,
    BudgetCheckBatchData,
    BudgetCheckBatchFailure,
    prepare_batch_checks,
    request_budget_check_batch,
    BudgetConfirmData,
    BudgetStatusData,
    request_budget_check,
    confirm_and_record,
    confirm_event,
    record_denial,
    request_budget_status,
)
from solwyn_cli.commands._cost_filters import CostQueryError
from solwyn_cli.commands._record import (
    CALL_ID_HINT,
    RecordingResult,
    build_event,
    request_ingest,
)
from solwyn_cli.commands._tags import parse_tags, validate_tags
from solwyn_cli.commands.tags import (
    TagDiscoveryResponse,
    TagValuesPage,
    fetch_tags,
    fetch_tag_values,
)
from solwyn_cli.commands.costs import (
    CostRange,
    CostsData,
    fetch_costs,
    fetch_cost_total,
    CostTotalResponse,
)
from solwyn_cli.commands.forecast import ForecastData, fetch_forecast
from solwyn_cli.commands.overview import OverviewData, fetch_overview
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
    "get_forecast",
    "get_costs",
    "list_projects",
    "get_cost_total",
    "list_budget_rules",
    "record_call",
    "list_tags",
    "list_tag_values",
    "list_agent_runs",
    "get_agent_run",
    "list_alert_events",
    "budget_check_batch",
    "get_overview",
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


class _PrivateMcpDiagnostics(logging.Filter):
    """Redact SDK transport/session diagnostics before any handler captures them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.pathname in {sdk_server.__file__, sdk_session.__file__}:
            # The 1.x session emits validation errors directly through root,
            # while the server emits stream exceptions through its own logger.
            # Both may include whole requests and Pydantic input/traceback data.
            record.msg = "MCP protocol diagnostic."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            if hasattr(record, "message"):
                record.message = record.msg
        return True


class _ToolOnlyFastMCP(FastMCP):
    """FastMCP 1.28.1 server that registers only the supported tool handlers."""

    def _setup_handlers(self) -> None:
        # Capabilities derive from the low-level handler registry. Registering only
        # these two handlers prevents unsupported prompt/resource advertisement.
        self._mcp_server.list_tools()(self.list_tools)
        # The 1.x decorator can reflect invalid inputs, outputs and unknown names.
        # Keep validation, dispatch and serialization inside our safe boundary.
        self._mcp_server.request_handlers[types.CallToolRequest] = (
            self._call_tool_request
        )
        # Logger filters run before handlers, including DEBUG/captured handlers.
        # Root filters do not apply to propagated records, so cover both emitters.
        for sdk_logger in (logging.getLogger(), logging.getLogger(sdk_server.__name__)):
            if not any(
                isinstance(item, _PrivateMcpDiagnostics) for item in sdk_logger.filters
            ):
                sdk_logger.addFilter(_PrivateMcpDiagnostics())

    async def _call_tool_request(
        self, request: types.CallToolRequest
    ) -> types.ServerResult:
        return types.ServerResult(
            await self.call_tool(request.params.name, request.params.arguments or {})
        )

    def close_inputs(self) -> None:
        """Apply the same strict argument model at discovery and execution."""
        for tool in self._tool_manager.list_tools():
            argument_model = tool.fn_metadata.arg_model
            argument_model.model_config.update(
                extra="forbid", hide_input_in_errors=True
            )
            argument_model.model_rebuild(force=True)
            tool.parameters = argument_model.model_json_schema(by_alias=True)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return _protocol_result(
                _ToolResult[BaseModel](
                    schema="solwyn.mcp.error.v1",
                    ok=False,
                    error=_ToolError(
                        code="unknown_tool",
                        message="The requested tool is unavailable.",
                        hint="Call list_tools to inspect the available tools.",
                    ),
                )
            )

        result_type = tool.fn_metadata.output_model
        try:
            # Retain the SDK's existing JSON-string compatibility and defaults.
            parsed = tool.fn_metadata.pre_parse_json(arguments)
            inputs = tool.fn_metadata.arg_model.model_validate(parsed)
        except ValidationError as error:
            return _protocol_result(
                _error(
                    result_type,
                    code="invalid_arguments",
                    message="The tool arguments are invalid.",
                    hint=_validation_hint(error, tool.parameters),
                )
            )
        except Exception:
            return _protocol_result(
                _error(
                    result_type,
                    code="invalid_arguments",
                    message="The tool arguments are invalid.",
                    hint="Check the declared input fields and their types in list_tools.",
                )
            )

        try:
            result = await tool.fn(**inputs.model_dump_one_level())
            result = result_type.model_validate(result)
            return _protocol_result(_execution_outcome(result))
        except Exception:
            # Never interpolate, log, or attach a traceback from an operation or
            # output validation failure. The exception may contain private data.
            return _protocol_result(
                _error(
                    result_type,
                    code="internal_error",
                    message="The tool could not complete the operation.",
                    hint="Inspect known operation state before retrying a write; its outcome may be unknown.",
                )
            )


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


class _RecordResult(_ToolResult[RecordingResult]):
    schema_name: Literal["solwyn.mcp.record.v1"] = Field(
        default="solwyn.mcp.record.v1", alias="schema"
    )


class _TagsResult(_ToolResult[TagDiscoveryResponse]):
    schema_name: Literal["solwyn.mcp.tags.v1"] = Field(
        default="solwyn.mcp.tags.v1", alias="schema"
    )


class _TagValuesResult(_ToolResult[TagValuesPage]):
    schema_name: Literal["solwyn.mcp.tags.values.v1"] = Field(
        default="solwyn.mcp.tags.values.v1", alias="schema"
    )


def _tool_tags(tags: dict[str, str] | None) -> dict[str, str] | None:
    # Explicit tool arguments override ambient defaults key by key.
    return validate_tags({**(parse_tags() or {}), **(validate_tags(tags) or {})})


class _BudgetStatusResult(_ToolResult[BudgetStatusData]):
    schema_name: Literal["solwyn.mcp.budget-status.v1"] = Field(
        default="solwyn.mcp.budget-status.v1",
        alias="schema",
    )


class _BudgetRulesResult(_ToolResult[BudgetRulesData]):
    schema_name: Literal["solwyn.budget.rules.v1"] = Field(
        default="solwyn.budget.rules.v1", alias="schema"
    )


class _BudgetCheckResult(_ToolResult[BudgetCheckData]):
    schema_name: Literal["solwyn.mcp.budget-check.v1"] = Field(
        default="solwyn.mcp.budget-check.v1",
        alias="schema",
    )


class _BudgetCheckBatchResult(_ToolResult[BudgetCheckBatchData]):
    schema_name: Literal["solwyn.budget.check.batch.v1"] = Field(
        default="solwyn.budget.check.batch.v1", alias="schema"
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


class _ForecastResult(_ToolResult[ForecastData]):
    schema_name: Literal["solwyn.mcp.forecast.v1"] = Field(
        default="solwyn.mcp.forecast.v1",
        alias="schema",
    )


class _CostsResult(_ToolResult[CostsData]):
    schema_name: Literal["solwyn.mcp.costs.v1"] = Field(
        default="solwyn.mcp.costs.v1",
        alias="schema",
    )


class _AlertEventsResult(_ToolResult[AlertEventListResponse]):
    schema_name: Literal["solwyn.alerts.v1"] = Field(
        default="solwyn.alerts.v1", alias="schema"
    )


class _CostTotalResult(_ToolResult[CostTotalResponse]):
    schema_name: Literal["solwyn.mcp.cost-total.v1"] = Field(
        default="solwyn.mcp.cost-total.v1", alias="schema"
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


class _OverviewResult(_ToolResult[OverviewData]):
    schema_name: Literal["solwyn.mcp.overview.v1"] = Field(
        default="solwyn.mcp.overview.v1", alias="schema"
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
COSTS_DESCRIPTION = "Read project costs grouped by a Core dimension or tag:<key>, with shared cost filters and timezone-aware custom windows."
FORECAST_DESCRIPTION = (
    "Read the configured project's month-to-date spend forecast without "
    "authorizing spend."
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
        "Reserve budget only immediately before the caller-made spend. Denials record attribution by default; no_record skips ingest. "
        f"{shared_sharp_edges}",
        "Settle only a non-null reservation after the caller-made spend. Records attribution with the same call_id by default; no_record skips ingest. Recording failure does not undo settlement. "
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


def _schema_choices(
    schema: dict[str, Any], root: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    if "$ref" in schema:
        yield from _schema_choices(
            root["$defs"][schema["$ref"].rsplit("/", 1)[-1]], root
        )
    elif "anyOf" in schema:
        for choice in schema["anyOf"]:
            yield from _schema_choices(choice, root)
    else:
        yield schema


def _validation_hint(error: ValidationError, schema: dict[str, Any]) -> str:
    """Build correction paths solely from declared schema names, never input keys."""
    paths = set()
    for issue in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        choices = list(_schema_choices(schema, schema))
        path = ""
        for part in issue["loc"]:
            next_schema = None
            for choice in choices:
                if isinstance(part, int) and "items" in choice:
                    next_schema = choice["items"]
                    path += "[]"
                    break
                if isinstance(part, str) and part in choice.get("properties", {}):
                    next_schema = choice["properties"][part]
                    path += ("." if path else "") + part
                    break
            if next_schema is None:
                # Unknown properties and arbitrary dictionary keys stop at their
                # declared parent; even their names may contain private values.
                break
            choices = list(_schema_choices(next_schema, schema))
        paths.add(path or "arguments")
    return (
        "Check fields: "
        + ", ".join(sorted(paths))
        + ". Use only declared fields and their documented types."
    )


def _execution_outcome(result: ResultT) -> ResultT:
    """Retain completed data while exposing execution and recording failures."""
    if isinstance(result.data, BudgetCheckBatchData):
        items = []
        has_errors = False
        for item in result.data.results:
            if item.error_code is not None:
                has_errors = True
                item = item.model_copy(
                    update={
                        "error_code": item.error_code
                        if item.error_code in _SAFE_REMOTE_ERROR_CODES
                        else "api_error",
                        "error_message": "The Solwyn API could not complete this check.",
                    }
                )
            items.append(item)
        data = result.data.model_copy(update={"results": items})
        return result.model_copy(
            update={
                "data": data,
                "ok": result.ok and not has_errors,
                "error": result.error
                or (
                    _ToolError(
                        code="batch_check_failed",
                        message="One or more budget checks failed.",
                        hint="Completed results remain valid; do not replay completed checks.",
                    )
                    if has_errors
                    else None
                ),
            }
        )

    recorded = (
        result.data
        if isinstance(result.data, RecordingResult)
        else result.data.recorded
        if isinstance(result.data, BudgetConfirmData)
        else None
    )
    # A valid denial remains a successful decision even if its optional receipt
    # failed. Standalone recording and confirmation report incomplete execution.
    if recorded is not None and recorded.status in {"rejected", "failed"}:
        settled = isinstance(result.data, BudgetConfirmData)
        return result.model_copy(
            update={
                "ok": False,
                "error": _ToolError(
                    code="recording_rejected"
                    if recorded.status == "rejected"
                    else "recording_failed",
                    message=(
                        "Reservation settlement succeeded, but attribution recording did not complete."
                        if settled
                        else "Attribution recording did not complete."
                    ),
                    hint="Preserve the call identity and retry attribution with the same call_id and attempt; do not reserve again.",
                ),
            }
        )
    return result


def _protocol_result(result: _ToolResult) -> types.CallToolResult:
    payload = result.model_dump(mode="json", by_alias=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=not result.ok,
    )


def _operation_error(
    result_type: type[ResultT],
    error: Exception,
    *,
    invalid_response_code: str,
) -> ResultT:
    if isinstance(error, CostQueryError):
        return _error(
            result_type,
            code="invalid_cost_query",
            message="The cost query was invalid.",
            hint=(
                "Provide paired timezone-aware from_/to bounds with to later than from_; "
                "use unique key=value tags with keys of 1–64 characters, values of at "
                "most 256 characters and no NUL. Keep agent_run at most 256 characters; "
                "tag:<key> uses the same key bounds."
            ),
        )
    if isinstance(error, AgentRunAmbiguousError):
        return _error(
            result_type,
            code="ambiguous_agent_run",
            message="More than one run has that exact name.",
            hint="Call list_agent_runs to inspect visible run names and IDs, then retry with an exact ID.",
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
    if not isinstance(error, (ValidationError, ValueError, TypeError)):
        return _error(
            result_type,
            code="internal_error",
            message="The tool could not complete the operation.",
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


def _alert_events_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
    *,
    account: bool = False,
    **filters: Any,
) -> _AlertEventsResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            data = request_alert_events(
                client=client,
                project_id=None
                if account
                else _project_id(command_runtime, project_id),
                account=account,
                **filters,
            )
        return _success(_AlertEventsResult, data)
    except AlertQueryError:
        return _error(
            _AlertEventsResult,
            code="invalid_alert_query",
            message="The alert query was invalid.",
            hint="Use range 7d|30d|90d or paired timezone-aware from_/to bounds; scope and rule_type allow at most 32 characters and no NUL.",
        )
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _AlertEventsResult, error, invalid_response_code="invalid_alerts_response"
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


def _budget_rules_operation(
    project_id: str | None, launch_options: McpLaunchOptions
) -> _BudgetRulesResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            data = request_budget_rules(
                client=client, project_id=_project_id(command_runtime, project_id)
            )
        return _success(_BudgetRulesResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetRulesResult,
            error,
            invalid_response_code="invalid_budget_rules_response",
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
    tags: dict[str, str] | None = None,
    estimated_output_tokens: int | None = None,
    no_record: bool = False,
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
        resolved_run_id = (
            agent_run_id
            if agent_run_id is not None
            else os.environ.get("SOLWYN_AGENT_RUN_ID")
        )
        request = BudgetCheckRequest(
            price_hints_version="1",
            run_directive_version="1" if resolved_run_id is not None else None,
            estimated_input_tokens=estimated_input_tokens,
            model=model,
            provider=provider,
            estimated_media=estimated_media,
            fallback_providers=fallback_providers,
            fallback_models=fallback_models,
            agent_run_id=resolved_run_id,
            tags=_tool_tags(tags),
        )
    except (ValidationError, ValueError):
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
            data = record_denial(
                client=client,
                request=request,
                data=data,
                estimated_output_tokens=estimated_output_tokens,
                no_record=no_record,
            )
        return _success(_BudgetCheckResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetCheckResult,
            error,
            invalid_response_code="invalid_budget_check_response",
        )


def _budget_check_batch_operation(
    *,
    launch_options: McpLaunchOptions,
    checks: list[dict[str, Any]],
    no_record: bool,
    estimated_output_tokens: int | None,
) -> _BudgetCheckBatchResult:
    try:
        prepared = prepare_batch_checks(checks)
    except (ValidationError, ValueError, TypeError):
        return _error(
            _BudgetCheckBatchResult,
            code="invalid_budget_check_batch_request",
            message="Batch input must be a non-empty array of valid check bodies.",
        )
    try:
        with tool_call_client(launch_options) as (_, client):
            auth_error = _require_project_key(
                _BudgetCheckBatchResult, client.current_credential, "checks"
            )
            if auth_error is not None:
                return auth_error
            data = request_budget_check_batch(
                client=client,
                checks=prepared,
                no_record=no_record,
                estimated_output_tokens=estimated_output_tokens,
            )
        return _success(_BudgetCheckBatchResult, data)
    except BudgetCheckBatchFailure as error:
        failure = _operation_error(
            _BudgetCheckBatchResult,
            error.cause,
            invalid_response_code="invalid_budget_check_batch_response",
        )
        tool_error = failure.error.model_copy(
            update={"hint": " ".join(filter(None, (failure.error.hint, error.hint)))}
        )
        return _BudgetCheckBatchResult(ok=False, data=error.data, error=tool_error)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetCheckBatchResult,
            error,
            invalid_response_code="invalid_budget_check_batch_response",
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
    latency_ms: float = 0,
    status: str = "success",
    agent_run_id: str | None = None,
    parent_run_id: str | None = None,
    run_name: str | None = None,
    tags: dict[str, str] | None = None,
    no_record: bool = False,
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
        event = confirm_event(
            request=request,
            latency_ms=latency_ms,
            status=status,
            agent_run_id=agent_run_id
            if agent_run_id is not None
            else os.environ.get("SOLWYN_AGENT_RUN_ID"),
            parent_agent_run_id=parent_run_id,
            agent_run_name=run_name,
            tags=_tool_tags(tags),
        )
    except (ValidationError, ValueError):
        return _error(
            _BudgetConfirmResult,
            code="invalid_budget_confirm_request",
            message="The budget confirmation request is invalid.",
            hint=CALL_ID_HINT,
        )
    try:
        with tool_call_client(launch_options) as (_, client):
            auth_error = _require_project_key(
                _BudgetConfirmResult, client.current_credential, "confirmation"
            )
            if auth_error is not None:
                return auth_error
            data = confirm_and_record(
                client=client, request=request, event=event, no_record=no_record
            )
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


def _forecast_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
) -> _ForecastResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = fetch_forecast(client=client, project_id=resolved_project)
        return _success(_ForecastResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _ForecastResult,
            error,
            invalid_response_code="invalid_forecast_response",
        )


def _costs_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    time_range: CostRange,
    group_by: str,
    **filters: Any,
) -> _CostsResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            resolved_project = _project_id(command_runtime, project_id)
            data = fetch_costs(
                client=client,
                project_id=resolved_project,
                time_range=time_range,
                group_by=group_by,
                **filters,
            )
        return _success(_CostsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _CostsResult,
            error,
            invalid_response_code="invalid_costs_response",
        )


def _cost_total_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    time_range: str = "7d",
    **filters: Any,
) -> _CostTotalResult:
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            data = fetch_cost_total(
                client=client,
                project_id=_project_id(command_runtime, project_id),
                time_range=time_range,
                **filters,
            )
        return _success(_CostTotalResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _CostTotalResult, error, invalid_response_code="invalid_costs_response"
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


def _record_operation(
    *, launch_options: McpLaunchOptions, inputs: dict[str, object]
) -> _RecordResult:
    try:
        inputs["tags"] = _tool_tags(inputs.get("tags"))
        if inputs.get("agent_run_id") is None:
            inputs["agent_run_id"] = os.environ.get("SOLWYN_AGENT_RUN_ID")
        event = build_event(
            **{key: value for key, value in inputs.items() if value is not None}
        )
    except (ValueError, TypeError):
        return _error(
            _RecordResult,
            code="invalid_record_request",
            message="The recording request is invalid.",
            hint=CALL_ID_HINT,
        )
    try:
        with tool_call_client(launch_options) as (_, client):
            auth_error = _require_project_key(
                _RecordResult, client.current_credential, "recording"
            )
            if auth_error is not None:
                return auth_error
            data = request_ingest(client=client, events=[event])
        return _success(_RecordResult, data)
    except (ApiError, AuthError, ValueError, TypeError) as error:
        result = _operation_error(
            _RecordResult, error, invalid_response_code="invalid_ingest_response"
        )
        if result.error is not None:
            retry_hint = (
                f"Reuse call_id {event.call_id} when retrying this logical call."
            )
            result = result.model_copy(
                update={
                    "error": result.error.model_copy(
                        update={
                            "hint": f"{result.error.hint} {retry_hint}"
                            if result.error.hint
                            else retry_hint
                        }
                    )
                }
            )
        return result


def _tags_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    key: str | None = None,
    prefix: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
):
    result_class = _TagsResult if key is None else _TagValuesResult
    try:
        if key is not None:
            validate_tags({key: prefix or ""})
    except ValueError:
        return _error(
            result_class, code="invalid_tag_query", message="Invalid tag key or prefix."
        )
    try:
        with tool_call_client(launch_options) as (command_runtime, client):
            project = _project_id(command_runtime, project_id)
            data = (
                fetch_tags(client=client, project_id=project)
                if key is None
                else fetch_tag_values(
                    client=client,
                    project_id=project,
                    key=key,
                    prefix=prefix,
                    limit=limit,
                    cursor=cursor,
                )
            )
        return _success(result_class, data)
    except (ApiError, AuthError, ValueError, TypeError) as error:
        return _operation_error(
            result_class, error, invalid_response_code="invalid_tags_response"
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
            code=(
                str(error)
                if str(error)
                in {"custom_window_pair_required", "custom_window_invalid"}
                else "custom_window_invalid"
            ),
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
            if from_time is not None:
                message = (
                    "Custom windows longer than "
                    f"{allowed[-1].removesuffix('d')} days are not available on "
                    "your tier."
                    if allowed
                    else "The requested custom window is not available on your tier."
                )
            else:
                message = f"Range '{time_range.value}' is not available on your tier."
            return _error(
                _AgentRunsResult,
                code="upgrade_required",
                message=message,
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


def _overview_operation(launch_options: McpLaunchOptions) -> _OverviewResult:
    try:
        with tool_call_client(launch_options) as (_, client):
            if isinstance(client.current_credential, ProjectKeyCredential):
                return _error(
                    _OverviewResult,
                    code="authentication_required",
                    message="User login is required to read the account overview.",
                    hint="Unset SOLWYN_API_KEY and run solwyn login, then retry.",
                )
            data = fetch_overview(client=client)
        return _success(_OverviewResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _OverviewResult,
            error,
            invalid_response_code="invalid_overview_response",
        )


def create_server(*, launch_options: McpLaunchOptions | None = None) -> FastMCP:
    """Build the stateless local MCP server and register the supported tools."""
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
        tags: dict[str, str] | None = None,
        estimated_output_tokens: Annotated[
            int | None, Field(ge=0, le=100_000_000)
        ] = None,
        no_record: bool = False,
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
                tags=tags,
                estimated_output_tokens=estimated_output_tokens,
                no_record=no_record,
            )
        )

    @server.tool(
        name=TOOL_NAMES[2],
        description=confirm_description,
        structured_output=True,
    )
    async def budget_confirm(
        reservation_id: Annotated[str, Field(min_length=1)],
        call_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=36,
                pattern=CALL_ID_PATTERN,
                description=CALL_ID_HINT,
            ),
        ],
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
        latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        status: Literal["success", "error"] = "success",
        agent_run_id: BudgetAgentRunId | None = None,
        parent_run_id: BudgetAgentRunId | None = None,
        run_name: Annotated[str | None, Field(max_length=255)] = None,
        tags: dict[str, str] | None = None,
        no_record: bool = False,
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
                latency_ms=latency_ms,
                status=status,
                agent_run_id=agent_run_id,
                parent_run_id=parent_run_id,
                run_name=run_name,
                tags=tags,
                no_record=no_record,
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
        description=FORECAST_DESCRIPTION,
        structured_output=True,
    )
    async def get_forecast(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _ForecastResult:
        return await anyio.to_thread.run_sync(
            partial(_forecast_operation, project_id, launch)
        )

    @server.tool(
        name=TOOL_NAMES[5],
        description=COSTS_DESCRIPTION,
        structured_output=True,
    )
    async def get_costs(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        time_range: CostRange = CostRange.SEVEN_DAYS,
        group_by: str = "model",
        from_: str | None = None,
        to: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        service_tier: str | None = None,
        provider_region: str | None = None,
        agent_run: str | None = None,
        failover_reason: str | None = None,
        model_fallback: bool | None = None,
        provider_fallback: bool | None = None,
        tag: list[str] | None = None,
    ) -> _CostsResult:
        return await anyio.to_thread.run_sync(
            partial(
                _costs_operation,
                launch_options=launch,
                project_id=project_id,
                time_range=time_range,
                group_by=group_by,
                from_=from_,
                to=to,
                model=model,
                provider=provider,
                modality=modality,
                status=status,
                service_tier=service_tier,
                provider_region=provider_region,
                agent_run=agent_run,
                failover_reason=failover_reason,
                model_fallback=model_fallback,
                provider_fallback=provider_fallback,
                tag=tag,
            )
        )

    @server.tool(
        name=TOOL_NAMES[6],
        description=PROJECTS_DESCRIPTION,
        structured_output=True,
    )
    async def list_projects() -> _ProjectsResult:
        return await anyio.to_thread.run_sync(partial(_projects_operation, launch))

    @server.tool(
        name="get_cost_total",
        description="Read uncapped project cost totals with the same cost filters and custom windows as get_costs.",
        structured_output=True,
    )
    async def get_cost_total(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        time_range: CostRange = CostRange.SEVEN_DAYS,
        from_: str | None = None,
        to: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        service_tier: str | None = None,
        provider_region: str | None = None,
        agent_run: str | None = None,
        failover_reason: str | None = None,
        model_fallback: bool | None = None,
        provider_fallback: bool | None = None,
        tag: list[str] | None = None,
    ) -> _CostTotalResult:
        return await anyio.to_thread.run_sync(
            partial(
                _cost_total_operation,
                launch_options=launch,
                project_id=project_id,
                time_range=time_range,
                from_=from_,
                to=to,
                model=model,
                provider=provider,
                modality=modality,
                status=status,
                service_tier=service_tier,
                provider_region=provider_region,
                agent_run=agent_run,
                failover_reason=failover_reason,
                model_fallback=model_fallback,
                provider_fallback=provider_fallback,
                tag=tag,
            )
        )

    @server.tool(
        name="list_budget_rules",
        description="Read budget rule conditions, routing, and separate paused/snoozed states. A snoozed hard_deny still denies; pausing releases enforcement. Rule keys change when keyed fields are edited.",
        structured_output=True,
    )
    async def list_budget_rules(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _BudgetRulesResult:
        return await anyio.to_thread.run_sync(
            partial(_budget_rules_operation, project_id, launch)
        )

    @server.tool(
        name="record_call",
        description="Record one caller-owned completed call without a reservation. Do not duplicate SDK events. Reuse call_id on retries; costs are server-priced. Inspect status/rejected/duplicates even when transport succeeds.",
        structured_output=True,
    )
    async def record_call(
        model: BudgetCheckModel,
        provider: ProviderName,
        call_id: Annotated[
            str | None,
            Field(pattern=CALL_ID_PATTERN, max_length=36, description=CALL_ID_HINT),
        ] = None,
        input_tokens: Annotated[int | None, Field(ge=0, le=100_000_000)] = None,
        output_tokens: Annotated[int | None, Field(ge=0, le=100_000_000)] = None,
        token_details: TokenDetails | None = None,
        media_usage: MediaUsage | None = None,
        latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
        status: Literal["success", "error"] = "success",
        modality: Modality = Modality.TEXT,
        provider_region: Annotated[str | None, Field(max_length=32)] = None,
        service_tier: ServiceTier | None = None,
        agent_run_id: BudgetAgentRunId | None = None,
        parent_agent_run_id: BudgetAgentRunId | None = None,
        agent_run_name: Annotated[str | None, Field(max_length=255)] = None,
        tags: dict[str, str] | None = None,
    ) -> _RecordResult:
        inputs = dict(
            model=model,
            provider=provider,
            call_id=call_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_details=token_details,
            media_usage=media_usage,
            latency_ms=latency_ms,
            status=status,
            modality=modality,
            provider_region=provider_region,
            service_tier=service_tier,
            agent_run_id=agent_run_id,
            parent_agent_run_id=parent_agent_run_id,
            agent_run_name=agent_run_name,
            tags=tags,
        )
        return await anyio.to_thread.run_sync(
            partial(_record_operation, launch_options=launch, inputs=inputs)
        )

    @server.tool(
        name="list_tags",
        description="List account-scoped tag discovery through a project. Team required; keys preserve whitespace and case.",
        structured_output=True,
    )
    async def list_tags(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _TagsResult:
        return await anyio.to_thread.run_sync(
            partial(_tags_operation, launch_options=launch, project_id=project_id)
        )

    @server.tool(
        name="list_tag_values",
        description="Read one page of account-scoped values for a verbatim tag key. Team required; prefix is case-sensitive.",
        structured_output=True,
    )
    async def list_tag_values(
        key: Annotated[str, Field(min_length=1, max_length=64)],
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        prefix: Annotated[str | None, Field(max_length=256)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> _TagValuesResult:
        return await anyio.to_thread.run_sync(
            partial(
                _tags_operation,
                launch_options=launch,
                project_id=project_id,
                key=key,
                prefix=prefix,
                limit=limit,
                cursor=cursor,
            )
        )

    @server.tool(
        name="list_agent_runs",
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
        name="get_agent_run",
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

    @server.tool(
        name="list_alert_events",
        description="Read cursor-paginated alert history and per-channel delivery receipts. Project keys work for project history; account history requires JWT. failed_only filters partial/failed rollups locally; scope is the rule scope. No mutation tools are exposed.",
        structured_output=True,
    )
    async def list_alert_events(
        project_id: Annotated[str | None, Field(min_length=1)] = None,
        account: bool = False,
        time_range: str = "30d",
        from_: str | None = None,
        to: str | None = None,
        rule_type: str | None = None,
        scope: str | None = None,
        failed_only: bool = False,
        cursor: str | None = None,
        all_pages: bool = False,
    ) -> _AlertEventsResult:
        return await anyio.to_thread.run_sync(
            partial(
                _alert_events_operation,
                project_id,
                launch,
                account=account,
                time_range=time_range,
                from_=from_,
                to=to,
                rule_type=rule_type,
                scope=scope,
                failed_only=failed_only,
                cursor=cursor,
                all_pages=all_pages,
            )
        )

    @server.tool(
        name="budget_check_batch",
        description=(
            "Reserve budget immediately before multiple caller-made un-instrumented provider calls. "
            "Checks are JSON check bodies, fully validated before submission and chunked at 32; "
            "results preserve input order. Each allowed item holds its own reservation: confirm it "
            "after its call. Unknown models return per-item errors; 404/503 fail the request. "
            "Denials record one receipt unless no_record; stopped runs must not retry under a new run ID. "
            "Admission counts each item against checks_per_minute. Do not poll or replay to refresh reservations."
        ),
        structured_output=True,
    )
    async def budget_check_batch(
        checks: Annotated[list[BudgetCheckRequest], Field(min_length=1)],
        no_record: bool = False,
        estimated_output_tokens: Annotated[
            int | None, Field(ge=0, le=100_000_000)
        ] = None,
    ) -> _BudgetCheckBatchResult:
        return await anyio.to_thread.run_sync(
            partial(
                _budget_check_batch_operation,
                launch_options=launch,
                checks=[
                    check.model_dump(mode="json", exclude_unset=True)
                    for check in checks
                ],
                no_record=no_record,
                estimated_output_tokens=estimated_output_tokens,
            )
        )

    @server.tool(
        name="get_overview",
        description=(
            "Read calendar-month account spend, requests, trend, savings, and "
            "free-tier quota metadata. Requires login/JWT authentication."
        ),
        structured_output=True,
    )
    async def get_overview() -> _OverviewResult:
        return await anyio.to_thread.run_sync(partial(_overview_operation, launch))

    server.close_inputs()
    return server
