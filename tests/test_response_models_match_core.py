"""Every response model the CLI parses accepts what Core's spec says it returns.

The vendored snapshot is Core's OpenAPI document. A strict model (extra="forbid")
that lacks a property Core returns rejects every response that carries it, so
the CLI reports invalid_*_response and MCP reports invalid_read_response. That
happened for cost events (is_provider_fallback, failover_reason and the trace
fields) and agent runs (first_trace_id, trace_url).

RESPONSE_MODELS maps each response-parsing model to its spec component. For
every pair: a strict model declares every spec property; a property the spec
allows to be null accepts null; the model allows every spec enum value; and the
model requires nothing the spec does not require. Every target must be an
object schema, so a pairing with an enum or scalar component cannot pass
vacuously. CLASSIFIED_NOT_RESPONSES names every other strict model, so a new
response model cannot skip this guard; only MCP tool result envelopes are
recognised by type, because their data models are classified on their own.
"""

import copy
import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import solwyn_cli


SNAPSHOT = json.loads(Path("src/solwyn_cli/spec/openapi_snapshot.json").read_text())
COMPONENTS = SNAPSHOT["components"]["schemas"]

# "module:QualifiedName" -> Core component. Nested models are listed too.
RESPONSE_MODELS = {
    "solwyn_cli.client:ApprovalView": "CliApprovalResponse",
    "solwyn_cli.commands._account_reads:AccountPhoneResponse": "AccountPhoneResponse",
    "solwyn_cli.commands._account_reads:UncountedTallyResponse": "UncountedTallyResponse",
    "solwyn_cli.commands._alert_models:AlertDeliveryLegEntry": "AlertDeliveryLegEntry",
    "solwyn_cli.commands._alert_models:AlertDeliveryStatus": "AlertDeliveryStatus",
    "solwyn_cli.commands._alert_models:AlertEventEntry": "AlertEventEntry",
    "solwyn_cli.commands._alert_models:AlertEventListResponse": "AlertEventListResponse",
    "solwyn_cli.commands._alert_models:AlertEventWindow": "AlertEventWindow",
    "solwyn_cli.commands._alert_reads:AlertLatestResponse": "AlertLatestResponse",
    "solwyn_cli.commands._alert_reads:AlertOpenResponse": "AlertOpenResponse",
    "solwyn_cli.commands._alert_reads:AlertRuleLookup": "AlertRuleLookup",
    "solwyn_cli.commands._billing:CancelResponse": "CancelResponse",
    "solwyn_cli.commands._billing:CheckoutResponse": "CheckoutResponse",
    "solwyn_cli.commands._billing:PlanChangeResponse": "PlanChangeResponse",
    "solwyn_cli.commands._billing:PlanPreviewLineResponse": "PlanPreviewLineResponse",
    "solwyn_cli.commands._billing:PlanPreviewResponse": "PlanPreviewResponse",
    "solwyn_cli.commands._billing:PortalResponse": "PortalResponse",
    "solwyn_cli.commands._cost_advice_models:BaselineResult": "BaselineResult",
    "solwyn_cli.commands._cost_advice_models:CauseResult": "CauseResult",
    "solwyn_cli.commands._cost_advice_models:CauseSpan": "CauseSpan",
    "solwyn_cli.commands._cost_advice_models:CostDriftResponse": "CostDriftResponse",
    "solwyn_cli.commands._cost_advice_models:CostSimulationResponse": "CostSimulationResponse",
    "solwyn_cli.commands._cost_advice_models:CostSimulationTarget": "CostSimulationTarget",
    "solwyn_cli.commands._cost_advice_models:DeltaCell": "DeltaCell",
    "solwyn_cli.commands._cost_advice_models:DriftCaveats": "DriftCaveats",
    "solwyn_cli.commands._cost_advice_models:DriftGroup": "DriftGroup",
    "solwyn_cli.commands._cost_advice_models:PricedModelTarget": "PricedModelTarget",
    "solwyn_cli.commands._cost_advice_models:PricedModelsResponse": "PricedModelsResponse",
    "solwyn_cli.commands._cost_advice_models:SimulationCaveats": "SimulationCaveats",
    "solwyn_cli.commands._cost_advice_models:SimulationExcludedCounts": "SimulationExcludedCounts",
    "solwyn_cli.commands._cost_advice_models:UnitEconomicsResponse": "UnitEconomicsResponse",
    "solwyn_cli.commands._cost_models:AccountCostDailyPoint": "AccountCostDailyPoint",
    "solwyn_cli.commands._cost_models:AccountCostGroup": "AccountCostGroup",
    "solwyn_cli.commands._cost_models:AccountCostQueryResponse": "AccountCostQueryResponse",
    "solwyn_cli.commands._cost_models:CostEventResponse": "CostEventResponse",
    "solwyn_cli.commands._cost_models:CostEventsResponse": "CostEventsResponse",
    "solwyn_cli.commands._cost_models:CostGroup": "CostGroup",
    "solwyn_cli.commands._cost_models:CostQueryResponse": "CostQueryResponse",
    "solwyn_cli.commands._cost_models:CostTotalResponse": "CostTotalResponse",
    "solwyn_cli.commands._cost_models:CostWindow": "CostWindow",
    "solwyn_cli.commands._cost_models:MediaBreakdown": "MediaBreakdown",
    "solwyn_cli.commands._cost_models:SavingsSummary": "SavingsSummary",
    "solwyn_cli.commands._entitlement:AnalyticsCapabilities": "AnalyticsCapabilities",
    "solwyn_cli.commands._entitlement:ApiKeyCapabilities": "ApiKeyCapabilities",
    "solwyn_cli.commands._entitlement:BillingCatalogOverage": "BillingCatalogOverage",
    "solwyn_cli.commands._entitlement:BillingCatalogPriceMonth": "BillingCatalogPriceMonth",
    "solwyn_cli.commands._entitlement:BillingCatalogPriceYear": "BillingCatalogPriceYear",
    "solwyn_cli.commands._entitlement:BillingCatalogPrices": "BillingCatalogPrices",
    "solwyn_cli.commands._entitlement:BillingCatalogResponse": "BillingCatalogResponse",
    "solwyn_cli.commands._entitlement:BillingCatalogTier": "BillingCatalogTier",
    "solwyn_cli.commands._entitlement:BudgetCapabilities": "BudgetCapabilities",
    "solwyn_cli.commands._entitlement:EnforcementCapabilities": "EnforcementCapabilities",
    "solwyn_cli.commands._entitlement:IngestCapabilities": "IngestCapabilities",
    "solwyn_cli.commands._entitlement:InvoiceSummary": "InvoiceSummary",
    "solwyn_cli.commands._entitlement:NotificationCapabilities": "NotificationCapabilities",
    "solwyn_cli.commands._entitlement:PendingPlan": "PendingPlan",
    "solwyn_cli.commands._entitlement:ScheduledPlanChange": "ScheduledPlanChangeResponse",
    "solwyn_cli.commands._entitlement:SubscriptionStatusResponse": "SubscriptionStatusResponse",
    "solwyn_cli.commands._entitlement:TierCapabilities": "TierCapabilities",
    "solwyn_cli.commands._notifications_common:AlertEmailResponse": "AlertEmailResponse",
    "solwyn_cli.commands._notifications_common:DeliveryResponse": "SlackTestResponse",
    "solwyn_cli.commands._notifications_common:EmailConfig": "EmailConfig",
    "solwyn_cli.commands._notifications_common:EmailTestResponse": "EmailTestResponse",
    "solwyn_cli.commands._notifications_common:NotificationCapabilitiesResponse": "NotificationCapabilitiesResponse",
    "solwyn_cli.commands._notifications_common:NotificationChannelResponse": "NotificationChannelResponse",
    "solwyn_cli.commands._notifications_common:NotificationConfigResponse": "NotificationConfigResponse",
    "solwyn_cli.commands._notifications_common:SmsConfig": "SmsConfig",
    "solwyn_cli.commands._record:IngestDuplicate": "IngestDuplicate",
    "solwyn_cli.commands._record:IngestRejection": "IngestRejection",
    "solwyn_cli.commands._record:IngestResponse": "IngestResponse",
    "solwyn_cli.commands._team:InvitationOut": "InvitationOut",
    "solwyn_cli.commands._team:InvitationPreviewResponse": "InvitationPreviewResponse",
    "solwyn_cli.commands._team:MemberOut": "MemberOut",
    "solwyn_cli.commands.alerts:AccountAlertResponse": "AccountAlertResponse",
    "solwyn_cli.commands.alerts:AccountAlertSnoozeResponse": "AccountAlertSnoozeResponse",
    "solwyn_cli.commands.attest:AttestationAccountSpendAlert": "AttestationAccountSpendAlert",
    "solwyn_cli.commands.attest:AttestationAlertEvent": "AttestationAlertEvent",
    "solwyn_cli.commands.attest:AttestationAlerts": "AttestationAlerts",
    "solwyn_cli.commands.attest:AttestationAudit": "AttestationAudit",
    "solwyn_cli.commands.attest:AttestationAuditEvent": "AttestationAuditEvent",
    "solwyn_cli.commands.attest:AttestationBudget": "AttestationBudget",
    "solwyn_cli.commands.attest:AttestationScope": "AttestationScope",
    "solwyn_cli.commands.attest:AttestationSpendSummary": "AttestationSpendSummary",
    "solwyn_cli.commands.attest:AttestationSubscription": "AttestationSubscription",
    "solwyn_cli.commands.attest:AttestationSubscriptionSnapshot": "AttestationSubscriptionSnapshot",
    "solwyn_cli.commands.attest:AttestationWindow": "AttestationWindow",
    "solwyn_cli.commands.attest:SpendControlAttestation": "SpendControlAttestation",
    "solwyn_cli.commands.audit:AuditEventEntry": "AuditEventEntry",
    "solwyn_cli.commands.audit:AuditEventListResponse": "AuditEventListResponse",
    "solwyn_cli.commands.budget:BudgetCheckBatchItem": "BudgetCheckBatchItem",
    "solwyn_cli.commands.budget:_AlertRouting": "AlertRouting",
    "solwyn_cli.commands.budget:_BudgetCheckBatchResponse": "BudgetCheckBatchResponse",
    "solwyn_cli.commands.budget:_BudgetCheckData": "BudgetCheckResponse",
    "solwyn_cli.commands.budget:_JournalBudgetDecision": "BudgetCheckResponse",
    "solwyn_cli.commands.budget:BudgetCheckOutcome[_JournalBudgetDecision]": (
        "BudgetCheckOutcome"
    ),
    "solwyn_cli.commands.budget:RecoverableBudgetCheckResponse[_JournalBudgetDecision]": (
        "RecoverableBudgetCheckResponse"
    ),
    "solwyn_cli.commands.budget:_BudgetConstraints": "BudgetConstraintsResponse",
    "solwyn_cli.commands.budget:_BudgetStatusData": "BudgetStatusResponse",
    "solwyn_cli.commands.budget:_BudgetThresholdState": "ThresholdStateResponse",
    "solwyn_cli.commands.budget:_DailyAnomalyState": "DailyAnomalyStateResponse",
    "solwyn_cli.commands.budget:_ForecastOverrunState": "ForecastOverrunStateResponse",
    "solwyn_cli.commands.budget:_RoutingChannel": "RoutingChannel",
    "solwyn_cli.commands.budget:_RoutingMuted": "RoutingMuted",
    "solwyn_cli.commands.budget:_RunControlDirective": "RunControlDirective",
    "solwyn_cli.commands.budget:_RunawayRunState": "RunawayRunStateResponse",
    "solwyn_cli.commands.budget:_ScopedBudgetState": "ScopedBudgetStateResponse",
    "solwyn_cli.commands.budget:_UnitCostAnomalyState": "UnitCostAnomalyStateResponse",
    "solwyn_cli.commands.budget_rules:AlertsPreviewResponse": "AlertsPreviewResponse",
    "solwyn_cli.commands.budget_rules:BudgetRestoreDefaultsResponse": "BudgetRestoreDefaultsResponse",
    "solwyn_cli.commands.budget_rules:RuleActionResponse": "RuleActionResponse",
    "solwyn_cli.commands.budget_rules:SnoozeResponse": "SnoozeResponse",
    "solwyn_cli.commands.budget_rules:_RestoreThreshold": "BudgetThresholdEntry",
    "solwyn_cli.commands.credentials:_CredentialList": "CliCredentialListResponse",
    "solwyn_cli.commands.doctor:ReadinessCheck": "ReadinessCheck",
    "solwyn_cli.commands.doctor:ReadinessResponse": "ReadinessResponse",
    "solwyn_cli.commands.doctor:_BudgetProbeResponse": "BudgetStatusResponse",
    "solwyn_cli.commands.doctor:_MeProbeResponse": "MeResponse",
    "solwyn_cli.commands.forecast:_ForecastResponse": "ForecastResponse",
    "solwyn_cli.commands.keys:_ProjectKeyMetadata": "ProjectKeyResponse",
    "solwyn_cli.commands.keys:_ProjectKeyRotateResponse": "ProjectKeyRotateResponse",
    "solwyn_cli.commands.mcp_doctor:_AccountResponse": "MeResponse",
    "solwyn_cli.commands.mcp_doctor:_KeyResponse": "AuthenticatedKeyResponse",
    "solwyn_cli.commands.mcp_doctor:_ReadResponse": "BudgetStatusResponse",
    "solwyn_cli.commands.overview:OverviewResponse": "OverviewResponse",
    "solwyn_cli.commands.projects:_Project": "ProjectResponse",
    "solwyn_cli.commands.projects:_ProjectCreateResponse": "ProjectCreateResponse",
    "solwyn_cli.commands.providers:ProviderFailoverResponse": "ProviderFailoverResponse",
    "solwyn_cli.commands.providers:UntrackedSurfaceResponse": "UntrackedSurfaceResponse",
    "solwyn_cli.commands.runs:_AgentRunEntry": "AgentRunEntry",
    "solwyn_cli.commands.runs:_AgentRunListData": "AgentRunListResponse",
    "solwyn_cli.commands.runs:_AgentRunStopData": "AgentRunStopResponse",
    "solwyn_cli.commands.runs:_CostWindow": "CostWindow",
    "solwyn_cli.commands.status:_BudgetResponse": "BudgetStatusResponse",
    "solwyn_cli.commands.status:_ProviderHealth": "ProviderHealthResponse",
    "solwyn_cli.commands.tags:TagDiscoveryResponse": "TagDiscoveryResponse",
    "solwyn_cli.commands.tags:TagKeySummary": "TagKeySummary",
    "solwyn_cli.commands.tags:TagRegistryUsage": "TagRegistryUsage",
    "solwyn_cli.commands.tags:TagValueSummary": "TagValueSummary",
    "solwyn_cli.commands.tags:TagValuesPage": "TagValuesPage",
    "solwyn_cli.commands.webhooks:WebhookCreateResponse": "WebhookCreateResponse",
    "solwyn_cli.commands.webhooks:WebhookResponse": "WebhookResponse",
    "solwyn_cli.commands.webhooks:WebhookRotateResponse": "WebhookRotateResponse",
    "solwyn_cli.commands.whoami:_AccountSelf": "AccountSelfResponse",
    "solwyn_cli.commands.whoami:_Identity": "MeResponse",
    "solwyn_cli.commands.whoami:_Project": "ProjectResponse",
    "solwyn_cli.errors:BillingConflictDetail": "BillingConflictDetail",
    "solwyn_cli.errors:BillingNotFoundDetail": "BillingNotFoundDetail",
    "solwyn_cli.errors:BillingUnavailableDetail": "BillingUnavailableDetail",
    "solwyn_cli.errors:BillingValidationDetail": "BillingValidationDetail",
    "solwyn_cli.oauth:DeviceAuthorization": "DeviceAuthorizationResponse",
    "solwyn_cli.oauth:TokenResponse": "DeviceTokenResponse",
}

# Strict models that never parse a Core response, by reason.
_LOCAL_STATE = "keychain, config, IPC or recovery state the CLI writes and reads itself"
_CLI_OUTPUT = "a CLI or MCP output payload the CLI builds"
_BASE = "a shared base that only sets model_config"
_GENERIC = "an unparametrized generic; its parametrized response form is registered"
_REQUEST = (
    "a request body, or part of one, the CLI builds; test_request_drift guards it"
)
_MCP_INPUT = "an MCP tool input: a described copy of a request mirror"
CLASSIFIED_NOT_RESPONSES = {
    "solwyn_cli._mcp_doctor_ipc:HelperResult": _LOCAL_STATE,
    "solwyn_cli._mcp_doctor_ipc:PrivateRequest": _LOCAL_STATE,
    "solwyn_cli._mcp_launch:LaunchDescriptor": _LOCAL_STATE,
    "solwyn_cli._mcp_operation:OperationRecovery": _LOCAL_STATE,
    "solwyn_cli._mcp_operation:ReadRecovery": _LOCAL_STATE,
    "solwyn_cli._mcp_operation:RecoveryItem": _LOCAL_STATE,
    "solwyn_cli._vendor.batch:BudgetCheckOutcome": _GENERIC,
    "solwyn_cli._vendor.batch:BudgetCheckOutcome[_BudgetCheckData]": _CLI_OUTPUT,
    "solwyn_cli._vendor.batch:RecoverableBudgetCheckRequest": _REQUEST,
    "solwyn_cli._vendor.batch:RecoverableBudgetCheckResponse": _GENERIC,
    "solwyn_cli._vendor.notification_wire:AlertEmailUpdate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:DiscordConfig": _REQUEST,
    "solwyn_cli._vendor.notification_wire:EmailConfigUpdate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:NotificationChannelCreate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:NotificationChannelUpdate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:NotificationConfigUpdate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:NotificationWire": _BASE,
    "solwyn_cli._vendor.notification_wire:PagerDutyConfig": _REQUEST,
    "solwyn_cli._vendor.notification_wire:PagerDutyTestRequest": _REQUEST,
    "solwyn_cli._vendor.notification_wire:SlackConfig": _REQUEST,
    "solwyn_cli._vendor.notification_wire:SlackTestRequest": _REQUEST,
    "solwyn_cli._vendor.notification_wire:SmsConfigUpdate": _REQUEST,
    "solwyn_cli._vendor.notification_wire:TeamsConfig": _REQUEST,
    "solwyn_cli._vendor.notification_wire:TelegramConfig": _REQUEST,
    "solwyn_cli._vendor.wire:AccountAlertPreviewRequest": _REQUEST,
    "solwyn_cli._vendor.wire:AccountAlertSnoozeRequest": _REQUEST,
    "solwyn_cli._vendor.wire:AccountAlertUpdateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:AlertPreviewItem": _REQUEST,
    "solwyn_cli._vendor.wire:BudgetCheckBatchRequest": _REQUEST,
    "solwyn_cli._vendor.wire:BudgetCheckRequest": _REQUEST,
    "solwyn_cli._vendor.wire:BudgetConfirmRequest": _REQUEST,
    "solwyn_cli._vendor.wire:BudgetUpdateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:CostSimulationRequest": _REQUEST,
    "solwyn_cli._vendor.wire:IngestMetadataEvent": _REQUEST,
    "solwyn_cli._vendor.wire:MediaUsage": _REQUEST,
    "solwyn_cli._vendor.wire:ProjectAlertsPreviewRequest": _REQUEST,
    "solwyn_cli._vendor.wire:ProjectCreateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:ProjectKeyRotateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:ProjectUpdateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:SnoozeRequest": _REQUEST,
    "solwyn_cli._vendor.wire:TokenDetails": _REQUEST,
    "solwyn_cli._vendor.wire:WebhookCreateRequest": _REQUEST,
    "solwyn_cli._vendor.wire:_DailyAnomalyConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_ForecastOverrunConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_RunawayRunConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_ScopedBudgetConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_ThresholdConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_UnitCostAnomalyConfig": _REQUEST,
    "solwyn_cli._vendor.wire:_WireModel": _BASE,
    "solwyn_cli.artifacts:_ArtifactState": _LOCAL_STATE,
    "solwyn_cli.auth:LoginCredential": _LOCAL_STATE,
    "solwyn_cli.auth:LoginMetadata": _LOCAL_STATE,
    "solwyn_cli.auth:PendingDevice": _LOCAL_STATE,
    "solwyn_cli.auth:ProjectKeyCredential": _LOCAL_STATE,
    "solwyn_cli.commands._alert_reads:AccountAlertWithLatestResponse": _CLI_OUTPUT,
    "solwyn_cli.commands._alert_reads:AlertLatestRequest": _REQUEST,
    "solwyn_cli.commands._alert_reads:RuleLatest": _CLI_OUTPUT,
    "solwyn_cli.commands._billing:CheckoutRequest": _REQUEST,
    "solwyn_cli.commands._billing:PlanChangeRequest": _REQUEST,
    "solwyn_cli.commands._billing:PlanPreviewRequest": _REQUEST,
    "solwyn_cli.commands._billing:_BillingModel": _BASE,
    "solwyn_cli.commands._cost_advice_models:_Response": _BASE,
    "solwyn_cli.commands._cost_models:_Response": _BASE,
    "solwyn_cli.commands._entitlement:_Response": _BASE,
    "solwyn_cli.commands._notifications_common:ChannelDeleted": _CLI_OUTPUT,
    "solwyn_cli.commands._notifications_common:ChannelList": _CLI_OUTPUT,
    "solwyn_cli.commands._notifications_common:ReadModel": _BASE,
    "solwyn_cli.commands._record:RecordingResult": _CLI_OUTPUT,
    "solwyn_cli.commands._team:AccountUpdateRequest": _REQUEST,
    "solwyn_cli.commands._team:InviteRequest": _REQUEST,
    "solwyn_cli.commands._team:RoleUpdateRequest": _REQUEST,
    "solwyn_cli.commands._team:TeamInvitesData": _CLI_OUTPUT,
    "solwyn_cli.commands._team:TeamMembersData": _CLI_OUTPUT,
    "solwyn_cli.commands._team:TeamModel": _BASE,
    "solwyn_cli.commands.attest:_AttestationModel": _BASE,
    "solwyn_cli.commands.audit:_AuditQuery": _LOCAL_STATE,
    "solwyn_cli.commands.budget:BudgetCheckBatchData": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:DenialRecordingRetry": _LOCAL_STATE,
    "solwyn_cli.commands.budget:RecoverableBatchData": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:RecoverableBatchItem": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:_BudgetConfirmData": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:_BudgetSetData": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:_BudgetStatusOutput": _CLI_OUTPUT,
    "solwyn_cli.commands.budget:_ThresholdStateBase": _BASE,
    "solwyn_cli.commands.budget_rules:BudgetRulesData": _CLI_OUTPUT,
    "solwyn_cli.commands.credentials:_RevokeData": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_AuthenticatedProbe": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_ApiUrl": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_Diagnostic": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_DoctorData": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_ReadinessCheckDiagnostic": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_ReadinessDiagnostic": _CLI_OUTPUT,
    "solwyn_cli.commands.doctor:_SnapshotDiagnostic": _CLI_OUTPUT,
    "solwyn_cli.commands.forecast:ForecastData": _CLI_OUTPUT,
    "solwyn_cli.commands.init:_InitData": _CLI_OUTPUT,
    "solwyn_cli.commands.init:_Snippet": _CLI_OUTPUT,
    "solwyn_cli.commands.init:_Verification": _CLI_OUTPUT,
    "solwyn_cli.commands.keys:_ProjectKeyRevokeData": _CLI_OUTPUT,
    "solwyn_cli.commands.login:_LoginData": _CLI_OUTPUT,
    "solwyn_cli.commands.mcp_doctor:Check": _CLI_OUTPUT,
    "solwyn_cli.commands.mcp_doctor:CredentialCheck": _CLI_OUTPUT,
    "solwyn_cli.commands.mcp_doctor:KeyContext": _CLI_OUTPUT,
    "solwyn_cli.commands.mcp_doctor:McpDoctorData": _CLI_OUTPUT,
    "solwyn_cli.commands.mcp_doctor:ToolConstraint": _CLI_OUTPUT,
    "solwyn_cli.commands.plan:_PlanChangeData": _CLI_OUTPUT,
    "solwyn_cli.commands.plan:_PlanData": _CLI_OUTPUT,
    "solwyn_cli.commands.plan:_PlanDiscardData": _CLI_OUTPUT,
    "solwyn_cli.commands.plan:_PlanDismissData": _CLI_OUTPUT,
    "solwyn_cli.commands.projects:_ProjectDeleteData": _CLI_OUTPUT,
    "solwyn_cli.commands.projects:_ProjectsListData": _CLI_OUTPUT,
    "solwyn_cli.commands.providers:ProviderFailoversData": _CLI_OUTPUT,
    "solwyn_cli.commands.providers:UntrackedSurfacesData": _CLI_OUTPUT,
    "solwyn_cli.commands.status:_ProjectStatusData": _CLI_OUTPUT,
    "solwyn_cli.commands.status:_StatusData": _CLI_OUTPUT,
    "solwyn_cli.commands.tags:TagRetirement": _CLI_OUTPUT,
    "solwyn_cli.commands.webhooks:_WebhookDeleteData": _CLI_OUTPUT,
    "solwyn_cli.commands.webhooks:_WebhookListData": _CLI_OUTPUT,
    "solwyn_cli.commands.whoami:_CredentialSummary": _CLI_OUTPUT,
    "solwyn_cli.commands.whoami:_WhoamiData": _CLI_OUTPUT,
    "solwyn_cli.errors:_BillingErrorDetail": _BASE,
    "solwyn_cli.mcp.parameters:BudgetCheckRequest": _MCP_INPUT,
    "solwyn_cli.mcp.parameters:DenialRecordingRetry": _MCP_INPUT,
    "solwyn_cli.mcp.parameters:IngestMetadataEvent": _MCP_INPUT,
    "solwyn_cli.mcp.parameters:MediaUsage": _MCP_INPUT,
    "solwyn_cli.mcp.parameters:TokenDetails": _MCP_INPUT,
    "solwyn_cli.mcp.server:McpLaunchOptions": _LOCAL_STATE,
    "solwyn_cli.mcp.server:_ToolError": _CLI_OUTPUT,
    "solwyn_cli.output:OneTimeSecretPanel": _CLI_OUTPUT,
    "solwyn_cli.output:OutputError": _CLI_OUTPUT,
    "solwyn_cli.output:TableData": _CLI_OUTPUT,
    "solwyn_cli.runtime:CommandContext": _LOCAL_STATE,
}


def _is_mcp_tool_result(cls: type) -> bool:
    """MCP result envelopes; the data models they wrap are classified above."""
    from solwyn_cli.mcp import server

    return cls.__module__ == server.__name__ and issubclass(cls, server._ToolResult)


def _load(qualified: str) -> type[BaseModel]:
    module, name = qualified.split(":")
    # Parametrized generics ("Outcome[Item]") resolve in the module's namespace.
    return eval(name, vars(importlib.import_module(module)))  # noqa: S307


def _object_schema(component: str) -> dict:
    schema = _resolve(COMPONENTS[component], SNAPSHOT)
    assert schema.get("type", "object") == "object" and (
        "properties" in schema or "allOf" in schema
    ), f"{component} is not an object schema, so no response model can map to it"
    return schema


def _resolve(schema: dict, root: dict) -> dict:
    while "$ref" in schema:
        parts = schema["$ref"].removeprefix("#/").split("/")
        node: Any = root
        for part in parts:
            node = node[part]
        schema = node
    return schema


def _branches(schema: dict, root: dict) -> list[dict]:
    schema = _resolve(schema, root)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return [b for item in schema[key] for b in _branches(item, root)]
    return [schema]


def _accepts_null(schema: dict, root: dict) -> bool:
    return not schema or any(
        b.get("type") == "null" or not b for b in _branches(schema, root)
    )


def _enum_values(schema: dict, root: dict) -> set:
    values: set = set()
    for branch in _branches(schema, root):
        values |= set(branch.get("enum", []))
        if "const" in branch:
            values.add(branch["const"])
    return values


def _properties(component: str) -> tuple[dict, set[str]]:
    schema = _object_schema(component)
    properties = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    for part in schema.get("allOf", []):
        part = _resolve(part, SNAPSHOT)
        properties.update(part.get("properties", {}))
        required |= set(part.get("required", []))
    return properties, required


@pytest.mark.parametrize(("qualified", "component"), sorted(RESPONSE_MODELS.items()))
def test_response_model_accepts_everything_core_returns(
    qualified: str, component: str
) -> None:
    model = _load(qualified)
    spec_properties, spec_required = _properties(component)
    local = model.model_json_schema(mode="validation", by_alias=True)
    local_properties = local.get("properties", {})
    local_required = set(local.get("required", []))
    if model.model_config.get("extra") == "forbid":
        rejected = sorted(set(spec_properties) - set(local_properties))
        assert rejected == [], f"{qualified} rejects {component} fields {rejected}"
    for name, remote in spec_properties.items():
        if name not in local_properties:
            continue
        mine = local_properties[name]
        if _accepts_null(remote, SNAPSHOT):
            assert _accepts_null(mine, local), f"{qualified}.{name} rejects null"
        allowed = _enum_values(mine, local)
        if allowed:
            missing = _enum_values(remote, SNAPSHOT) - allowed
            assert not missing, f"{qualified}.{name} rejects {sorted(missing)}"
    cli_only = sorted(local_required - spec_required)
    assert cli_only == [], (
        f"{qualified} requires {cli_only}, which {component} does not require"
    )


def _strict_models() -> dict[str, type[BaseModel]]:
    # Import everything first: parametrizing a generic in one module (e.g.
    # Outcome[Item] in commands.budget) adds a class to another module.
    modules = [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(solwyn_cli.__path__, "solwyn_cli.")
    ]
    found = {}
    for module in modules:
        for value in list(vars(module).values()):
            if (
                inspect.isclass(value)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
                and value.model_config.get("extra") == "forbid"
            ):
                found[f"{value.__module__}:{value.__qualname__}"] = value
    return found


def _unclassified() -> list[str]:
    # By identity: a registered generic such as Response[Item] reports the
    # module that defines the generic, not the one that parametrizes it.
    registered = {_load(qualified) for qualified in RESPONSE_MODELS}
    return sorted(
        qualified
        for qualified, model in _strict_models().items()
        if model not in registered
        and qualified not in CLASSIFIED_NOT_RESPONSES
        and not _is_mcp_tool_result(model)
    )


def test_every_strict_model_is_classified() -> None:
    unclassified = _unclassified()
    assert unclassified == [], (
        "Classify each strict model: add a response model to RESPONSE_MODELS "
        f"with its Core component, or name why it never parses a response: {unclassified}"
    )


def test_registry_entries_exist() -> None:
    for qualified, component in RESPONSE_MODELS.items():
        assert component in COMPONENTS, (qualified, component)
        _object_schema(component)
        assert issubclass(_load(qualified), BaseModel), qualified
    stale = sorted(set(CLASSIFIED_NOT_RESPONSES) - set(_strict_models()))
    assert stale == [], f"classified models that no longer exist: {stale}"


@pytest.mark.parametrize(
    ("component", "change"),
    [
        ("CostEventResponse", "new_property"),
        ("AgentRunEntry", "new_property"),
        ("CostEventResponse", "nullable_cost"),
        ("FailoverReason", "new_enum_value"),
    ],
)
def test_guard_trips_when_core_changes_a_response(
    monkeypatch: pytest.MonkeyPatch, component: str, change: str
) -> None:
    snapshot = copy.deepcopy(SNAPSHOT)
    schemas = snapshot["components"]["schemas"]
    if change == "new_property":
        schemas[component]["properties"]["added_by_core"] = {"type": "string"}
    elif change == "nullable_cost":
        schemas[component]["properties"]["cost"] = {
            "anyOf": [{"type": "number"}, {"type": "null"}]
        }
    else:
        schemas[component]["enum"].append("added_by_core")
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOT", snapshot)
    monkeypatch.setattr(sys.modules[__name__], "COMPONENTS", schemas)
    model = {
        "CostEventResponse": "solwyn_cli.commands._cost_models:CostEventResponse",
        "FailoverReason": "solwyn_cli.commands._cost_models:CostEventResponse",
        "AgentRunEntry": "solwyn_cli.commands.runs:_AgentRunEntry",
    }[component]
    target = RESPONSE_MODELS[model]

    with pytest.raises(AssertionError):
        test_response_model_accepts_everything_core_returns(model, target)


# Review follow-ups: every required field and every target schema is checked,
# and classification cannot be satisfied by a name or a module alone.

COST_EVENT = "solwyn_cli.commands._cost_models:CostEventResponse"


def test_guard_rejects_a_field_only_the_cli_requires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solwyn_cli.commands._cost_models import CostEventResponse

    class _ReviewCostEvent(CostEventResponse):
        required_only_by_cli: str

    monkeypatch.setattr(
        sys.modules[__name__], "_ReviewCostEvent", _ReviewCostEvent, raising=False
    )

    with pytest.raises(AssertionError, match="required_only_by_cli"):
        test_response_model_accepts_everything_core_returns(
            f"{__name__}:_ReviewCostEvent", "CostEventResponse"
        )


@pytest.mark.parametrize("target", ["FailoverReason", "CallStatus"])
def test_guard_rejects_a_target_that_is_not_an_object_schema(target: str) -> None:
    with pytest.raises(AssertionError, match="object schema"):
        test_response_model_accepts_everything_core_returns(COST_EVENT, target)


def test_registry_rejects_every_model_mapped_to_an_unrelated_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys.modules[__name__],
        "RESPONSE_MODELS",
        {qualified: "FailoverReason" for qualified in RESPONSE_MODELS},
    )

    with pytest.raises(AssertionError, match="object schema"):
        test_registry_entries_exist()


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("solwyn_cli.commands.costs", "ReviewRequest"),
        ("solwyn_cli.commands.costs", "ReviewResponse"),
        ("solwyn_cli.mcp.server", "_ReviewNotAToolResult"),
        ("solwyn_cli._vendor.batch", "ReviewVendorResponse"),
    ],
)
def test_classification_catches_a_new_strict_model(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str
) -> None:
    from pydantic import ConfigDict

    module = importlib.import_module(module_name)
    synthetic = type(
        class_name,
        (BaseModel,),
        {
            "__module__": module_name,
            "__qualname__": class_name,
            "__annotations__": {"value": str},
            "model_config": ConfigDict(extra="forbid"),
        },
    )
    monkeypatch.setattr(module, class_name, synthetic, raising=False)

    with pytest.raises(AssertionError) as caught:
        test_every_strict_model_is_classified()

    assert f"{module_name}:{class_name}" in str(caught.value)
