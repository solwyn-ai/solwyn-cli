"""Budget rules, routing previews, and explicit enforcement state actions."""

import json
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from solwyn_cli.commands._alert_models import AlertEventEntry, AlertEventWindow
from urllib.parse import quote

import typer
from pydantic import BaseModel, ConfigDict, TypeAdapter, Field

from solwyn_cli._vendor.wire import (
    BudgetMode,
    BudgetUpdateRequest,
    ProjectAlertsPreviewRequest,
    ProviderName,
    SnoozeRequest,
    _BudgetRule,
)
from solwyn_cli.auth import AuthError, JwtCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.commands.budget import (
    _AlertRouting,
    _ThresholdState,
    _configuration_from_status,
    request_budget_status,
    request_budget_update,
)
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

_RULE_NOTICE = "A snoozed hard_deny still denies; pause releases enforcement."
_KEY_NOTICE = (
    "Rule keys are content-addressed; editing keyed fields changes the rule_key."
)
RuleType = Literal[
    "budget_threshold",
    "runaway_run",
    "scoped_budget",
    "forecast_overrun",
    "daily_anomaly",
    "unit_cost_anomaly",
]
Scope = Literal["model", "provider", "agent_run", "tag"]
_RULE_ADAPTER = TypeAdapter(_BudgetRule)


def _help(
    endpoint: str, *, write: bool = True, schema: str = "solwyn.budget.rule_action.v1"
) -> str:
    return help_contract(
        endpoint=endpoint,
        auth="JWT owner/admin" if write else "key or JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema=schema,
        guidance=(_RULE_NOTICE, _KEY_NOTICE),
    )


rules_app = typer.Typer(
    help="Inspect budget rules.",
    epilog=_help(
        "GET /api/v1/projects/{project_id}/budget",
        write=False,
        schema="solwyn.budget.rules.v1",
    ),
)
rule_app = typer.Typer(
    help="Manage individual budget rules.", epilog=_help("composite budget rule router")
)
threshold_app = typer.Typer(
    help="Snooze threshold notifications by percentage.",
    epilog=_help("composite budget threshold router"),
)


class RuleRequestError(ValueError):
    """A locally detected rule request error, before any mutation."""


class BudgetRulesData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    rules: list[_ThresholdState]
    last_fired: dict[str, AlertEventEntry | None] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    latest_window: AlertEventWindow | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


BudgetRulesWithLatestData = BudgetRulesData


class RuleActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_key: str
    paused: bool | None = None
    deleted: bool = False


class SnoozeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snoozed_until: datetime | None


class AlertsPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[_AlertRouting]


class _RestoreThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percent: int
    channel_ids: list[str]
    snoozed_until: datetime | None = None


class BudgetRestoreDefaultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    thresholds: list[_RestoreThreshold]


@contextmanager
def _client(
    context: typer.Context, *, write: bool
) -> Iterator[tuple[SolwynClient, str]]:
    credential = required_credential(context, resolve=resolve_credential)
    if write and not isinstance(credential, JwtCredential):
        exit_with_error(
            context,
            OutputError(
                code="jwt_required",
                message="Budget rule mutations require a user login with owner/admin permissions.",
                hint="Run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    runtime = command_context(context)
    if not runtime.project:
        exit_with_error(
            context,
            OutputError(
                code="project_required",
                message="Select a project with --project or SOLWYN_PROJECT_ID.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            yield client, runtime.project
    except ApiError as error:
        if (
            error.status_code == 422
            and error.code == "unknown_notification_channel"
            and isinstance(error.detail, dict)
        ):
            ids = error.detail.get("channel_ids", [])
            error = ApiError(
                status_code=422,
                code=error.code,
                message=error.message,
                hint="Unknown notification channel IDs: "
                + ", ".join(str(i) for i in ids),
                detail=error.detail,
            )
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
                code="invalid_budget_rules_response",
                message="The budget rules response was invalid.",
            ),
        )


def request_budget_rules(
    *, client: SolwynClient, project_id: str, with_last_fired: bool = False
) -> BudgetRulesData:
    if with_last_fired:
        from solwyn_cli.commands._alert_reads import require_alert_jwt

        require_alert_jwt(client)
    status = request_budget_status(client=client, project_id=project_id)
    data = BudgetRulesData(project_id=project_id, rules=status.thresholds)
    if not with_last_fired:
        return data
    from solwyn_cli.commands._alert_reads import AlertRuleLookup, request_latest_events

    latest, window = request_latest_events(
        client=client,
        rules=[
            AlertRuleLookup(
                rule_key=r.rule_key,
                project_id=project_id,
                rule_type=r.type,
                percent=getattr(r, "percent", None),
            )
            for r in data.rules
        ],
    )
    return BudgetRulesWithLatestData(
        **data.model_dump(),
        latest_window=window,
        last_fired={
            rule.rule_key: firing.last_fired
            for rule, firing in zip(data.rules, latest, strict=True)
        },
    )


def _rules_table(data: BudgetRulesData) -> TableData:
    rows = []
    for rule in data.rules:
        params = rule.model_dump(
            mode="json",
            exclude={
                "type",
                "rule_key",
                "routing",
                "paused",
                "snoozed_until",
                "channel_ids",
                "mode",
                "current_usage",
            },
            exclude_none=True,
        )
        usage = getattr(rule, "current_usage", None)
        usage_limit = (
            ("—" if usage is None else f"{usage:.6g}") + f" / {rule.limit:.6g}"
            if rule.type == "scoped_budget"
            else "—"
        )
        rows.append(
            (
                rule.rule_key,
                rule.type,
                json.dumps(params, sort_keys=True),
                getattr(rule, "mode", "alert_only"),
                str(rule.paused).lower(),
                rule.snoozed_until.isoformat() if rule.snoozed_until else "—",
                str(rule.routing.will_send).lower(),
                ", ".join(f"{c.name} ({c.id})" for c in rule.routing.channels) or "—",
                rule.routing.muted.reason if rule.routing.muted else "—",
                usage_limit,
            )
        )
    with_latest = data.last_fired is not None
    if with_latest:
        rows = [
            (
                *row,
                data.last_fired[row[0]].emitted_at.isoformat()
                if data.last_fired[row[0]]
                else "—",
            )
            for row in rows
        ]
    return TableData(
        columns=(
            "Rule key",
            "Type",
            "Params",
            "Mode",
            "Paused",
            "Snoozed until",
            "Will send",
            "Channels",
            "Muted reason",
            "Usage / limit",
        )
        + (("Last fired",) if with_latest else ()),
        rows=tuple(rows),
        fold_cells=True,
    )


def _write_rules(
    context: typer.Context, data: BudgetRulesData, *, added: bool = False
) -> None:
    typer.echo(_RULE_NOTICE, err=True)
    output_engine(context).write_success(
        schema="solwyn.budget.rules.v1",
        data=data,
        table=TableData(
            columns=("Field", "Value"),
            rows=tuple(
                (key, json.dumps(value, sort_keys=True))
                for key, value in data.rules[0].model_dump(mode="json").items()
            ),
            fold_cells=True,
        )
        if added
        else _rules_table(data),
    )


@rules_app.command(
    "list",
    epilog=_help(
        "GET /api/v1/projects/{project_id}/budget",
        write=False,
        schema="solwyn.budget.rules.v1",
    ),
)
def list_rules(
    context: typer.Context,
    with_last_fired: Annotated[
        bool,
        typer.Option(
            "--with-last-fired",
            help="JWT only: include latest firing within Core’s 30d retention window.",
        ),
    ] = False,
) -> None:
    """List conditions, routing, and separate paused and snoozed states."""
    with _client(context, write=False) as (client, project):
        data = request_budget_rules(
            client=client, project_id=project, with_last_fired=with_last_fired
        )
    _write_rules(context, data)


def build_rule(
    *,
    kind: str,
    percent: int | None = None,
    spend: float | None = None,
    calls: int | None = None,
    window: int | None = None,
    mode: BudgetMode | None = None,
    scope: str | None = None,
    match: str | None = None,
    value: str | None = None,
    limit: float | None = None,
    multiplier: float | None = None,
    lookback: int | None = None,
    channels: list[str] | None = None,
) -> _BudgetRule:
    """Validate type-specific CLI options without silently discarding any."""
    options = {
        "percent": percent,
        "spend_threshold_usd": spend,
        "call_threshold": calls,
        "window_minutes": window,
        "mode": mode,
        "scope": scope,
        "match": match,
        "match_value": value,
        "limit": limit,
        "multiplier": multiplier,
        "lookback_days": lookback,
    }
    payload = {
        "type": kind,
        "channel_ids": channels or [],
        **{k: v for k, v in options.items() if v is not None},
    }
    if kind == "runaway_run" and mode is None:
        payload["mode"] = BudgetMode.ALERT_ONLY
    rule = _RULE_ADAPTER.validate_python(payload)
    _validate_rule(rule)
    return rule


def _validate_rule(rule: _BudgetRule) -> None:
    for value in rule.model_dump().values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Numeric rule parameters must be finite.")
    if rule.type == "runaway_run":
        if (rule.call_threshold is None) != (rule.window_minutes is None):
            raise ValueError("--calls and --window must be supplied together.")
        if Decimal(str(rule.spend_threshold_usd)) * 100 % 1:
            raise ValueError("Runaway spend must be whole cents.")
    if rule.type == "scoped_budget":
        if Decimal(str(rule.limit)) * 1_000_000 % 1:
            raise ValueError("Scoped limits must be exact micro-dollars.")
        if rule.scope != "tag":
            rule.match = rule.match.strip()
            if not rule.match:
                raise ValueError("Scoped match cannot be blank.")
        if rule.scope == "agent_run" and len(rule.match) > 256:
            raise ValueError("Run IDs cannot exceed 256 characters.")
        if rule.scope == "provider":
            ProviderName(rule.match)


def _identity(rule: _BudgetRule) -> tuple[object, ...]:
    if rule.type == "budget_threshold":
        return rule.type, rule.percent
    if rule.type in {"runaway_run", "forecast_overrun"}:
        return rule.type, round(rule.spend_threshold_usd * 100)
    if rule.type in {"daily_anomaly", "unit_cost_anomaly"}:
        return rule.type, round(rule.multiplier * 100)
    return rule.type, rule.scope, rule.match, rule.match_value


def request_rule_add(
    *, client: SolwynClient, project_id: str, rule: _BudgetRule
) -> _ThresholdState:
    """Guard a total replacement with a second read; never replay an uncertain PUT."""
    before = request_budget_status(client=client, project_id=project_id)
    config = _configuration_from_status(before)
    if len(config.thresholds) >= 50:
        raise RuleRequestError("A budget configuration supports at most 50 rules.")
    if (
        before.constraints.max_thresholds >= 0
        and len(config.thresholds) >= before.constraints.max_thresholds
    ):
        raise ApiError(
            status_code=402,
            code="upgrade_required",
            message=f"This project allows {before.constraints.max_thresholds} total budget rules across all types.",
            hint="Review your plan in the Solwyn dashboard.",
            detail={"feature": "budget.threshold_count"},
        )
    if any(_identity(existing) == _identity(rule) for existing in config.thresholds):
        raise RuleRequestError(
            "A rule with the same condition or scoped selector already exists."
        )
    rules = [*config.thresholds, rule]
    # Keep other rules in their original order and sort only threshold slots.
    thresholds = iter(
        sorted(
            (r for r in rules if r.type == "budget_threshold"), key=lambda r: r.percent
        )
    )
    rules = [next(thresholds) if r.type == "budget_threshold" else r for r in rules]
    request = BudgetUpdateRequest(**{**config.model_dump(), "thresholds": rules})
    current = request_budget_status(client=client, project_id=project_id)
    if _configuration_from_status(current) != config:
        raise ApiError(
            status_code=0,
            code="budget_configuration_changed",
            message="Budget configuration changed while adding the rule; no update was sent.",
            hint="Review solwyn budget rules list, then retry.",
            detail=None,
        )
    updated = request_budget_update(
        client=client, project_id=project_id, request=request
    )
    for result in updated.thresholds:
        candidate = _RULE_ADAPTER.validate_python(
            result.model_dump(exclude={"rule_key", "routing", "current_usage"})
        )
        if candidate == rule:
            return result
    raise ValueError("The updated response did not contain the added rule.")


@rule_app.command(
    "add",
    epilog=_help(
        "GET then PUT /api/v1/projects/{project_id}/budget",
        schema="solwyn.budget.rules.v1",
    ),
)
def add(
    context: typer.Context,
    kind: Annotated[RuleType, typer.Option("--type")],
    percent: Annotated[int | None, typer.Option("--percent")] = None,
    spend: Annotated[float | None, typer.Option("--spend")] = None,
    calls: Annotated[int | None, typer.Option("--calls")] = None,
    window: Annotated[int | None, typer.Option("--window")] = None,
    mode: Annotated[BudgetMode | None, typer.Option("--mode")] = None,
    scope: Annotated[Scope | None, typer.Option("--scope")] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    value: Annotated[str | None, typer.Option("--value")] = None,
    limit: Annotated[float | None, typer.Option("--limit")] = None,
    multiplier: Annotated[float | None, typer.Option("--multiplier")] = None,
    lookback: Annotated[int | None, typer.Option("--lookback")] = None,
    channels: Annotated[list[str] | None, typer.Option("--channel")] = None,
) -> None:
    """Append a rule, preserving the complete project configuration."""
    try:
        rule = build_rule(
            kind=kind,
            percent=percent,
            spend=spend,
            calls=calls,
            window=window,
            mode=mode,
            scope=scope,
            match=match,
            value=value,
            limit=limit,
            multiplier=multiplier,
            lookback=lookback,
            channels=channels,
        )
    except ValueError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_rule",
                message="Invalid rule parameters.",
                hint="Use only parameters for the selected --type; pair --calls/--window, and supply --value only for tag scope.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    with _client(context, write=True) as (client, project):
        try:
            result = request_rule_add(client=client, project_id=project, rule=rule)
        except RuleRequestError as error:
            exit_with_error(
                context,
                OutputError(code="invalid_budget_rule", message=str(error)),
                semantic=ErrorSemantic.USAGE,
            )
    typer.echo(_KEY_NOTICE, err=True)
    _write_rules(
        context, BudgetRulesData(project_id=project, rules=[result]), added=True
    )


def parse_until(value: str) -> datetime:
    duration = re.fullmatch(r"\+(\d+)([mhd])", value)
    if duration:
        seconds = int(duration[1]) * {"m": 60, "h": 3600, "d": 86400}[duration[2]]
        return datetime.now(UTC) + timedelta(seconds=seconds)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Snooze time must include a timezone.")
    return parsed


def _action(
    context: typer.Context,
    identifier: str,
    action: str,
    *,
    threshold: bool = False,
    until: str | None = None,
) -> None:
    body = None
    if until is not None:
        try:
            body = SnoozeRequest(snoozed_until=parse_until(until)).model_dump(
                mode="json"
            )
        except (ValueError, OverflowError):
            exit_with_error(
                context,
                OutputError(
                    code="invalid_snooze_time",
                    message="Use a timezone-aware ISO datetime or +2h, +30m, +1d.",
                ),
                semantic=ErrorSemantic.USAGE,
            )
    with _client(context, write=True) as (client, project):
        path = f"/api/v1/projects/{quote(project, safe='')}/budget/{'threshold' if threshold else 'rule'}/{quote(identifier, safe='')}"
        if action != "remove":
            path += "/" + action
        response = client.request(
            "DELETE" if action == "remove" else "POST",
            path,
            **({"json": body} if body is not None else {}),
        )
        data = (
            SnoozeResponse if action in {"snooze", "unsnooze"} else RuleActionResponse
        ).model_validate(response.json())
        if isinstance(data, RuleActionResponse) and data.rule_key != identifier:
            raise ValueError("Rule response did not match the request.")
    typer.echo(_RULE_NOTICE, err=True)
    output_engine(context).write_success(
        schema="solwyn.budget.rule_action.v1",
        data=data,
        table=TableData(
            columns=("Field", "Value"),
            rows=tuple(
                (k, str(v) if v is not None else "—")
                for k, v in data.model_dump(mode="json").items()
            ),
        ),
    )


@rule_app.command(
    "remove",
    epilog=_help("DELETE /api/v1/projects/{project_id}/budget/rule/{rule_key}"),
)
def remove(context: typer.Context, rule_key: str) -> None:
    """Remove a rule."""
    _action(context, rule_key, "remove")


@rule_app.command(
    "pause",
    epilog=_help("POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/pause"),
)
def pause(context: typer.Context, rule_key: str) -> None:
    """Pause notifications and release rule enforcement."""
    _action(context, rule_key, "pause")


@rule_app.command(
    "resume",
    epilog=_help("POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/resume"),
)
def resume(context: typer.Context, rule_key: str) -> None:
    """Resume notifications and rule enforcement."""
    _action(context, rule_key, "resume")


@rule_app.command(
    "snooze",
    epilog=_help("POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/snooze"),
)
def snooze(
    context: typer.Context,
    rule_key: str,
    until: Annotated[str, typer.Option("--until")],
) -> None:
    """Snooze notifications; hard-deny enforcement remains active."""
    _action(context, rule_key, "snooze", until=until)


@rule_app.command(
    "unsnooze",
    epilog=_help("POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/unsnooze"),
)
def unsnooze(context: typer.Context, rule_key: str) -> None:
    """Clear a rule's notification snooze."""
    _action(context, rule_key, "unsnooze")


@threshold_app.command(
    "snooze",
    epilog=_help(
        "POST /api/v1/projects/{project_id}/budget/threshold/{percent}/snooze"
    ),
)
def threshold_snooze(
    context: typer.Context,
    percent: Annotated[int, typer.Argument(min=1, max=100)],
    until: Annotated[str, typer.Option("--until")],
) -> None:
    """Snooze a budget threshold's notifications."""
    _action(context, str(percent), "snooze", threshold=True, until=until)


@threshold_app.command(
    "unsnooze",
    epilog=_help(
        "POST /api/v1/projects/{project_id}/budget/threshold/{percent}/unsnooze"
    ),
)
def threshold_unsnooze(
    context: typer.Context, percent: Annotated[int, typer.Argument(min=1, max=100)]
) -> None:
    """Clear a threshold's notification snooze."""
    _action(context, str(percent), "unsnooze", threshold=True)


def restore_defaults(context: typer.Context) -> None:
    """Restore threshold defaults, then read all retained rules."""
    with _client(context, write=True) as (client, project):
        response = client.request(
            "POST",
            f"/api/v1/projects/{quote(project, safe='')}/budget/restore-defaults",
        )
        restored = BudgetRestoreDefaultsResponse.model_validate(response.json())
        if restored.project_id != project:
            raise ValueError("Restore response project did not match.")
        data = request_budget_rules(client=client, project_id=project)
    _write_rules(context, data)


@rule_app.command(
    "preview",
    epilog=_help(
        "POST /api/v1/projects/{project_id}/alerts/preview",
        write=False,
        schema="solwyn.budget.rule_preview.v1",
    ),
)
def preview(
    context: typer.Context,
    rule_key: Annotated[str | None, typer.Argument()] = None,
    channels: Annotated[list[str] | None, typer.Option("--channel")] = None,
    kind: Annotated[RuleType | None, typer.Option("--type")] = None,
    percent: Annotated[int | None, typer.Option("--percent")] = None,
    spend: Annotated[float | None, typer.Option("--spend")] = None,
    calls: Annotated[int | None, typer.Option("--calls")] = None,
    window: Annotated[int | None, typer.Option("--window")] = None,
    mode: Annotated[BudgetMode | None, typer.Option("--mode")] = None,
    scope: Annotated[Scope | None, typer.Option("--scope")] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    value: Annotated[str | None, typer.Option("--value")] = None,
    limit: Annotated[float | None, typer.Option("--limit")] = None,
    multiplier: Annotated[float | None, typer.Option("--multiplier")] = None,
    lookback: Annotated[int | None, typer.Option("--lookback")] = None,
) -> None:
    """Preview saved or draft rule routing without mutating configuration."""
    draft = None
    options = dict(
        percent=percent,
        spend=spend,
        calls=calls,
        window=window,
        mode=mode,
        scope=scope,
        match=match,
        value=value,
        limit=limit,
        multiplier=multiplier,
        lookback=lookback,
    )
    try:
        if kind is not None:
            if rule_key is not None:
                raise ValueError("Choose a saved rule or a draft condition.")
            draft = build_rule(kind=kind, channels=channels, **options)
        elif any(v is not None for v in options.values()):
            raise ValueError("Draft condition parameters require --type.")
    except ValueError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_budget_rule",
                message="Choose a saved RULE_KEY or a valid --type and its condition parameters.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    with _client(context, write=False) as (client, project):
        if draft is None:
            config = request_budget_status(client=client, project_id=project)
            rules = [
                r
                for r in config.thresholds
                if rule_key is None or r.rule_key == rule_key
            ]
            if rule_key is not None and not rules:
                raise ApiError(
                    status_code=404,
                    code="rule_not_found",
                    message="Rule not found.",
                    hint=None,
                    detail=None,
                )
        else:
            rules = [draft]
        request = ProjectAlertsPreviewRequest(
            alerts=[
                {
                    "rule": r.model_dump(
                        exclude={"rule_key", "routing", "current_usage"}
                    ),
                    "channel_ids": channels,
                }
                for r in rules
            ]
        )
        response = client.request(
            "POST",
            f"/api/v1/projects/{quote(project, safe='')}/alerts/preview",
            json=request.model_dump(mode="json"),
        )
        data = AlertsPreviewResponse.model_validate(response.json())
        if len(data.results) != len(rules):
            raise ValueError("Preview result count did not match the request.")
    output_engine(context).write_success(
        schema="solwyn.budget.rule_preview.v1",
        data=data,
        table=TableData(
            columns=(
                "Rule key",
                "Will send",
                "Email",
                "Channels",
                "Source",
                "Muted reason",
            ),
            rows=tuple(
                (
                    getattr(rule, "rule_key", "draft"),
                    str(r.will_send).lower(),
                    r.email or "—",
                    ", ".join(c.id for c in r.channels) or "—",
                    r.source,
                    r.muted.reason if r.muted else "—",
                )
                for rule, r in zip(rules, data.results, strict=True)
            ),
        ),
    )


def register(budget_app: typer.Typer) -> None:
    budget_app.add_typer(rules_app, name="rules")
    budget_app.add_typer(rule_app, name="rule")
    budget_app.add_typer(threshold_app, name="threshold")
    budget_app.command(
        "restore-defaults",
        epilog=_help(
            "POST /api/v1/projects/{project_id}/budget/restore-defaults; GET /api/v1/projects/{project_id}/budget",
            schema="solwyn.budget.rules.v1",
        ),
    )(restore_defaults)
