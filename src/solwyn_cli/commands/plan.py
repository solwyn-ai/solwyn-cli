"""Current subscription plan and entitlement command."""

from contextlib import contextmanager, redirect_stdout
from datetime import datetime
import json
import sys
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import webbrowser
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli.approvals import pin_retry_value
from solwyn_cli.auth import AuthError, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._billing import (
    BillingInterval,
    PaidTier,
    CancelResponse,
    CheckoutRequest,
    CheckoutResponse,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanPreviewResponse,
    PortalResponse,
    fetch_plan_preview,
    require_billing_login,
    require_empty_204,
)
from solwyn_cli.commands._entitlement import (
    PendingPlan,
    ScheduledPlanChange,
    TierCapabilities,
    fetch_billing_status,
    fetch_billing_catalog,
)
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData, _terminal_safe
from solwyn_cli.runtime import command_context, output_engine


PLAN_HELP = help_contract(
    endpoint="GET /api/v1/billing/status",
    auth="login or API key with account context",
    exits="0 success; 1 generic/API; 2 usage; 3 auth",
    schema="solwyn.plan.v1",
)

_CTA_PATH = "/settings/billing"


class _PlanData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["free", "production", "team", "scale"]
    billing_interval: Literal["month", "year"] | None
    status: str | None
    current_period_end: datetime | None
    cancel_at: datetime | None
    grace_period_until: datetime | None
    scheduled_plan_change: ScheduledPlanChange | None
    pending_plan: PendingPlan | None
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


def _date(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else "none"


def _table(data: _PlanData) -> TableData:
    capabilities = data.capabilities
    period_end_label = "Period end"
    if (
        data.current_period_end is not None
        and data.tier != "free"
        and data.status in {"active", "trialing", "past_due"}
    ):
        period_end_label = "Ends" if data.cancel_at else "Renews"
    rows: list[tuple[str, str]] = [
        ("Tier", data.tier),
        ("Billing interval", data.billing_interval or "none"),
        ("Status", data.status or "none"),
    ]
    rows.extend(
        [
            (period_end_label, _date(data.current_period_end)),
            ("Cancel at", _date(data.cancel_at)),
        ]
    )
    if data.status == "past_due":
        rows.append(("Grace until", _date(data.grace_period_until)))
    change = data.scheduled_plan_change
    rows.extend(
        [
            (
                "Scheduled plan",
                f"{change.target_tier or 'unknown'} / {change.target_billing_interval or 'unknown'}"
                if change
                else "none",
            ),
            (
                "Scheduled effective at",
                _date(change.effective_at) if change else "none",
            ),
            (
                "Pending plan",
                f"{data.pending_plan.tier} / {data.pending_plan.billing_interval}"
                if data.pending_plan
                else "none",
            ),
        ]
    )
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


app = typer.Typer(
    invoke_without_command=True,
    help="Inspect plans and entitlements.",
    epilog=PLAN_HELP,
)


def fetch_plan(*, client: SolwynClient) -> _PlanData:
    status = fetch_billing_status(client=client)
    return _PlanData(
        **status.model_dump(exclude={"stripe_customer_id", "recent_invoices"}),
        cta_path=_CTA_PATH,
    )


@app.callback()
def plan(context: typer.Context) -> None:
    """Show the current plan, subscription lifecycle, limits, and capabilities."""
    if context.invoked_subcommand is not None:
        return
    runtime = command_context(context)
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            data = fetch_plan(client=client)
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


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/billing/catalog",
        auth="login or API key with user context",
        exits="0 success; 1 generic/API; 2 usage; 3 auth",
        schema="solwyn.plan.catalog.v1",
    ),
)
def list_plans(context: typer.Context) -> None:
    """Compare catalog prices in cents, included requests, limits and capabilities."""
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            data = fetch_billing_catalog(client=client)
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_plan_catalog_response",
                message="The plan catalog response was invalid.",
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.plan.catalog.v1",
        data=data,
        table=TableData(
            columns=(
                "Tier",
                "Currency",
                "Month cents",
                "Year cents",
                "Included requests",
                "Overage decimal cents",
                "Limits",
                "Capabilities",
            ),
            rows=tuple(
                (
                    tier.tier,
                    data.currency,
                    str(tier.prices.month.amount_cents)
                    if tier.prices.month
                    else "none",
                    str(tier.prices.year.amount_cents) if tier.prices.year else "none",
                    str(tier.overage.included_requests) if tier.overage else "none",
                    tier.overage.unit_amount_decimal_cents if tier.overage else "none",
                    str(tier.limits),
                    tier.capabilities.model_dump_json(),
                )
                for tier in data.tiers.values()
            ),
            fold_cells=True,
        ),
    )


# Billing writes remain CLI-only. Core enforces billing.manage (owner today).
def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _effects(context: typer.Context, text: str) -> None:
    # Explicit literal text and terminal sanitization keep server descriptions
    # out of Rich markup and escape sequences; stdout stays one JSON result.
    typer.echo(_terminal_safe(text), err=True)


def _check_confirmation(context: typer.Context, *, yes: bool, effect: str) -> None:
    if not yes and not _is_interactive():
        exit_with_error(
            context,
            OutputError(
                code="confirmation_required",
                message=f"{effect} Confirmation is required.",
                hint="Use an interactive terminal or pass --yes to acknowledge these effects.",
            ),
            semantic=ErrorSemantic.USAGE,
        )


def _confirm(context: typer.Context, *, yes: bool) -> None:
    if yes:
        return
    try:
        with redirect_stdout(sys.stderr):
            accepted = typer.confirm(
                "Continue with these billing effects?", default=False, err=True
            )
    except (EOFError, typer.Abort):
        accepted = False
    if not accepted:
        exit_with_error(
            context,
            OutputError(
                code="confirmation_declined",
                message="Billing action was not submitted.",
                hint="Review the effects before confirming or using --yes.",
            ),
            semantic=ErrorSemantic.USAGE,
        )


@contextmanager
def _billing_client(context: typer.Context):
    credential = required_credential(context, resolve=resolve_credential)
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            require_billing_login(client)
            yield client
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(
                code="authentication_failed",
                message=str(error),
                hint="Run solwyn login. If SOLWYN_AUTH=project_key is set, unset it.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_billing_response",
                message="The billing response was invalid.",
                hint="If a write was submitted, its outcome may be unknown; inspect solwyn plan before retrying.",
            ),
        )


def _billing_help(
    endpoint: str, schema: str, *, auth: str = "owner login (billing.manage)"
) -> str:
    return help_contract(
        endpoint=endpoint,
        auth=auth,
        exits="0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found",
        schema=schema,
        guidance=(
            "Effects are shown before interactive confirmation or --yes. No automatic 503/transport replay.",
        ),
    )


def _billing_table(data: BaseModel) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(
            (field, json.dumps(value, ensure_ascii=True))
            for field, value in data.model_dump(mode="json").items()
        ),
        fold_cells=True,
    )


def _lifecycle_effect(status: _PlanData) -> str:
    return (
        f"Current plan: {status.tier} / {status.billing_interval or 'none'}; status: {status.status or 'none'}. "
        f"Current term end: {_date(status.current_period_end)}; cancel_at: {_date(status.cancel_at)}; "
        f"grace_period_until: {_date(status.grace_period_until)}. "
        f"Scheduled change: {status.scheduled_plan_change.model_dump_json() if status.scheduled_plan_change else 'none'}."
    )


@app.command(
    "cancel",
    epilog=_billing_help(
        "GET /api/v1/billing/status then POST /api/v1/billing/cancel",
        "solwyn.plan.cancel.v1",
    ),
)
def cancel(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm cancellation at the current term end."),
    ] = False,
) -> None:
    """End the plan at the current term end (the year for annual plans), no proration or refund."""
    effect = "End the plan at the current term end (the current year term on annual plans), with no proration or refund."
    _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        status = fetch_plan(client=client)
        _effects(context, effect + " " + _lifecycle_effect(status))
        _confirm(context, yes=yes)
        data = CancelResponse.model_validate(
            client.request("POST", "/api/v1/billing/cancel").json()
        )
    output_engine(context).write_success(
        schema="solwyn.plan.cancel.v1", data=data, table=_billing_table(data)
    )


@app.command(
    "resume",
    epilog=_billing_help(
        "GET /api/v1/billing/status then DELETE /api/v1/billing/cancel",
        "solwyn.plan.resume.v1",
    ),
)
def resume(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Confirm clearing cancellation and resuming renewal."
        ),
    ] = False,
) -> None:
    """Clear a Solwyn or portal cancellation so renewal resumes at the term end."""
    effect = "Clear the cancellation and resume paid renewal at the current term end."
    _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        status = fetch_plan(client=client)
        _effects(context, effect + " " + _lifecycle_effect(status))
        _confirm(context, yes=yes)
        data = CancelResponse.model_validate(
            client.request("DELETE", "/api/v1/billing/cancel").json()
        )
    output_engine(context).write_success(
        schema="solwyn.plan.resume.v1", data=data, table=_billing_table(data)
    )


class _PlanChangeData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool
    preview: PlanPreviewResponse
    result: PlanChangeResponse | None


class _PlanDiscardData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discarded: bool


class _PlanDismissData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dismissed: bool


def _preview_effect(preview: PlanPreviewResponse) -> str:
    timing = (
        "Applies immediately with proration."
        if preview.mode == "immediate"
        else "Scheduled at the current term end; nothing due now."
    )
    return (
        f"Change to {preview.target_tier} / {preview.target_billing_interval}. {timing} "
        "Estimate, not a price lock; Core derives the final timing and amounts may drift. "
        + preview.model_dump_json()
    )


@app.command(
    "change",
    epilog=_billing_help(
        "POST /api/v1/billing/plan-preview then POST /api/v1/billing/plan-change; --discard uses GET status then DELETE /api/v1/billing/plan-change",
        "solwyn.plan.change.v1; solwyn.plan.discard.v1",
        auth="login for preview; owner login (billing.manage) for writes",
    ),
)
def change(
    context: typer.Context,
    tier: Annotated[
        PaidTier | None,
        typer.Option("--tier", help="Target paid tier: production, team or scale."),
    ] = None,
    interval: Annotated[
        BillingInterval | None,
        typer.Option("--interval", help="Target billing interval: month or year."),
    ] = None,
    discard: Annotated[
        bool,
        typer.Option(
            "--discard",
            help="Discard the scheduled plan change; cannot combine with targets or --dry-run.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Read the actual estimate without submitting a change; login at any role.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Confirm the displayed plan change or discard effects."
        ),
    ] = False,
) -> None:
    """Preview then confirm a plan change, or discard a scheduled change."""
    if (discard and (tier is not None or interval is not None or dry_run)) or (
        not discard and (tier is None or interval is None)
    ):
        exit_with_error(
            context,
            OutputError(
                code="invalid_plan_change_arguments",
                message="Use --tier and --interval together, or --discard alone; --discard cannot use --dry-run.",
                hint="Use plan change --tier T --interval month|year --dry-run for a read-only estimate.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    effect = (
        "Discard the scheduled change; the current plan renews as it is."
        if discard
        else f"Change the paid plan to {tier} / {interval} after reviewing the estimate."
    )
    if not dry_run:
        _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        if discard:
            status = fetch_plan(client=client)
            _effects(context, effect + " " + _lifecycle_effect(status))
            _confirm(context, yes=yes)
            require_empty_204(client.request("DELETE", "/api/v1/billing/plan-change"))
            data = _PlanDiscardData(discarded=True)
        else:
            preview = fetch_plan_preview(client=client, tier=tier, interval=interval)
            result = None
            if not dry_run:
                _effects(context, _preview_effect(preview))
                _confirm(context, yes=yes)
                request = PlanChangeRequest(
                    target_tier=tier, target_billing_interval=interval
                )
                result = PlanChangeResponse.model_validate(
                    client.request(
                        "POST", "/api/v1/billing/plan-change", json=request.model_dump()
                    ).json()
                )
            data = _PlanChangeData(dry_run=dry_run, preview=preview, result=result)
    output_engine(context).write_success(
        schema="solwyn.plan.discard.v1" if discard else "solwyn.plan.change.v1",
        data=data,
        table=_billing_table(data),
    )


def _safe_hosted_url(url: str) -> bool:
    if (
        any(
            unicodedata.category(char).startswith("C") or char.isspace() for char in url
        )
        or "\\" in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _hosted_result(
    context: typer.Context,
    *,
    data: CheckoutResponse | PortalResponse,
    schema: str,
    no_browser: bool,
) -> None:
    url = data.checkout_url if isinstance(data, CheckoutResponse) else data.portal_url
    if not _safe_hosted_url(url):
        raise ValueError("Invalid hosted URL.")
    # Print in full even in a narrow terminal. Creation does not mean paid.
    output_engine(context).write_success(
        schema=schema,
        data=data,
        table=TableData(columns=("Hosted URL",), rows=((url,),), fold_cells=True),
    )
    if _is_interactive() and not no_browser:
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            _effects(
                context,
                "Browser could not open; use the printed URL manually. The hosted session was created.",
            )


@app.command(
    "upgrade",
    epilog=_billing_help("POST /api/v1/billing/checkout", "solwyn.plan.upgrade.v1"),
)
def upgrade(
    context: typer.Context,
    tier: Annotated[
        PaidTier,
        typer.Option("--tier", help="Paid checkout tier: production, team or scale."),
    ],
    interval: Annotated[
        BillingInterval,
        typer.Option("--interval", help="Checkout billing interval: month or year."),
    ],
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the URL without opening a browser."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm creating the paid checkout session.")
    ] = False,
    client_request_id: Annotated[
        UUID | None,
        typer.Option(
            "--client-request-id",
            hidden=True,
            help="Repeat a checkout request ID; printed in approval retry commands.",
        ),
    ] = None,
) -> None:
    """Create a hosted paid checkout URL; always print it, open only with a terminal."""
    effect = f"Create a paid checkout session for {tier} / {interval}; older open checkout sessions may be replaced. Payment and subscription start are completed in hosted checkout."
    _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        _effects(context, effect)
        _confirm(context, yes=yes)
        request_id = str(client_request_id or uuid4())
        # An approval retry must repeat the request ID to rebuild the same body.
        pin_retry_value("client_request_id", request_id)
        request = CheckoutRequest(
            tier=tier, billing_interval=interval, client_request_id=request_id
        )
        data = CheckoutResponse.model_validate(
            client.request(
                "POST", "/api/v1/billing/checkout", json=request.model_dump()
            ).json()
        )
        _hosted_result(
            context, data=data, schema="solwyn.plan.upgrade.v1", no_browser=no_browser
        )


@app.command(
    "portal",
    epilog=_billing_help("POST /api/v1/billing/portal", "solwyn.plan.portal.v1"),
)
def portal(
    context: typer.Context,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the URL without opening a browser."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm creating a billing portal session.")
    ] = False,
) -> None:
    """Create a hosted billing portal URL; always print it, open only with a terminal."""
    effect = "Create a hosted billing portal session to manage payment methods, invoices and available subscription settings; changes require action in the portal."
    _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        _effects(context, effect)
        _confirm(context, yes=yes)
        data = PortalResponse.model_validate(
            client.request("POST", "/api/v1/billing/portal").json()
        )
        _hosted_result(
            context, data=data, schema="solwyn.plan.portal.v1", no_browser=no_browser
        )


pending_app = typer.Typer(
    help="Manage the captured paid-signup plan intent.",
    epilog=help_contract(
        endpoint="composite router for pending plan intent",
        auth="owner login (billing.manage)",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    ),
)
app.add_typer(pending_app, name="pending")


@pending_app.command(
    "dismiss",
    epilog=_billing_help(
        "GET /api/v1/billing/status then DELETE /api/v1/billing/plan-intent",
        "solwyn.plan.pending.dismiss.v1",
    ),
)
def dismiss(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm dismissing the captured signup intent."),
    ] = False,
) -> None:
    """Dismiss pending signup intent; does not cancel or change an active subscription."""
    effect = "Dismiss the captured pending signup intent. This does not cancel or change an active subscription."
    _check_confirmation(context, yes=yes, effect=effect)
    with _billing_client(context) as client:
        status = fetch_plan(client=client)
        _effects(
            context,
            effect
            + f" Pending intent: {status.pending_plan.model_dump_json() if status.pending_plan else 'none'}.",
        )
        _confirm(context, yes=yes)
        require_empty_204(client.request("DELETE", "/api/v1/billing/plan-intent"))
    data = _PlanDismissData(dismissed=True)
    output_engine(context).write_success(
        schema="solwyn.plan.pending.dismiss.v1", data=data, table=_billing_table(data)
    )
