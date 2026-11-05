"""Public MCP SDK server and the supported Solwyn tool surface."""

import json
import inspect
import logging
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
from functools import partial
from typing import Any, Annotated, Generic, Literal, TypeVar

from mcp import MCPError, types
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server import runner as sdk_runner
from mcp.shared import jsonrpc_dispatcher as sdk_dispatcher
from mcp.shared import direct_dispatcher as sdk_direct_dispatcher
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_validator,
)

from solwyn_cli import __version__, auth, credential_store, project_resolver, runtime
from solwyn_cli.mcp.declarations import ToolDeclaration
from solwyn_cli import _mcp_operation as operation
from solwyn_cli._vendor.batch import new_operation_id, operation_expiry
from solwyn_cli._vendor.validation import PROJECT_ID_HEADER, PROJECT_ID_PATTERN
from solwyn_cli.mcp.parameters import (
    BudgetCheckRequest,
    MediaUsage,
    TokenDetails,
    parameter_descriptions,
    DenialRecordingRetry,
    ConfirmationRecordingRetry,
)
from solwyn_cli._vendor.wire import (
    CALL_ID_PATTERN,
    Modality,
    BudgetConfirmRequest,
    ProviderName,
    ServiceTier,
)
from solwyn_cli.artifacts import agent_guidance
from solwyn_cli.auth import (
    AuthError,
    CredentialClass,
    LoginCredential,
    ApiKeyCredential,
    LoginRequiredError,
    LOGIN_REQUIRED_HINT,
    METERING_KEY_HINT,
    CredentialStorageError,
    MeteringKeyRequiredError,
    ResolvedCredential,
    missing_credential_hint,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands.alerts import (
    AlertEventListResponse,
    AlertQueryError,
    request_alert_events,
)
from solwyn_cli.commands.budget_rules import (
    BudgetRulesData,
    request_budget_rules,
)
from solwyn_cli.commands.budget import (
    BudgetCheckData,
    BudgetCheckBatchData,
    prepare_batch_checks,
    request_recoverable_budget_batch,
    RecoverableBatchData,
    BudgetConfirmData,
    BudgetStatusData,
    request_budget_check,
    confirm_and_record,
    confirm_event,
    prepare_confirm_recording_retry,
    record_confirm_only,
    record_denial,
    request_budget_status,
)
from solwyn_cli.commands._cost_filters import (
    CostQueryError,
    AccountProjectConflictError,
    UnsupportedAccountCostFilterError,
)
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
from solwyn_cli.errors import (
    API_KEY_HINTS,
    ApiError,
    BILLING_HINTS,
    BUDGET_BUSY_HINT,
    CLI_CREDENTIAL_HINTS,
    INGEST_BUSY_HINT,
    INGEST_UNKNOWN_HINT,
    APPROVAL_INVALID_MESSAGES,
    LEGACY_BATCH_BUSY_HINT,
    MEMBERSHIP_RECOVERY_HINT,
    SERVICE_BUSY_HINT,
    WRITE_BUSY_HINT,
    approval_url_from,
    redact_secrets,
)


from solwyn_cli.commands._team import (
    READ_HINT,
    TeamMembersData,
    TeamInvitesData,
    fetch_members,
    fetch_invites,
)
from solwyn_cli.commands._account_reads import (
    AccountPhoneResponse,
    UncountedTallyResponse,
    fetch_phone,
    fetch_uncounted,
)
from solwyn_cli.commands._alert_reads import (
    AlertOpenResponse,
    AccountAlertWithLatestResponse,
    AlertRange,
    ActivityType,
    request_account_alert,
    request_account_alert_page,
)
from solwyn_cli.commands.alerts import AccountAlertResponse
from solwyn_cli.commands._entitlement import (
    BillingCatalogResponse,
    fetch_billing_catalog,
)
from solwyn_cli.commands.plan import _PlanData, fetch_plan
from solwyn_cli.commands.whoami import _WhoamiData, fetch_identity
from solwyn_cli.commands._cost_advice_models import (
    BaselineResult,
    UnitEconomicsResponse,
    CostDriftResponse,
    PricedModelsResponse,
    CostSimulationResponse,
)
from solwyn_cli.commands._cost_models import (
    CostEventsResponse,
    AccountCostQueryResponse,
)
from solwyn_cli.commands._cost_filters import window_params
from solwyn_cli.commands.costs import fetch_account_costs
from solwyn_cli.commands._cost_reads import (
    fetch_cost_events,
    fetch_priced_models,
    fetch_project_read,
    fetch_simulation,
    simulation_request,
)
from solwyn_cli.commands.providers import (
    ProviderFailoversData,
    UntrackedSurfacesData,
    FailoverRange,
    UntrackedRange,
    fetch_provider_failovers,
    fetch_untracked_surfaces,
)
from solwyn_cli.commands._billing import (
    PaidTier,
    BillingInterval,
    PlanPreviewResponse,
    fetch_plan_preview,
)
from solwyn_cli.commands.audit import (
    AuditRange,
    AuditEventListResponse,
    fetch_audit_events,
)


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
    "list_cost_events",
    "get_cost_baseline",
    "get_unit_economics",
    "get_cost_drift",
    "list_priced_models",
    "simulate_costs",
    "list_provider_failovers",
    "list_untracked_surfaces",
    "list_audit_events",
    "plan_status",
    "plan_catalog",
    "get_whoami",
    "get_account_phone",
    "list_alert_activity",
    "list_open_alert_deliveries",
    "budget_uncounted",
    "get_account_alert",
    "plan_preview",
    "list_team_members",
    "list_team_invites",
)
_SAFE_REMOTE_ERROR_CODES = frozenset(
    {
        "billing_plan_no_change",
        "billing_subscription_not_found",
        "billing_preview_unavailable",
        "authentication_failed",
        "authentication_required",
        "budget_denied",
        "email_not_verified",
        "email_provider_unavailable",
        "forbidden",
        "invalid_project",
        "invalid_token",
        "not_found",
        "operation_expired",
        "operation_not_found",
        "operation_conflict",
        "operation_in_future",
        "outcome_unavailable",
        "admission_failed",
        "invalid_operation_id",
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
        "approval_required",
        "approval_invalid",
        "approval_denied",
        "approval_expired",
        "approval_limit_reached",
        "session_required",
        "cli_credential_invalid",
        "cli_credential_expired",
        "cli_credential_revoked",
        # The API-key path (Core spec 2.4): fixed messages, CLI-owned hints.
        "api_key_invalid",
        "api_key_expired",
        "api_key_not_allowed",
        "key_not_dedicated",
        "project_selector_required",
        "project_selector_conflict",
    }
)
_SAFE_REMOTE_HINTS = {
    "cli_credential_invalid": CLI_CREDENTIAL_HINTS["cli_credential_invalid"],
    "cli_credential_expired": CLI_CREDENTIAL_HINTS["cli_credential_expired"],
    "cli_credential_revoked": CLI_CREDENTIAL_HINTS["cli_credential_revoked"],
    "session_required": "This action is only available in the dashboard.",
    "approval_limit_reached": (
        "Approve or deny pending approvals in the browser, or wait for them to "
        "expire, then retry."
    ),
    "approval_invalid": "Run the CLI command again to request a new approval.",
    "approval_expired": "Run the CLI command again to request a new approval.",
    "billing_plan_no_change": BILLING_HINTS["billing_plan_no_change"],
    "billing_subscription_not_found": BILLING_HINTS["billing_subscription_not_found"],
    "billing_preview_unavailable": BILLING_HINTS["billing_preview_unavailable"],
    "service_busy": SERVICE_BUSY_HINT,
    "account_membership_revoked": MEMBERSHIP_RECOVERY_HINT,
    "outcome_unavailable": "Recover with the same operation_id using recover_only; admission may have applied.",
    "authentication_failed": "Run solwyn login again, then retry.",
    "authentication_required": "Run solwyn login, then retry.",
    "forbidden": "Check account access for this operation, then retry.",
    "invalid_token": "Run solwyn login again, then retry.",
    # Rotation is login-only and destructive, so the agent-facing hint stops at the
    # remedy an agent can actually apply and at a read-only way to inspect scope.
    "read_only_key": (
        "The configured project key is read-only; set SOLWYN_API_KEY to a "
        "write-scoped key, then retry. Confirm a key's scope with "
        "solwyn keys list."
    ),
    "api_key_invalid": API_KEY_HINTS["api_key_invalid"],
    "api_key_expired": API_KEY_HINTS["api_key_expired"],
    "api_key_not_allowed": API_KEY_HINTS["api_key_not_allowed"],
    "key_not_dedicated": API_KEY_HINTS["key_not_dedicated"],
    "project_selector_required": (
        "Restart the server with --project before mcp, SOLWYN_PROJECT, or the "
        "repo's .solwyn.env naming the project."
    ),
    "project_selector_conflict": API_KEY_HINTS["project_selector_conflict"],
    "project_not_found": (
        "Check the project name with solwyn projects list, or use the proj_ id. "
        "A key only reaches the projects it was created for."
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
# Server free text is never relayed to an agent: each known code gets a fixed
# CLI-owned message, filled only from validated structured detail fields.
_REMOTE_REJECTED_MESSAGE = "The Solwyn API rejected the request."
_REMOTE_ERROR_MESSAGES = {
    "account_membership_revoked": "The account membership for this login was revoked.",
    "admission_failed": "Budget admission failed for this operation.",
    "approval_denied": "The change was denied in the browser.",
    "approval_expired": "The approval expired.",
    "approval_invalid": "The approval is not valid for this request.",
    "approval_limit_reached": "Too many approvals are pending for this login.",
    "approval_required": "This change needs a human's approval in the browser.",
    "api_key_expired": "The API key expired.",
    "api_key_invalid": "The API key is invalid or revoked.",
    "api_key_not_allowed": "API keys cannot make this change or read this data.",
    "key_not_dedicated": (
        "This project only accepts API keys dedicated to it, and this key also "
        "reaches other projects."
    ),
    "project_selector_conflict": "The project selection conflicts with the request.",
    "project_selector_required": "This API key needs a project.",
    "authentication_failed": "Solwyn rejected the credential.",
    "authentication_required": "Solwyn authentication is required.",
    "billing_plan_no_change": "The account is already on the requested plan.",
    "billing_preview_unavailable": "The plan change preview is unavailable.",
    "billing_subscription_not_found": "The account has no paid subscription.",
    "budget_denied": "The budget denied this request.",
    "cli_credential_expired": "The Solwyn login expired.",
    "cli_credential_invalid": "The Solwyn login is not valid.",
    "cli_credential_revoked": "The Solwyn login was revoked.",
    "email_not_verified": "The account email address is not verified.",
    "email_provider_unavailable": "The email provider is unavailable.",
    "forbidden": "This credential cannot perform this operation.",
    "invalid_operation_id": "The operation ID is not valid.",
    "invalid_project": "The project is not valid for this credential.",
    "invalid_token": "The credential is not valid.",
    "not_found": "The requested resource was not found.",
    "operation_conflict": "The operation ID was already used for a different request.",
    "operation_expired": "The operation expired.",
    "operation_in_future": "The operation time is in the future.",
    "operation_not_found": "The operation was not found.",
    "outcome_unavailable": "The operation outcome is not available.",
    "project_limit_reached": "Project limit reached for the current plan.",
    "project_not_found": "The project was not found.",
    "quota_exceeded": (
        "Monthly request quota reached. Cost analytics pause until the month "
        "resets; calls and enforcement continue."
    ),
    "rate_limited": "The Solwyn API rate limit was reached.",
    "read_only_key": "The configured project key is read-only.",
    "reservation_not_found": "The reservation was not found.",
    "session_required": "This action requires a dashboard session.",
    "token_expired": "The credential expired.",
    "unauthorized": "The credential is not authorized for this request.",
    "unknown_model": "Solwyn does not recognize this model.",
    "unknown_notification_channel": "A notification channel was not found.",
    "upgrade_required": "This feature requires a higher plan tier.",
}
_TIER_NAMES = {
    "free": "Free",
    "production": "Production",
    "team": "Team",
    "scale": "Scale",
}
_MAX_REMOTE_COUNT = 10**12


class _PrivateMcpDiagnostics(logging.Filter):
    """Redact SDK transport/session diagnostics before any handler captures them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.pathname in _SDK_DIAGNOSTIC_PATHS:
            # SDK 2 runner and dispatchers may log raw frames, method names,
            # or transport exceptions before the Solwyn callback is reached.
            record.msg = "MCP protocol diagnostic."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            if hasattr(record, "message"):
                record.message = record.msg
        return True


# Narrow source allowlist for the pinned SDK's stdio/legacy/current emitters.
# No application logger is disabled and severity remains unchanged.
_SDK_DIAGNOSTIC_MODULES = (sdk_runner, sdk_dispatcher, sdk_direct_dispatcher)
_SDK_DIAGNOSTIC_PATHS = frozenset(module.__file__ for module in _SDK_DIAGNOSTIC_MODULES)


async def _private_protocol_errors(
    ctx: ServerRequestContext, call_next: CallNext
) -> HandlerResult:
    """Keep SDK lookup errors useful without reflecting arbitrary method text."""
    try:
        return await call_next(ctx)
    except MCPError as error:
        if error.code == types.METHOD_NOT_FOUND:
            raise MCPError(
                code=types.METHOD_NOT_FOUND, message="Method not found"
            ) from None
        raise


def _configure_private_server(server: Server) -> None:
    """Adapt only this newly constructed low-level SDK 2.1.1 server."""
    # Public but provisional across 2.x minor releases: retain the exact pin.
    # The low-level server installs only inbound telemetry by default. Clear it
    # before adding any Solwyn middleware; never reset the host tracer/propagator.
    # Replies only: outbound MCP requests have separate SDK instrumentation.
    server.middleware.clear()
    # Public middleware wraps lookup before the tool callback in both eras.
    server.middleware.append(_private_protocol_errors)
    for module in _SDK_DIAGNOSTIC_MODULES:
        sdk_logger = logging.getLogger(module.__name__)
        if not any(
            isinstance(item, _PrivateMcpDiagnostics) for item in sdk_logger.filters
        ):
            sdk_logger.addFilter(_PrivateMcpDiagnostics())


class _ToolCatalog:
    """One ordered declaration set for public discovery and safe invocation."""

    def __init__(self, profile: Literal["read", "guardrails"]) -> None:
        self.tools: dict[str, ToolDeclaration] = {}
        self._profile = profile

    def tool(
        self,
        *,
        name: str,
        title: str,
        description: str,
        read_only: bool = True,
        credential_requirement: Literal["login", "project_key", "either"] = "either",
        credential_note: str | None = None,
    ):
        def register(fn):
            if self._profile == "read" and not read_only:
                return fn
            note = (
                credential_note
                or {
                    "login": "Requires a Solwyn login.",
                    "project_key": "Requires a write-scoped project API key; writes use its project, not the selected read project.",
                    "either": "Accepts a Solwyn login or a project API key with access to the selected project.",
                }[credential_requirement]
            )
            if name in self.tools:
                raise ValueError("Duplicate tool declaration")
            self.tools[name] = ToolDeclaration.from_function(
                fn,
                name=name,
                title=title,
                description=description,
                parameters=parameter_descriptions(
                    name, list(inspect.signature(fn).parameters)
                ),
                read_only=read_only,
                credential_requirement=credential_requirement,
                credential_note=note,
            )
            return fn

        return register

    async def list_tools(
        self, ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[tool.definition for tool in self.tools.values()],
            ttl_ms=60_000,
            cache_scope="private",
        )

    async def call_tool(
        self, ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        tool = self.tools.get(params.name)
        if tool is None:
            raise MCPError(code=-32602, message="Unknown tool")
        control = None
        try:
            async with operation.operation_scope() as control:
                if params.name == "budget_check_batch":
                    arguments = params.arguments or {}
                    identifier = arguments.get("operation_id")
                    if (
                        identifier is None
                        and arguments.get("checks") is not None
                        and not arguments.get("recover_only")
                        and arguments.get("record_only") is None
                    ):
                        identifier = new_operation_id()
                    if isinstance(identifier, str):
                        try:
                            operation_expiry(identifier)
                        except (ValueError, OverflowError):
                            pass
                        else:
                            control.remember(
                                operation_id=identifier, state="not_submitted"
                            )
                result = await self._call_tool(ctx, params)
                # Serialization is CPU work too; the same deadline owns it.
                wire = await operation.run_sync(
                    partial(_bounded_protocol_result, result, control)
                )
                _exit_if_stranded()
                return wire
        except (operation.OperationLimit, TimeoutError) as error:
            code = (
                error.code
                if isinstance(error, operation.OperationLimit)
                else "operation_deadline"
            )
            result = _error(
                tool.output_model,
                code=code,
                message="The tool reached its fixed work limit.",
                hint="Preserve known identities and the continuation cursor. Recover batch outcomes with the same operation_id; never replay an ambiguous write.",
            )
            return _bounded_protocol_result(result, control)

    async def _call_tool(
        self, ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> "_ToolResult":
        tool = self.tools.get(params.name)
        if tool is None:
            raise MCPError(code=-32602, message="Unknown tool")
        arguments = params.arguments or {}
        result_type = tool.output_model
        try:
            # Retain the SDK's existing JSON-string compatibility and defaults.
            inputs = await operation.run_sync(
                lambda: tool.input_model.model_validate(
                    tool.parse_json_arguments(arguments)
                )
            )
        except ValidationError as error:
            return _error(
                result_type,
                code="invalid_arguments",
                message="The tool arguments are invalid.",
                hint=await operation.run_sync(
                    partial(_validation_hint, error, tool.definition.input_schema)
                ),
            )
        except (operation.OperationLimit, TimeoutError):
            raise
        except Exception:
            return _error(
                result_type,
                code="invalid_arguments",
                message="The tool arguments are invalid.",
                hint="Check the declared input fields and their types in list_tools.",
            )

        if (
            params.name == "budget_confirm"
            and inputs.record_only is not None
            and inputs.model_fields_set != {"record_only"}
        ):
            return _error(
                result_type,
                code="invalid_budget_confirm_request",
                message="Use record_only without other confirmation arguments.",
                hint="Supply only the exact recording_retry event from the original result.",
            )

        try:
            result = await tool.fn(
                **{name: getattr(inputs, name) for name in type(inputs).model_fields}
            )
            result = await operation.run_sync(
                lambda: result_type.model_validate(result)
            )
            return _execution_outcome(result)
        except (operation.OperationLimit, TimeoutError):
            raise
        except Exception:
            # Never interpolate, log, or attach a traceback from an operation or
            # output validation failure. The exception may contain private data.
            failure = _error(
                result_type,
                code="internal_error",
                message="The tool could not complete the operation.",
                hint="Inspect known operation state before retrying a write; its outcome may be unknown.",
            )
            return failure


class McpLaunchOptions(BaseModel):
    """Immutable non-secret CLI overrides retained for fresh per-call resolution.

    ``project_id`` is the global ``--project`` given before ``mcp`` (a name or
    an id). The reporting tools bind their project and key once per server
    (``_reporting``); read tools resolve per call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str | None = None
    project_id: str | None = None
    profile: Literal["read", "guardrails"] = "guardrails"
    _reporting: "_ReportingBinding" = PrivateAttr(
        default_factory=lambda: _ReportingBinding()
    )


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
    recovery: operation.ReadRecovery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_outcome(self) -> "_ToolResult[DataT]":
        if self.ok != (self.data is not None and self.error is None):
            raise ValueError("Tool result must contain exactly one outcome")
        return self


class _RecordResult(_ToolResult[RecordingResult]):
    project_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="The project Core says the call was for (Solwyn-Project-Id).",
    )
    recovery: operation.OperationRecovery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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
    project_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="The project Core says the call was for (Solwyn-Project-Id).",
    )
    recovery: operation.OperationRecovery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    schema_name: Literal["solwyn.mcp.budget-check.v1"] = Field(
        default="solwyn.mcp.budget-check.v1",
        alias="schema",
    )


class _BudgetCheckBatchResult(_ToolResult[RecoverableBatchData]):
    project_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="The project Core says the call was for (Solwyn-Project-Id).",
    )
    recovery: operation.OperationRecovery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    schema_name: Literal["solwyn.budget.check.batch.v1"] = Field(
        default="solwyn.budget.check.batch.v1", alias="schema"
    )


class _BudgetConfirmResult(_ToolResult[BudgetConfirmData]):
    project_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="The project Core says the call was for (Solwyn-Project-Id).",
    )
    recovery: operation.OperationRecovery | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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


class _CostsResult(_ToolResult[CostsData | AccountCostQueryResponse]):
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


class _ListCostEventsResult(_ToolResult[CostEventsResponse]):
    schema_name: Literal["solwyn.costs.events.v1"] = Field(
        default="solwyn.costs.events.v1", alias="schema"
    )


class _GetCostBaselineResult(_ToolResult[BaselineResult]):
    schema_name: Literal["solwyn.costs.baseline.v1"] = Field(
        default="solwyn.costs.baseline.v1", alias="schema"
    )


class _GetUnitEconomicsResult(_ToolResult[UnitEconomicsResponse]):
    schema_name: Literal["solwyn.costs.unit-economics.v1"] = Field(
        default="solwyn.costs.unit-economics.v1", alias="schema"
    )


class _GetCostDriftResult(_ToolResult[CostDriftResponse]):
    schema_name: Literal["solwyn.costs.drift.v1"] = Field(
        default="solwyn.costs.drift.v1", alias="schema"
    )


class _ListPricedModelsResult(_ToolResult[PricedModelsResponse]):
    schema_name: Literal["solwyn.costs.priced-models.v1"] = Field(
        default="solwyn.costs.priced-models.v1", alias="schema"
    )


class _SimulateCostsResult(_ToolResult[CostSimulationResponse]):
    schema_name: Literal["solwyn.costs.simulation.v1"] = Field(
        default="solwyn.costs.simulation.v1", alias="schema"
    )


class _ListProviderFailoversResult(_ToolResult[ProviderFailoversData]):
    schema_name: Literal["solwyn.providers.failovers.v1"] = Field(
        default="solwyn.providers.failovers.v1", alias="schema"
    )


class _ListUntrackedSurfacesResult(_ToolResult[UntrackedSurfacesData]):
    schema_name: Literal["solwyn.providers.untracked.v1"] = Field(
        default="solwyn.providers.untracked.v1", alias="schema"
    )


class _ListAuditEventsResult(_ToolResult[AuditEventListResponse]):
    schema_name: Literal["solwyn.audit.v1"] = Field(
        default="solwyn.audit.v1", alias="schema"
    )


class _PlanStatusResult(_ToolResult[_PlanData]):
    schema_name: Literal["solwyn.plan.v1"] = Field(
        default="solwyn.plan.v1", alias="schema"
    )


class _PlanCatalogResult(_ToolResult[BillingCatalogResponse]):
    schema_name: Literal["solwyn.plan.catalog.v1"] = Field(
        default="solwyn.plan.catalog.v1", alias="schema"
    )


class _PlanPreviewResult(_ToolResult[PlanPreviewResponse]):
    schema_name: Literal["solwyn.plan.preview.v1"] = Field(
        default="solwyn.plan.preview.v1", alias="schema"
    )


class _ListTeamMembersResult(_ToolResult[TeamMembersData]):
    schema_name: Literal["solwyn.team.members.v1"] = Field(
        default="solwyn.team.members.v1", alias="schema"
    )


class _ListTeamInvitesResult(_ToolResult[TeamInvitesData]):
    schema_name: Literal["solwyn.team.invites.v1"] = Field(
        default="solwyn.team.invites.v1", alias="schema"
    )


class _GetWhoamiResult(_ToolResult[_WhoamiData]):
    schema_name: Literal["solwyn.whoami.v1"] = Field(
        default="solwyn.whoami.v1", alias="schema"
    )


class _GetAccountPhoneResult(_ToolResult[AccountPhoneResponse]):
    schema_name: Literal["solwyn.account.phone.v1"] = Field(
        default="solwyn.account.phone.v1", alias="schema"
    )


class _ListAlertActivityResult(_ToolResult[AlertEventListResponse]):
    schema_name: Literal["solwyn.alerts.activity.v1"] = Field(
        default="solwyn.alerts.activity.v1", alias="schema"
    )


class _ListOpenAlertDeliveriesResult(_ToolResult[AlertOpenResponse]):
    schema_name: Literal["solwyn.alerts.pending.v1"] = Field(
        default="solwyn.alerts.pending.v1", alias="schema"
    )


class _BudgetUncountedResult(_ToolResult[UncountedTallyResponse]):
    schema_name: Literal["solwyn.budget.uncounted.v1"] = Field(
        default="solwyn.budget.uncounted.v1", alias="schema"
    )


class _GetAccountAlertResult(
    _ToolResult[AccountAlertWithLatestResponse | AccountAlertResponse]
):
    schema_name: Literal["solwyn.alerts.account.v1"] = Field(
        default="solwyn.alerts.account.v1", alias="schema"
    )


@contextmanager
def tool_call_client(
    launch_options: McpLaunchOptions | None = None,
    *,
    purpose: CredentialClass = "user",
    project: str | None = None,
    needs_project: bool = False,
) -> Iterator[tuple[runtime.CommandContext, SolwynClient]]:
    """Resolve fresh runtime, credential, and client state for one MCP tool call.

    Metering tools resolve only an API key (``SOLWYN_API_KEY``, else the key
    stored for the launch project); every other tool prefers the login. An
    API key is always bound to its project; a login resolves the project only
    for project reads (``needs_project``). ``project`` is the tool's own
    project argument, which overrides the launch project.
    """
    launch = launch_options or McpLaunchOptions()
    command_runtime = runtime.resolve_command_context(
        api_url=launch.api_url,
        project=launch.project_id,
        output_format=None,
        json_flag=False,
        no_color=True,
    )
    if project is not None and project.strip(" \t"):
        selected = project.strip(" \t")
        command_runtime = command_runtime.model_copy(
            update={
                "project": selected,
                "project_selector": selected,
                # A tool argument names the project explicitly, like a flag.
                "project_source": "cli",
            }
        )
    operation.checkpoint()
    reset_api_url = auth.use_api_url(command_runtime.api_url)
    try:
        credential = auth.resolve_credential(
            purpose=purpose, api_url=command_runtime.api_url
        )
        login_only = purpose != "metering" and auth.auth_selection() == "login"
        if (
            command_runtime.project_selector is not None
            and not (credential is None and login_only)
            and (not isinstance(credential, LoginCredential) or needs_project)
        ):
            selection = project_resolver.resolve(
                runtime.candidates_of(command_runtime),
                purpose="spend" if purpose == "metering" else "read",
                api_url=command_runtime.api_url,
                credential=credential,
            )
            credential = selection.credential
            if selection.project_id is not None:
                command_runtime = command_runtime.model_copy(
                    update={"project": selection.project_id, "project_resolved": True}
                )
    except project_resolver.ProjectResolutionError as error:
        raise _local_project_error(error) from None
    except ValueError as error:
        raise AuthError(str(error)) from None
    finally:
        reset_api_url()
    operation.checkpoint()
    if credential is None:
        if purpose == "metering":
            error: AuthError = MeteringKeyRequiredError(METERING_KEY_HINT)
        else:
            error = AuthError(
                "Authentication required. Configure credentials and retry."
            )
        # Names where the lookup looked (store and path), never a secret.
        error.hint = missing_credential_hint(purpose)
        raise error
    with SolwynClient(
        api_url=command_runtime.api_url,
        credential=credential,
        **(
            {"transport": control.transport(), "sleeper": control.sleep}
            if (control := operation.current())
            else {}
        ),
    ) as client:
        yield command_runtime, client


def _client_options() -> dict[str, Any]:
    """The operation's transport and sleeper when a call controls them."""
    control = operation.current()
    return (
        {"transport": control.transport(), "sleeper": control.sleep} if control else {}
    )


class _RecordingClient:
    """Pass-through client that notes each (method, path) it sends."""

    def __init__(self, inner: Any, seen: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._seen = seen

    def __enter__(self) -> "_RecordingClient":
        self._inner.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._inner.__exit__(*args)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._seen.append((method.upper(), path))
        return self._inner.request(method, path, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _EchoingClient:
    """Pass-through client that keeps Core's Solwyn-Project-Id echo."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.project_id: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._inner.request(method, path, **kwargs)
        headers = getattr(response, "headers", None)
        echoed = headers.get(PROJECT_ID_HEADER) if headers is not None else None
        if isinstance(echoed, str) and PROJECT_ID_PATTERN.fullmatch(echoed):
            self.project_id = echoed
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# Refusals that stay true for the server's lifetime are kept and returned on
# every reporting call; a transport failure, busy service or locked store is
# retried on the next call.
_KEPT_REFUSAL_STATUSES = frozenset({400, 401, 403, 404})


class _ReportingBinding:
    """The launch project and key of the four reporting tools, resolved once.

    The project comes from the shared resolver (purpose ``spend``): --project
    before mcp, SOLWYN_PROJECT, then the repo's .solwyn.env, checked against
    each other; the key is SOLWYN_API_KEY, else the key stored for that
    project, checked against the login's account. The first reporting call
    validates the pair with GET /api/v1/auth/key on its own client. A refusal
    is kept and returned on every call; no other key is ever tried.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcome: tuple[str, ResolvedCredential, bool] | Exception | None = None
        self._inputs: _LaunchInputs | None = None

    def freeze(self, launch: McpLaunchOptions) -> None:
        """Capture the launch inputs once, without any network.

        The resolved command context (--project, SOLWYN_PROJECT, the API URL
        and the repo's .solwyn.env as it reads now, from the launch cwd) and
        the environment the credential policy reads (SOLWYN_API_KEY,
        SOLWYN_TOKEN, SOLWYN_AUTH). Every bind attempt, retries after a
        transient failure included, resolves from exactly these inputs, so a
        later edit of the repo file, the environment or the cwd can never
        change the project or key the reporting tools use.
        """
        with self._lock:
            if self._inputs is not None:
                return
            environ = dict(os.environ)
            try:
                context: runtime.CommandContext | Exception = (
                    runtime.resolve_command_context(
                        api_url=launch.api_url,
                        project=launch.project_id,
                        output_format=None,
                        json_flag=False,
                        no_color=True,
                        environ=environ,
                        cwd=Path.cwd(),
                    )
                )
            except Exception as error:  # noqa: BLE001 - kept, raised per call.
                context = error
            self._inputs = _LaunchInputs(context=context, environ=environ)

    def _keep(self, error: Exception) -> None:
        if isinstance(error, _LocalProjectError) or (
            isinstance(error, ApiError) and error.status_code in _KEPT_REFUSAL_STATUSES
        ):
            self._outcome = error

    def get(self, launch: McpLaunchOptions) -> tuple[str, ResolvedCredential, bool]:
        self.freeze(launch)
        with self._lock:
            if isinstance(self._outcome, Exception):
                raise self._outcome
            if self._outcome is not None:
                return self._outcome
            assert self._inputs is not None
            try:
                self._outcome = _bind_reporting(self._inputs)
            except (_LocalProjectError, ApiError) as error:
                self._keep(error)
                raise
            return self._outcome

    def validate(self, client: Any, credential: ApiKeyCredential) -> ApiKeyCredential:
        """GET /api/v1/auth/key once for the launch pair; keep the answer."""
        with self._lock:
            if isinstance(self._outcome, Exception):
                raise self._outcome
            if self._outcome is not None and self._outcome[2]:
                return self._outcome[1]  # type: ignore[return-value]
            try:
                response = client.request("GET", "/api/v1/auth/key")
                try:
                    info = project_resolver.AuthKeyResponse.model_validate(
                        response.json()
                    )
                except (ValidationError, ValueError, TypeError):
                    raise _LocalProjectError("invalid_auth_key_response") from None
                if info.project_id != credential.project:
                    raise _LocalProjectError("invalid_auth_key_response")
            except (_LocalProjectError, ApiError) as error:
                self._keep(error)
                raise
            validated = credential.model_copy(
                update={
                    "key_id": info.key_id,
                    "account_id": info.account_id,
                    "project_name": info.project_name,
                }
            )
            assert self._outcome is not None and not isinstance(
                self._outcome, Exception
            )
            self._outcome = (self._outcome[0], validated, True)
            return validated


@dataclass(frozen=True)
class _LaunchInputs:
    """What the reporting tools' project and key come from, read at launch."""

    context: runtime.CommandContext | Exception
    environ: Mapping[str, str]


def _bind_reporting(
    inputs: _LaunchInputs,
) -> tuple[str, ResolvedCredential, bool]:
    """(API URL, credential, whether /auth/key already answered for it).

    Reads only the frozen launch inputs (and the credential store), never the
    live environment, cwd or repo file.
    """
    if isinstance(inputs.context, Exception):
        raise inputs.context
    command_runtime = inputs.context
    if command_runtime.project_selector is None and command_runtime.project:
        command_runtime = command_runtime.model_copy(
            update={"project_selector": command_runtime.project}
        )
    api_url = command_runtime.api_url
    seen: list[tuple[str, str]] = []

    def factory(**kwargs: Any) -> _RecordingClient:
        return _RecordingClient(SolwynClient(**kwargs, **_client_options()), seen)

    reset_api_url = auth.use_api_url(api_url)
    try:
        # SOLWYN_API_KEY, or None: without a project id no stored key is chosen
        # here; the resolver picks the launch project's stored key below.
        environment_key = auth.resolve_credential(
            environ=inputs.environ, purpose="metering", api_url=api_url
        )
        if isinstance(environment_key, LoginCredential):
            # Never sent: the tool refuses a login (project_key_required).
            return api_url, environment_key, True
        try:
            selection = project_resolver.resolve(
                runtime.candidates_of(command_runtime),
                purpose="spend",
                api_url=api_url,
                credential=environment_key,
                environ=inputs.environ,
                factory=factory,
            )
        except project_resolver.ProjectResolutionError as error:
            raise _local_project_error(error) from None
        credential = selection.credential
        if credential is None:
            missing = MeteringKeyRequiredError(METERING_KEY_HINT)
            missing.hint = missing_credential_hint("metering")
            raise missing
        if not isinstance(credential, ApiKeyCredential):
            raise MeteringKeyRequiredError(METERING_KEY_HINT)
        if credential.project is None:
            raise _local_project_error(project_resolver.project_required())
        return api_url, credential, ("GET", "/api/v1/auth/key") in seen
    finally:
        reset_api_url()


@contextmanager
def reporting_client(launch_options: McpLaunchOptions) -> Iterator[_EchoingClient]:
    """A client for budget_check, budget_check_batch, budget_confirm and
    record_call: the launch project's key, Solwyn-Project on every request."""
    operation.checkpoint()
    binding = launch_options._reporting
    api_url, credential, validated = binding.get(launch_options)
    operation.checkpoint()
    with SolwynClient(
        api_url=api_url, credential=credential, **_client_options()
    ) as client:
        if isinstance(credential, ApiKeyCredential) and not validated:
            binding.validate(client, credential)
            operation.checkpoint()
        yield _EchoingClient(client)


def _echoed(result: ResultT, client: _EchoingClient) -> ResultT:
    """Add the project Core says the call was for (Solwyn-Project-Id)."""
    if client.project_id is None:
        return result
    return result.model_copy(update={"project_id": client.project_id})


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
COSTS_DESCRIPTION = "Read project costs grouped by a Core dimension or tag:<key>, or account costs grouped by project by default. Shared filters/custom windows apply; account mode permits only model/provider/modality and window filters."
FORECAST_DESCRIPTION = (
    "Read the configured project's month-to-date spend forecast without "
    "authorizing spend."
)
PROJECTS_DESCRIPTION = (
    "List projects visible to the logged-in user. Requires a Solwyn login."
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
        "Reserve budget only immediately before the caller-made spend. Denial is a successful decision with allowed=false, not an execution error. Denials record attribution by default; no_record skips ingest. Never automatically replay an ambiguous check: each invocation can create a new reservation. "
        f"{shared_sharp_edges}",
        "Settle a non-null reservation after caller-made spend using observed token/media usage and actual served provider/model. Records attribution with the same call_id and attempt_index; no_record skips ingest. Recording failure does not undo settlement. Save the FIRST recording_retry event. After known settlement success, use only record_only=FIRST event to retry ingest; this reports settlement=not_submitted. After a lost 204, within Core's project-bound 3600-second receipt window replay identical settlement inputs with no_record=true. After a known 204, submit only record_only=FIRST event; never replace it with the replay's unused new event. Ordinary fresh-confirm replay regenerates timestamp/SDK identity and rereads ambient tags. An event never returned cannot be reconstructed. The receipt cannot correct successful settlement. Missing-media 422 permits corrected quantities under the retained funding ID; never reserve again for completed provider work. "
        f"{shared_sharp_edges}",
    )


def _project_id(
    command_runtime: runtime.CommandContext,
    requested_project_id: str | None,
) -> str:
    # tool_call_client resolved the requested (or launch) project to its id.
    project_id = (
        command_runtime.project
        if command_runtime.project_resolved
        else requested_project_id or command_runtime.project
    )
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
    if recorded is not None and recorded.status in {
        "rejected",
        "failed",
        "unknown",
        "stopped",
        "not_recorded",
    }:
        settled = (
            isinstance(result.data, BudgetConfirmData)
            and result.data.settlement == "confirmed"
        )
        return result.model_copy(
            update={
                "ok": False,
                "error": _ToolError(
                    code="service_busy"
                    if recorded.status == "not_recorded"
                    else "recording_rejected"
                    if recorded.status == "rejected"
                    else "recording_unknown"
                    if recorded.status == "unknown"
                    else "recording_stopped"
                    if recorded.status == "stopped"
                    else "recording_failed",
                    message=(
                        "Reservation settlement succeeded, but attribution recording did not complete."
                        if settled
                        else "Attribution recording did not complete."
                    ),
                    hint=(
                        (INGEST_BUSY_HINT + " ")
                        if recorded.status == "not_recorded"
                        else ""
                    )
                    + (
                        "After known settlement success, use budget_confirm record_only with the FIRST retained recording_retry; never replace it with a settlement-only replay's unused new event. Keep the earlier settlement state separately and do not reserve again."
                        if isinstance(result.data, BudgetConfirmData)
                        else "Preserve the call identity and retry attribution with the same call_id and attempt; do not reserve again."
                    ),
                ),
            }
        )
    return result


def _login_required_hint() -> str:
    # A project key resolved; say where the login was looked for.
    lookup = auth.last_lookup()
    if lookup is None:
        return LOGIN_REQUIRED_HINT
    return f"{LOGIN_REQUIRED_HINT} Looked in: {lookup.describe()}."


def _exit_if_stranded() -> None:
    """A timed-out keyring write may still land: leave instead of holding.

    The process keeps the credential lock until it exits, so a server that
    stayed up would block every other Solwyn process. Exit shortly after this
    result is written; the host starts a fresh server on the next call.
    """
    if credential_store.stranded():
        timer = threading.Timer(1.0, os._exit, args=(1,))
        timer.daemon = True
        timer.start()


def _protocol_result(result: _ToolResult) -> types.CallToolResult:
    # Every tool result passes here; both representations share one redaction.
    payload = redact_secrets(result.model_dump(mode="json", by_alias=True))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=not result.ok,
    )


def _bounded_protocol_result(
    result: _ToolResult, control: operation.OperationControl | None
) -> types.CallToolResult:
    snapshot = control.snapshot() if control is not None else None
    if snapshot is not None:
        recovery = snapshot.recovery
        if not isinstance(
            result,
            (
                _RecordResult,
                _BudgetCheckResult,
                _BudgetCheckBatchResult,
                _BudgetConfirmResult,
            ),
        ):
            recovery = operation.ReadRecovery(cursor=recovery.cursor)
        if any(
            value is not None and value != []
            for value in recovery.model_dump().values()
        ):
            result = result.model_copy(update={"recovery": recovery})
        elif isinstance(result.recovery, operation.OperationRecovery):
            result = result.model_copy(update={"recovery": None})
        if not result.ok and result.data is None and snapshot.data is not None:
            result = type(result).model_validate(
                {**result.model_dump(), "data": snapshot.data}
            )
    wire = _protocol_result(result)
    if (
        len(wire.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
        + operation.RESULT_METADATA_ALLOWANCE
        <= operation.MAX_RESULT_BYTES
    ):
        return wire
    compact = result.model_copy(
        update={
            "ok": False,
            "data": None,
            "error": _ToolError(
                code="result_size_limit",
                message="The result exceeded the fixed MCP response limit.",
                hint="Preserve the compact known identities and cursor; do not replay an ambiguous mutation.",
            ),
        }
    )
    if snapshot is not None and isinstance(compact.recovery, operation.ReadRecovery):
        compact = compact.model_copy(
            update={"recovery": operation.ReadRecovery(cursor=snapshot.initial_cursor)}
        )
    return _protocol_result(compact)


class _LocalProjectError(ValueError):
    """A project resolution refusal from the CLI's own resolver.

    The resolver's text can name projects and accounts; an agent sees only a
    fixed message and hint per code.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_LAUNCH_PROJECT_HINT = (
    "Restart the server with --project before mcp, set SOLWYN_PROJECT, or set it "
    "in the repo's .solwyn.env."
)
_LOCAL_PROJECT_ERRORS: dict[str, tuple[str, str | None]] = {
    "project_required": ("A Solwyn project is required.", _LAUNCH_PROJECT_HINT),
    "usage_error": (
        "SOLWYN_PROJECT_ID was replaced by SOLWYN_PROJECT (a project name or proj_ "
        "id).",
        "Set SOLWYN_PROJECT, or pass --project before mcp, instead.",
    ),
    "project_mismatch": (
        "The project selected for this server does not match the repo's .solwyn.env.",
        "Edit .solwyn.env if the repo should use another project, or restart the "
        "server with the project it names. Never switch projects to get around a "
        "budget limit.",
    ),
    "repo_file_invalid": (
        "The repo's .solwyn.env is not valid.",
        'It must hold one line, SOLWYN_PROJECT="<project name or proj_ id>", plus '
        "comments.",
    ),
    "project_ambiguous": (
        "More than one stored API key is for a project with this name (different "
        "accounts).",
        "Use the proj_ id, or sign in with solwyn login.",
    ),
    "api_key_not_stored": (
        "No API key for the launch project is stored on this computer.",
        "Run solwyn keys create --project PROJECT --store-key, or set SOLWYN_API_KEY.",
    ),
    "api_key_account_mismatch": (
        "The API key for the launch project belongs to another account than the "
        "current login.",
        "Run solwyn keys create --project PROJECT --store-key.",
    ),
    "account_unresolved": (
        "Could not confirm which account the Solwyn login belongs to.",
        "Check the connection, then restart the server.",
    ),
    "project_not_found": (
        "The project was not found.",
        "Check the project name with solwyn projects list, or use the proj_ id.",
    ),
    "invalid_projects_response": ("The projects response was invalid.", None),
    "invalid_auth_key_response": ("The API key response was invalid.", None),
}


def _local_project_error(error: project_resolver.ProjectResolutionError) -> Exception:
    """The resolver's refusal as the MCP layer reports it."""
    if error.code == "authentication_required":
        return LoginRequiredError(error.message)
    return _LocalProjectError(
        error.code if error.code in _LOCAL_PROJECT_ERRORS else "project_not_found"
    )


def _operation_error(
    result_type: type[ResultT],
    error: Exception,
    *,
    invalid_response_code: str,
    login_required: bool = False,
    invalid_response_hint: str | None = None,
) -> ResultT:
    if isinstance(error, _LocalProjectError):
        message, hint = _LOCAL_PROJECT_ERRORS[error.code]
        return _error(result_type, code=error.code, message=message, hint=hint)
    if isinstance(error, AccountProjectConflictError):
        return _error(
            result_type,
            code="invalid_cost_query",
            message="The cost query was invalid.",
            hint="Omit project for account=true, or set account=false for project costs.",
        )
    if isinstance(error, UnsupportedAccountCostFilterError):
        return _error(
            result_type,
            code="invalid_cost_query",
            message="The cost query was invalid.",
            hint="Account costs support only model, provider, modality, group_by and range or paired from_/to bounds; remove project-only filters.",
        )
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
                _remote_error_message(error, code)
                if error.status_code is not None
                else "Could not connect to the Solwyn API."
            ),
            hint=_safe_remote_error_hint(error, code),
            status_code=error.status_code,
        )
    if isinstance(error, CredentialStorageError):
        # CLI-owned text naming the store and path; never server free text.
        return _error(
            result_type,
            code="credential_storage_failed",
            message=str(error),
            hint=error.hint,
        )
    if isinstance(error, AuthError):
        return _error(
            result_type,
            code="authentication_required",
            message="Solwyn authentication is required.",
            hint=(
                INGEST_UNKNOWN_HINT
                if error.ingest_outcome_uncertain
                else (error.hint or METERING_KEY_HINT)
                if isinstance(error, MeteringKeyRequiredError)
                else _login_required_hint()
                if login_required or isinstance(error, LoginRequiredError)
                else error.hint
                or "Configure a project key or run solwyn login, then retry."
            ),
        )
    if isinstance(error, ValueError) and str(error) == "project_required":
        return _error(
            result_type,
            code="project_required",
            message="A Solwyn project is required.",
            hint=(
                "Pass --project before mcp, set SOLWYN_PROJECT, or set it in the "
                "repo's .solwyn.env."
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
        hint=invalid_response_hint,
    )


def _bounded_count(value: object) -> int | None:
    if type(value) is int and 0 <= value <= _MAX_REMOTE_COUNT:
        return value
    return None


def _remote_error_message(error: ApiError, code: str) -> str:
    """Build a fixed message from validated detail fields, never server text."""
    message = _REMOTE_ERROR_MESSAGES.get(code, _REMOTE_REJECTED_MESSAGE)
    detail = error.detail if isinstance(error.detail, dict) else {}
    if code == "quota_exceeded":
        included = _bounded_count(detail.get("included_requests"))
        current = _bounded_count(detail.get("current_requests"))
        if included is not None and current is not None:
            message = message.replace("reached.", f"reached ({current}/{included}).", 1)
    elif code == "upgrade_required":
        tier = detail.get("required_tier")
        if isinstance(tier, str) and tier in _TIER_NAMES:
            message = f"This feature requires the {_TIER_NAMES[tier]} tier or above."
    elif code == "project_limit_reached":
        limit = _bounded_count(detail.get("limit"))
        if limit is not None:
            message = f"Project limit reached for the current plan ({limit} projects)."
    elif code == "approval_invalid":
        reason = detail.get("reason")
        if isinstance(reason, str) and reason in APPROVAL_INVALID_MESSAGES:
            message = APPROVAL_INVALID_MESSAGES[reason]
    return message


def _normalized_remote_error_code(error: ApiError) -> str:
    """Return only fixed semantic codes; never reflect arbitrary server values."""
    if error.code == "approval_limit_reached":
        return error.code
    if error.status_code == 429:
        return "rate_limited"
    if error.code == "account_membership_revoked":
        return "account_membership_revoked"
    if error.code in _SAFE_REMOTE_ERROR_CODES:
        return error.code
    if error.status_code == 503:
        return "service_busy"
    if error.status_code == 401:
        return "authentication_failed"
    if error.status_code == 403:
        return "forbidden"
    return "api_error"


def _safe_remote_error_hint(error: ApiError, code: str) -> str | None:
    """Keep only local allowlisted CTA paths from shared sanitized hints."""
    if error.ingest_outcome_uncertain and error.hint == INGEST_UNKNOWN_HINT:
        return INGEST_UNKNOWN_HINT
    if code == "service_busy" and error.hint in {
        BUDGET_BUSY_HINT,
        INGEST_BUSY_HINT,
        INGEST_UNKNOWN_HINT,
        LEGACY_BATCH_BUSY_HINT,
        WRITE_BUSY_HINT,
    }:
        return error.hint
    if code == "approval_required":
        # MCP tools never wait: hand the validated approval URL to the human.
        approval_url = approval_url_from(error.detail)
        return (
            f"A human must approve this change in the browser: {approval_url}"
            if approval_url is not None
            else "A human must approve this change in the browser."
        )
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
    credential: LoginCredential | ApiKeyCredential,
    operation: str,
) -> ResultT | None:
    if isinstance(credential, ApiKeyCredential):
        return None
    return _error(
        result_type,
        code="project_key_required",
        message=(
            "Budget checks require a project API key."
            if operation == "checks"
            else "Budget confirmation requires a project API key."
        ),
        hint=(
            "Set SOLWYN_API_KEY and SOLWYN_PROJECT, or run solwyn keys create "
            "--project PROJECT --store-key."
        ),
    )


def _alert_events_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
    *,
    account: bool = False,
    **filters: Any,
) -> _AlertEventsResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=not account
        ) as (command_runtime, client):
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
            _AlertEventsResult,
            error,
            invalid_response_code="invalid_alerts_response",
            login_required=account,
        )


def _budget_status_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
) -> _BudgetStatusResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
    project_id: str | None,
    launch_options: McpLaunchOptions,
    with_last_fired: bool = False,
) -> _BudgetRulesResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
            data = request_budget_rules(
                client=client,
                project_id=_project_id(command_runtime, project_id),
                with_last_fired=with_last_fired,
            )
        return _success(_BudgetRulesResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetRulesResult,
            error,
            invalid_response_code="invalid_budget_rules_response",
            login_required=with_last_fired,
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
        with reporting_client(launch_options) as client:
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
        return _echoed(_success(_BudgetCheckResult, data), client)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetCheckResult,
            error,
            invalid_response_code="invalid_budget_check_response",
        )


def _budget_check_batch_operation(
    *,
    launch_options: McpLaunchOptions,
    checks: list[dict[str, Any]] | None,
    operation_id: str | None,
    recover_only: bool,
    record_only: list[DenialRecordingRetry] | None,
    no_record: bool,
    estimated_output_tokens: int | None,
) -> _BudgetCheckBatchResult:
    try:
        if recover_only or record_only is not None:
            if (
                operation_id is None
                or checks is not None
                or (recover_only and record_only is not None)
            ):
                raise ValueError(
                    "Recovery and recording require only their retained identity"
                )
            if record_only is not None and (
                no_record or estimated_output_tokens is not None
            ):
                raise ValueError(
                    "Recording retry requires its unchanged retained payload"
                )
            prepared = None
        else:
            prepared = prepare_batch_checks(checks)
            if len(prepared) > 32:
                raise ValueError("At most 32 checks")
        control = operation.current()
        identifier = (
            operation_id
            or (control.recovery().operation_id if control else None)
            or new_operation_id()
        )
        operation_expiry(identifier)
        if record_only is not None:
            if len({receipt.index for receipt in record_only}) != len(record_only):
                raise ValueError("Recording indices must be unique")
            for receipt in record_only:
                receipt.validate_identity(identifier)
        operation.remember(operation_id=identifier, state="not_submitted")
    except (ValidationError, ValueError, TypeError):
        return _error(
            _BudgetCheckBatchResult,
            code="invalid_budget_check_batch_request",
            message="Provide 1–32 checks, or only the retained operation_id with recover_only or record_only.",
        )
    try:
        with reporting_client(launch_options) as client:
            auth_error = _require_project_key(
                _BudgetCheckBatchResult, client.current_credential, "checks"
            )
            if auth_error is not None:
                return auth_error
            data = request_recoverable_budget_batch(
                client=client,
                checks=prepared,
                operation_id=identifier,
                recover_only=recover_only,
                record_only=record_only,
                no_record=no_record,
                estimated_output_tokens=estimated_output_tokens,
            )
        if data.mode == "record_only" and any(
            item.status in {"failed", "rejected", "unknown", "stopped", "not_recorded"}
            for item in data.recordings
        ):
            return _error(
                _BudgetCheckBatchResult,
                code="service_busy"
                if all(item.status == "not_recorded" for item in data.recordings)
                else "recording_unknown"
                if any(item.status == "unknown" for item in data.recordings)
                else "recording_failed",
                message="Attribution retry did not complete; admission was not retried.",
                hint=INGEST_BUSY_HINT
                if all(item.status == "not_recorded" for item in data.recordings)
                else None,
            ).model_copy(update={"data": data, "project_id": client.project_id})
        failed = any(item.state != "completed" for item in data.items)
        if failed:
            failure = _error(
                _BudgetCheckBatchResult,
                code="batch_check_failed",
                message="The batch contains incomplete admission outcomes.",
                hint="Recover with the same operation_id. Never replay unknown or unattempted items automatically.",
            )
            return _echoed(failure.model_copy(update={"data": data}), client)
        return _echoed(_success(_BudgetCheckBatchResult, data), client)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _BudgetCheckBatchResult,
            error,
            invalid_response_code="invalid_budget_check_batch_response",
        )


def _budget_confirm_operation(
    *,
    launch_options: McpLaunchOptions,
    reservation_id: str | None,
    call_id: str | None,
    model: str | None,
    provider: ProviderName | None,
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
    media_usage: MediaUsage | None = None,
    modality: Modality = Modality.TEXT,
    attempt_index: int = 0,
    record_only: ConfirmationRecordingRetry | None = None,
    latency_ms: float = 0,
    status: str = "success",
    agent_run_id: str | None = None,
    parent_run_id: str | None = None,
    run_name: str | None = None,
    tags: dict[str, str] | None = None,
    no_record: bool = False,
) -> _BudgetConfirmResult:
    try:
        if record_only is not None:
            event = prepare_confirm_recording_retry(record_only)
            request = None
        else:
            request = BudgetConfirmRequest(
                reservation_id=reservation_id,
                call_id=call_id,
                model=model,
                provider=provider,
                is_provider_fallback=is_provider_fallback,
                provider_region=provider_region,
                service_tier=service_tier,
                media_usage=media_usage,
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
                modality=modality,
                attempt_index=attempt_index,
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
        with reporting_client(launch_options) as client:
            auth_error = _require_project_key(
                _BudgetConfirmResult, client.current_credential, "confirmation"
            )
            if auth_error is not None:
                return auth_error
            data = (
                record_confirm_only(client=client, event=event)
                if request is None
                else confirm_and_record(
                    client=client, request=request, event=event, no_record=no_record
                )
            )
        return _echoed(_success(_BudgetConfirmResult, data), client)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        failure = _operation_error(
            _BudgetConfirmResult,
            error,
            invalid_response_code="invalid_budget_confirm_response",
        )
        if (
            request is not None
            and isinstance(error, ApiError)
            and error.code == "transport_error"
            and failure.error is not None
        ):
            return failure.model_copy(
                update={
                    "error": failure.error.model_copy(
                        update={
                            "hint": "Settlement may have applied. Save the FIRST recording_retry if returned. Within Core's 3600-second receipt window, replay identical settlement inputs with no_record=true; after a known 204 use record_only with the FIRST event, never the replay's unused new event. An unseen event cannot be reconstructed; do not reserve again."
                        }
                    )
                }
            )
        return failure


def _project_status_operation(
    project_id: str | None,
    launch_options: McpLaunchOptions,
) -> _ProjectStatusResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
    group_by: str | None,
    account: bool = False,
    **filters: Any,
) -> _CostsResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=not account
        ) as (command_runtime, client):
            if account:
                if project_id is not None:
                    raise AccountProjectConflictError(
                        "Account and project_id are mutually exclusive."
                    )
                data = fetch_account_costs(
                    client=client,
                    time_range=time_range,
                    group_by=group_by or "project",
                    **filters,
                )
            else:
                data = fetch_costs(
                    client=client,
                    project_id=_project_id(command_runtime, project_id),
                    time_range=time_range,
                    group_by=group_by or "model",
                    **filters,
                )
        return _success(_CostsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _CostsResult,
            error,
            invalid_response_code="invalid_costs_response",
            login_required=account,
        )


def _cost_total_operation(
    *,
    launch_options: McpLaunchOptions,
    project_id: str | None,
    time_range: str = "7d",
    **filters: Any,
) -> _CostTotalResult:
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
            if isinstance(client.current_credential, ApiKeyCredential):
                return _error(
                    _ProjectsResult,
                    code="authentication_required",
                    message="User login is required to list projects.",
                    hint=_login_required_hint(),
                )
            data = ProjectsListData(projects=fetch_projects(client=client))
        return _success(_ProjectsResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _ProjectsResult,
            error,
            invalid_response_code="invalid_projects_response",
            login_required=True,
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
        with reporting_client(launch_options) as client:
            auth_error = _require_project_key(
                _RecordResult, client.current_credential, "recording"
            )
            if auth_error is not None:
                return auth_error
            data = request_ingest(client=client, events=[event])
            operation.retain(data, call_id=event.call_id, recording=data.status)
        return _echoed(_success(_RecordResult, data), client)
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
            operation.page(cursor)
    except ValueError:
        return _error(
            result_class, code="invalid_tag_query", message="Invalid tag key or prefix."
        )
    try:
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
        with tool_call_client(
            launch_options, project=project_id, needs_project=True
        ) as (command_runtime, client):
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
            if isinstance(client.current_credential, ApiKeyCredential):
                return _error(
                    _OverviewResult,
                    code="authentication_required",
                    message="User login is required to read the account overview.",
                    hint=_login_required_hint(),
                )
            data = fetch_overview(client=client)
        return _success(_OverviewResult, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        return _operation_error(
            _OverviewResult,
            error,
            invalid_response_code="invalid_overview_response",
            login_required=True,
        )


def fetch_simulation_options(
    *, client, project_id, provider, model, region, service_tier, **window
):
    request = simulation_request(
        provider=provider, model=model, region=region, service_tier=service_tier
    )
    return fetch_simulation(
        client=client, project_id=project_id, request=request, **window
    )


def fetch_drift_options(*, client, project_id, **window):
    return fetch_project_read(
        client=client,
        project_id=project_id,
        path="costs/drift",
        response_model=CostDriftResponse,
        params=window_params(**window),
    )


def _read_operation(
    result_type,
    reader,
    launch,
    *,
    project_id=None,
    project_read=False,
    identity=False,
    login_required=False,
    transport_read_hint=None,
    **options,
):
    try:
        with tool_call_client(
            launch, project=project_id, needs_project=project_read
        ) as (command_runtime, client):
            if project_read:
                options["project_id"] = _project_id(command_runtime, project_id)
            if identity:
                options["project"] = command_runtime.project
            data = reader(client=client, **options)
        return _success(result_type, data)
    except (ApiError, AuthError, ValidationError, ValueError, TypeError) as error:
        if (
            transport_read_hint
            and isinstance(error, ApiError)
            and error.code == "transport_error"
        ):
            return _error(
                result_type,
                code="api_error",
                message="Could not connect to the Solwyn API.",
                hint=transport_read_hint,
            )
        return _operation_error(
            result_type,
            error,
            invalid_response_code="invalid_read_response",
            login_required=login_required,
            invalid_response_hint=READ_HINT,
        )


def _create_catalog(launch: McpLaunchOptions) -> _ToolCatalog:
    """Construct declarations without resolving configuration or credentials."""
    catalog = _ToolCatalog(launch.profile)
    status_description, check_description, confirm_description = (
        _budget_tool_descriptions()
    )

    @catalog.tool(
        name="budget_status",
        title="Read budget status",
        description=status_description,
    )
    async def budget_status(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _BudgetStatusResult:
        return await operation.run_sync(
            partial(_budget_status_operation, project, launch)
        )

    @catalog.tool(
        name="budget_check",
        title="Check and reserve budget",
        read_only=False,
        credential_requirement="project_key",
        description=check_description,
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
        return await operation.run_sync(
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

    @catalog.tool(
        name="budget_confirm",
        title="Confirm usage and record attribution",
        read_only=False,
        credential_requirement="project_key",
        description=confirm_description,
    )
    async def budget_confirm(
        reservation_id: Annotated[str | None, Field(min_length=1)] = None,
        call_id: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=36,
                pattern=CALL_ID_PATTERN,
                description=CALL_ID_HINT,
            ),
        ] = None,
        model: Annotated[str | None, Field(min_length=1, max_length=2048)] = None,
        provider: ProviderName | None = None,
        is_provider_fallback: bool = False,
        provider_region: Annotated[
            str | None, Field(min_length=1, max_length=32)
        ] = None,
        service_tier: ServiceTier | None = None,
        media_usage: MediaUsage | None = None,
        modality: Modality = Modality.TEXT,
        attempt_index: Annotated[int, Field(ge=0)] = 0,
        record_only: ConfirmationRecordingRetry | None = None,
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
        return await operation.run_sync(
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
                media_usage=media_usage,
                modality=modality,
                attempt_index=attempt_index,
                record_only=record_only,
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

    @catalog.tool(
        name="get_project_status",
        title="Read project health",
        description=PROJECT_STATUS_DESCRIPTION,
    )
    async def get_project_status(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _ProjectStatusResult:
        return await operation.run_sync(
            partial(_project_status_operation, project, launch)
        )

    @catalog.tool(
        name="get_forecast",
        title="Read spend forecast",
        description=FORECAST_DESCRIPTION,
    )
    async def get_forecast(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _ForecastResult:
        return await operation.run_sync(partial(_forecast_operation, project, launch))

    @catalog.tool(
        name="get_costs",
        title="Read grouped costs",
        description=COSTS_DESCRIPTION,
        credential_note="Project mode accepts a project key or a Solwyn login; account=true requires a Solwyn login and is mutually exclusive with an explicit project. Account filters are restricted by Core.",
    )
    async def get_costs(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: CostRange = CostRange.SEVEN_DAYS,
        group_by: str | None = None,
        account: bool = False,
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
        return await operation.run_sync(
            partial(
                _costs_operation,
                launch_options=launch,
                project_id=project,
                time_range=time_range,
                group_by=group_by,
                account=account,
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

    @catalog.tool(
        name="list_projects",
        title="List account projects",
        credential_requirement="login",
        description=PROJECTS_DESCRIPTION,
    )
    async def list_projects() -> _ProjectsResult:
        return await operation.run_sync(partial(_projects_operation, launch))

    @catalog.tool(
        name="get_cost_total",
        title="Read cost total",
        description="Read uncapped project cost totals with the same cost filters and custom windows as get_costs.",
    )
    async def get_cost_total(
        project: Annotated[str | None, Field(min_length=1)] = None,
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
        return await operation.run_sync(
            partial(
                _cost_total_operation,
                launch_options=launch,
                project_id=project,
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

    @catalog.tool(
        name="list_budget_rules",
        title="List budget rules",
        credential_note="Project keys or a Solwyn login can list rules; with_last_fired=true requires a Solwyn login for the account latest-events read.",
        description="Read budget rule conditions, routing, and separate paused/snoozed states. A snoozed hard_deny still denies; pausing releases enforcement. Rule keys change when keyed fields are edited.",
    )
    async def list_budget_rules(
        project: Annotated[str | None, Field(min_length=1)] = None,
        with_last_fired: bool = False,
    ) -> _BudgetRulesResult:
        return await operation.run_sync(
            partial(_budget_rules_operation, project, launch, with_last_fired)
        )

    @catalog.tool(
        name="record_call",
        title="Record completed call attribution",
        read_only=False,
        credential_requirement="project_key",
        description="Record one caller-owned completed call without reserving or settling budget, including a record-only retry after known settlement success. Do not duplicate SDK events. Persist call_id before sending and reuse it with unchanged payload and attempt_index=0 on retries; omission generates a new ID per call. Core deduplicates the project/call_id/attempt_index identity while retained; this is not an update mechanism. Costs are server-priced. Inspect status, rejected and duplicates even when transport succeeds.",
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
        return await operation.run_sync(
            partial(_record_operation, launch_options=launch, inputs=inputs)
        )

    @catalog.tool(
        name="list_tags",
        title="List attribution tag keys",
        description="List account-scoped tag discovery through a project. Team required; keys preserve whitespace and case.",
    )
    async def list_tags(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _TagsResult:
        return await operation.run_sync(
            partial(_tags_operation, launch_options=launch, project_id=project)
        )

    @catalog.tool(
        name="list_tag_values",
        title="List attribution tag values",
        description="Read one page of account-scoped values for a verbatim tag key. Team required; prefix is case-sensitive.",
    )
    async def list_tag_values(
        key: Annotated[str, Field(min_length=1, max_length=64)],
        project: Annotated[str | None, Field(min_length=1)] = None,
        prefix: Annotated[str | None, Field(max_length=256)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> _TagValuesResult:
        return await operation.run_sync(
            partial(
                _tags_operation,
                launch_options=launch,
                project_id=project,
                key=key,
                prefix=prefix,
                limit=limit,
                cursor=cursor,
            )
        )

    @catalog.tool(
        name="list_agent_runs",
        title="List agent runs",
        description=AGENT_RUNS_DESCRIPTION,
    )
    async def list_agent_runs(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: RunRange = RunRange.SEVEN_DAYS,
        from_time: str | None = None,
        to_time: str | None = None,
        query: Annotated[str | None, Field(max_length=255)] = None,
        parent: Annotated[str | None, Field(min_length=1, max_length=256)] = None,
        sort: RunSort = RunSort.LAST_SEEN_AT,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> _AgentRunsResult:
        return await operation.run_sync(
            partial(
                _agent_runs_operation,
                launch_options=launch,
                project_id=project,
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

    @catalog.tool(
        name="get_agent_run",
        title="Read agent run",
        description=AGENT_RUN_DESCRIPTION,
    )
    async def get_agent_run_tool(
        run: Annotated[str, Field(min_length=1, max_length=255)],
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _AgentRunResult:
        return await operation.run_sync(
            partial(
                _agent_run_operation,
                launch_options=launch,
                run=run,
                project_id=project,
            )
        )

    @catalog.tool(
        name="list_alert_events",
        title="Read alert history",
        credential_note="Project history accepts a project key or a Solwyn login; account=true reads account-scope history, requires a Solwyn login and ignores project. Use list_alert_activity for all projects plus account scope.",
        description="Read cursor-paginated alert history and per-channel delivery receipts. Project keys work for project history; account history requires a Solwyn login. failed_only filters partial/failed rollups locally; scope is the rule scope. No mutation tools are exposed.",
    )
    async def list_alert_events(
        project: Annotated[str | None, Field(min_length=1)] = None,
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
        return await operation.run_sync(
            partial(
                _alert_events_operation,
                project,
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

    @catalog.tool(
        name="budget_check_batch",
        title="Check or recover a bounded batch",
        read_only=False,
        credential_requirement="project_key",
        credential_note="Requires a project API key. Submission and record_only require write scope; recover_only GET accepts read-only scope. Uses the key's project, not the selected read project.",
        description=(
            "Submit 1–32 checks once to Core's durable batch journal. Retain a caller-prepared UUIDv7 operation_id before calling so an entirely lost MCP reply is recoverable. Omission generates an ID returned when a reply can be delivered. recover_only with that ID performs GET only: no admission, recording, resumption or renewed reservation authority. Indexed completed, failed, unknown and unattempted outcomes remain distinct; reservations expire at their original checked_at + 300 seconds and journal IDs expire after 24 hours. Never replay an unknown outcome or unattempted suffix automatically, or replace an ID to evade conflicts/expiry. Denials are successful decisions and record attribution unless no_record; retain each recording_retry payload for explicit record_only retry, which only ingests that identical event. Settlement remains separate. All calls share a fixed 30-second work deadline, 1 MiB HTTP response budget and 2 MiB result limit. An admitted request may remain effective after cancellation."
        ),
    )
    async def budget_check_batch(
        checks: Annotated[
            list[BudgetCheckRequest] | None, Field(min_length=1, max_length=32)
        ] = None,
        no_record: bool = False,
        estimated_output_tokens: Annotated[
            int | None, Field(ge=0, le=100_000_000)
        ] = None,
        operation_id: Annotated[str | None, Field(min_length=36, max_length=36)] = None,
        recover_only: bool = False,
        record_only: Annotated[
            list[DenialRecordingRetry] | None, Field(min_length=1, max_length=32)
        ] = None,
    ) -> _BudgetCheckBatchResult:
        return await operation.run_sync(
            partial(
                _budget_check_batch_operation,
                launch_options=launch,
                checks=[
                    check.model_dump(mode="json", exclude_unset=True)
                    for check in checks
                ]
                if checks is not None
                else None,
                no_record=no_record,
                estimated_output_tokens=estimated_output_tokens,
                operation_id=operation_id,
                recover_only=recover_only,
                record_only=record_only,
            )
        )

    @catalog.tool(
        name="get_overview",
        title="Read account overview",
        credential_requirement="login",
        description=(
            "Read calendar-month account spend, requests, trend, savings, and "
            "free-tier quota metadata. Requires a Solwyn login."
        ),
    )
    async def get_overview() -> _OverviewResult:
        return await operation.run_sync(partial(_overview_operation, launch))

    @catalog.tool(
        name="list_cost_events",
        title="List cost events",
        credential_requirement="either",
        description="Read cursor-paginated cost events with shared filters and bounded all_pages.",
    )
    async def list_cost_events(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: str = "7d",
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
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        cursor: str | None = None,
        all_pages: bool = False,
    ) -> _ListCostEventsResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListCostEventsResult,
                fetch_cost_events,
                launch,
                project_id=project,
                project_read=True,
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
                limit=limit,
                cursor=cursor,
                all_pages=all_pages,
            )
        )

    @catalog.tool(
        name="get_cost_baseline",
        title="Get cost baseline",
        credential_requirement="either",
        description="Read committed daily spend baseline excluding today.",
    )
    async def get_cost_baseline(
        project: Annotated[str | None, Field(min_length=1)] = None,
        days: Annotated[int, Field(ge=3, le=30)] = 7,
    ) -> _GetCostBaselineResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetCostBaselineResult,
                fetch_project_read,
                launch,
                project_id=project,
                project_read=True,
                path="spend-baseline",
                response_model=BaselineResult,
                params={"days": days},
            )
        )

    @catalog.tool(
        name="get_unit_economics",
        title="Get unit economics",
        credential_requirement="either",
        description="Read unit-cost metrics and optional server-inferred causes.",
    )
    async def get_unit_economics(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: AlertRange = "30d",
        vs_prev: bool = True,
        cause: bool = False,
    ) -> _GetUnitEconomicsResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetUnitEconomicsResult,
                fetch_project_read,
                launch,
                project_id=project,
                project_read=True,
                path="unit-economics",
                response_model=UnitEconomicsResponse,
                params={"range": time_range, "vs_prev": vs_prev, "cause": cause},
            )
        )

    @catalog.tool(
        name="get_cost_drift",
        title="Get cost drift",
        credential_requirement="either",
        description="Compare historical stored spend and estimates with coverage and caveats.",
    )
    async def get_cost_drift(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: str = "7d",
        from_: str | None = None,
        to: str | None = None,
    ) -> _GetCostDriftResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetCostDriftResult,
                fetch_drift_options,
                launch,
                project_id=project,
                project_read=True,
                time_range=time_range,
                from_=from_,
                to=to,
            )
        )

    @catalog.tool(
        name="list_priced_models",
        title="List priced models",
        credential_requirement="either",
        description="List exact available priced targets and current pricing version.",
    )
    async def list_priced_models(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _ListPricedModelsResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListPricedModelsResult,
                fetch_priced_models,
                launch,
                project_id=project,
                project_read=True,
            )
        )

    @catalog.tool(
        name="simulate_costs",
        title="Simulate costs",
        credential_requirement="either",
        description="Read-only historical repricing; never reserves or bills. 429: one bounded retry. No 503/transport replay.",
    )
    async def simulate_costs(
        provider: ProviderName,
        model: Annotated[str, Field(min_length=1, max_length=2048)],
        project: Annotated[str | None, Field(min_length=1)] = None,
        region: Annotated[str | None, Field(max_length=32)] = None,
        service_tier: ServiceTier | None = None,
        time_range: str = "7d",
        from_: str | None = None,
        to: str | None = None,
    ) -> _SimulateCostsResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _SimulateCostsResult,
                fetch_simulation_options,
                launch,
                project_id=project,
                project_read=True,
                provider=provider,
                model=model,
                region=region,
                service_tier=service_tier,
                time_range=time_range,
                from_=from_,
                to=to,
            )
        )

    @catalog.tool(
        name="list_provider_failovers",
        title="List provider failovers",
        credential_requirement="either",
        description="Read recent provider/model failover groups, capped by Core at 50 groups.",
    )
    async def list_provider_failovers(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: FailoverRange = FailoverRange.SEVEN_DAYS,
    ) -> _ListProviderFailoversResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListProviderFailoversResult,
                fetch_provider_failovers,
                launch,
                project_id=project,
                project_read=True,
                time_range=time_range,
            )
        )

    @catalog.tool(
        name="list_untracked_surfaces",
        title="List untracked surfaces",
        credential_requirement="either",
        description="Read untracked surfaces with effective retention window, capped by Core at 25 observations.",
    )
    async def list_untracked_surfaces(
        project: Annotated[str | None, Field(min_length=1)] = None,
        time_range: UntrackedRange = UntrackedRange.SEVEN_DAYS,
    ) -> _ListUntrackedSurfacesResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListUntrackedSurfacesResult,
                fetch_untracked_surfaces,
                launch,
                project_id=project,
                project_read=True,
                time_range=time_range,
            )
        )

    @catalog.tool(
        name="list_audit_events",
        title="List audit events",
        credential_requirement="login",
        description="Read account audit events newest first with exact action filters and bounded pagination.",
    )
    async def list_audit_events(
        time_range: AuditRange = AuditRange.DAYS_30,
        action: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        cursor: str | None = None,
        all_pages: bool = False,
    ) -> _ListAuditEventsResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListAuditEventsResult,
                fetch_audit_events,
                launch,
                login_required=True,
                time_range=time_range,
                action=action,
                cursor=cursor,
                all_pages=all_pages,
            )
        )

    @catalog.tool(
        name="plan_status",
        credential_note="Accepts a Solwyn login or a project key with account context; no project selection is required.",
        title="Plan status",
        credential_requirement="either",
        description="Read current plan limits, capabilities and subscription lifecycle state.",
    )
    async def plan_status() -> _PlanStatusResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _PlanStatusResult,
                fetch_plan,
                launch,
            )
        )

    @catalog.tool(
        name="plan_catalog",
        credential_note="Accepts a Solwyn login or project keys with authenticated user context; no project selection is required.",
        title="Plan catalog",
        credential_requirement="either",
        description="Read plan catalog: prices in cents, currency, included requests, decimal cent overage, limits and capabilities.",
    )
    async def plan_catalog() -> _PlanCatalogResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _PlanCatalogResult,
                fetch_billing_catalog,
                launch,
            )
        )

    @catalog.tool(
        name="get_whoami",
        credential_note="A Solwyn login reads user/account/project identity; project keys return only local auth and selected-project context without revealing the key.",
        title="Get whoami",
        credential_requirement="either",
        description="Read account identity and email verification; project keys return local key context without a user identity.",
    )
    async def get_whoami() -> _GetWhoamiResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetWhoamiResult,
                fetch_identity,
                launch,
                identity=True,
            )
        )

    @catalog.tool(
        name="get_account_phone",
        title="Get account phone",
        credential_requirement="login",
        description="Read user phone verification and SMS consent state. Verification and consent writes stay in the dashboard.",
    )
    async def get_account_phone() -> _GetAccountPhoneResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetAccountPhoneResult,
                fetch_phone,
                launch,
                login_required=True,
            )
        )

    @catalog.tool(
        name="list_alert_activity",
        title="List alert activity",
        credential_requirement="login",
        description="Read account-wide activity across all projects plus account scope with delivery receipts.",
    )
    async def list_alert_activity(
        time_range: AlertRange = "30d",
        event_type: ActivityType | None = None,
        cursor: str | None = None,
        all_pages: bool = False,
    ) -> _ListAlertActivityResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListAlertActivityResult,
                request_account_alert_page,
                launch,
                login_required=True,
                time_range=time_range,
                event_type=event_type,
                cursor=cursor,
                all_pages=all_pages,
            )
        )

    @catalog.tool(
        name="list_open_alert_deliveries",
        title="List open alert deliveries",
        credential_requirement="login",
        description="Read pending/retrying deliveries and attempts regardless of event retention.",
    )
    async def list_open_alert_deliveries(
        cursor: str | None = None, all_pages: bool = False
    ) -> _ListOpenAlertDeliveriesResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListOpenAlertDeliveriesResult,
                request_account_alert_page,
                launch,
                login_required=True,
                pending=True,
                cursor=cursor,
                all_pages=all_pages,
            )
        )

    @catalog.tool(
        name="budget_uncounted",
        title="Budget uncounted",
        credential_requirement="either",
        description="Read advisory unpriced upper-bound offline traffic, possibly overcounted. A 503 means unknown; retry the read, never assume zero or no past traffic.",
    )
    async def budget_uncounted(
        project: Annotated[str | None, Field(min_length=1)] = None,
    ) -> _BudgetUncountedResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _BudgetUncountedResult,
                fetch_uncounted,
                launch,
                project_id=project,
                project_read=True,
            )
        )

    @catalog.tool(
        name="get_account_alert",
        title="Get account alert",
        credential_requirement="login",
        description="Read account spend alert configuration, optionally latest firing and lifecycle history within Core’s 30d retention window.",
    )
    async def get_account_alert(
        with_last_fired: bool = False,
    ) -> _GetAccountAlertResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _GetAccountAlertResult,
                request_account_alert,
                launch,
                login_required=True,
                with_last_fired=with_last_fired,
            )
        )

    @catalog.tool(
        name="plan_preview",
        title="Plan preview",
        credential_requirement="login",
        credential_note="Requires a Solwyn login at any account role; no selected project required.",
        description="Read a plan-change estimate: immediate proration or scheduling at term end, integer cents, currency and line periods. Never changes a subscription or bills; not a price lock. No 503/transport replay.",
    )
    async def plan_preview(
        tier: PaidTier, interval: BillingInterval
    ) -> _PlanPreviewResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _PlanPreviewResult,
                fetch_plan_preview,
                launch,
                login_required=True,
                transport_read_hint="The preview is read-only and has no billing effect; retry the preview when connectivity returns.",
                tier=tier,
                interval=interval,
            )
        )

    @catalog.tool(
        name="list_team_members",
        title="List team members",
        credential_requirement="login",
        description="Read the complete active account roster in join order. Any account member may read it; no project selection is required.",
    )
    async def list_team_members() -> _ListTeamMembersResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListTeamMembersResult,
                fetch_members,
                launch,
                login_required=True,
            )
        )

    @catalog.tool(
        name="list_team_invites",
        title="List team invites",
        credential_requirement="login",
        description="Read all open account invitations and their delivery status, including expired invitations. Core requires owner/admin role; no project selection is required.",
    )
    async def list_team_invites() -> _ListTeamInvitesResult:
        return await operation.run_sync(
            partial(
                _read_operation,
                _ListTeamInvitesResult,
                fetch_invites,
                launch,
                login_required=True,
            )
        )

    return catalog


def get_tool_catalog(
    *,
    profile: Literal["read", "guardrails"] = "guardrails",
) -> tuple[ToolDeclaration, ...]:
    """Return fresh ordered declarations for local diagnostics, without auth/I/O.

    Credential requirements describe operations; they never enforce permissions.
    Each call builds independent definitions, not the running server's catalog.
    """
    return tuple(_create_catalog(McpLaunchOptions(profile=profile)).tools.values())


def create_server(*, launch_options: McpLaunchOptions | None = None) -> Server:
    """Build the stateless local MCP server with one fixed launch profile."""
    launch = launch_options or McpLaunchOptions()
    # The reporting tools' inputs are read now, at launch, and never again.
    launch._reporting.freeze(launch)
    catalog = _create_catalog(launch)
    server = Server(
        "solwyn",
        title="Solwyn",
        version=__version__,
        instructions=(
            "Use Solwyn tools for explicit project and budget operations. "
            "Budget status is a pure read and the only polling surface. "
            "Budget check creates a reservation only immediately before caller-made "
            "un-instrumented provider spend. "
            "Budget confirm settles a non-null reservation after that spend. "
            "Never wrap Solwyn SDK-instrumented calls with budget check or confirm. "
            "When check returns a null reservation, skip confirm."
        ),
        on_list_tools=catalog.list_tools,
        on_call_tool=catalog.call_tool,
    )
    _configure_private_server(server)
    return server
