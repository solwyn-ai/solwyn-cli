"""Current subscription plan and entitlement command."""

from typing import Literal

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._entitlement import TierCapabilities, fetch_billing_status
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


PLAN_HELP = help_contract(
    endpoint="GET /api/v1/billing/status",
    auth="JWT or project key with account context",
    exits="0 success; 1 generic/API; 2 usage; 3 auth",
    schema="solwyn.plan.v1",
)

_CTA_PATH = "/billing"


class _PlanData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["free", "production", "team", "scale"]
    billing_interval: Literal["month", "year"] | None
    status: str | None
    limits: dict[str, int]
    capabilities: TierCapabilities
    cta_path: str


def _list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _boolean(value: bool) -> str:
    return "yes" if value else "no"


def _limit(value: int) -> str:
    return "unlimited" if value == -1 else str(value)


def _chunked_rows(label: str, values: list[str]) -> list[tuple[str, str]]:
    if not values:
        return [(label, "none")]
    return [
        (label if offset == 0 else "", ", ".join(values[offset : offset + 2]))
        for offset in range(0, len(values), 2)
    ]


def _table(data: _PlanData) -> TableData:
    capabilities = data.capabilities
    rows: list[tuple[str, str]] = [
        ("Tier", data.tier),
        ("Billing interval", data.billing_interval or "none"),
        ("Status", data.status or "none"),
    ]
    limit_labels = {
        "api_keys_per_project": "Limit api keys/project",
        "projects": "Limit projects",
        "requests_per_month": "Limit requests/month",
        "webhook_targets_per_project": "Limit webhook targets",
    }
    rows.extend(
        (limit_labels.get(name, f"Limit {name}"), _limit(value))
        for name, value in sorted(data.limits.items())
    )
    rows.append(("Analytics ranges", _list(capabilities.analytics.allowed_ranges)))
    rows.extend(
        _chunked_rows("Analytics group by", capabilities.analytics.allowed_group_by)
    )
    rows.extend(
        [
            ("Analytics tags", _boolean(capabilities.analytics.tags)),
            ("Analytics CSV", capabilities.analytics.csv_export_mode),
            (
                "Paginated cost events",
                _boolean(capabilities.analytics.cost_events_paginated),
            ),
            ("Budget modes", _list(capabilities.budget.allowed_modes)),
            ("Budget periods", _list(capabilities.budget.allowed_periods)),
            ("Budget sub-period", _boolean(capabilities.budget.sub_period_enabled)),
            ("Max thresholds", str(capabilities.budget.max_thresholds)),
            (
                "Hard-cap projects",
                _limit(capabilities.budget.max_hard_cap_projects),
            ),
            (
                "Account spend alerts",
                _boolean(capabilities.budget.account_spend_alerts),
            ),
            (
                "Notification channels",
                _list(capabilities.notifications.notification_channels),
            ),
            ("API-key expiry", _boolean(capabilities.api_keys.expiry_configurable)),
            ("Ingest/second", str(capabilities.ingest.rate_limit_per_second)),
            ("Checks/minute", str(capabilities.enforcement.checks_per_minute)),
            (
                "Failover entitlement",
                _boolean(capabilities.failover_entitlement),
            ),
            ("Data retention", f"{capabilities.data_retention_days} days"),
            ("Upgrade in dashboard", data.cta_path),
        ]
    )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def plan(context: typer.Context) -> None:
    """Show the current plan, limits, and capabilities."""
    runtime = command_context(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            status = fetch_billing_status(client=client)
        data = _PlanData(
            tier=status.tier,
            billing_interval=status.billing_interval,
            status=status.status,
            limits=status.limits,
            capabilities=status.capabilities,
            cta_path=_CTA_PATH,
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
                code="invalid_plan_response",
                message="The billing status response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.plan.v1",
        data=data,
        table=_table(data),
    )
