"""Strict account alert reads shared by CLI and MCP; no alert mutations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from solwyn_cli import _mcp_operation as operation
from solwyn_cli.auth import JwtRequiredError, JwtCredential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands.alerts import (
    AccountAlertResponse,
    AlertEventEntry,
    AlertEventListResponse,
    AlertEventWindow,
    AlertQueryError,
)

AlertRange = Literal["7d", "30d", "90d"]
ActivityType = Literal["fired", "snoozed", "unsnoozed", "paused", "resumed"]


class AlertOpenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[AlertEventEntry]
    next_cursor: str | None


class AlertRuleLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_key: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(max_length=36)
    rule_type: str = Field(min_length=1, max_length=32)
    percent: int | None = None
    period_keys: list[str] | None = Field(default=None, max_length=2)


class AlertLatestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[AlertRuleLookup] = Field(max_length=100)


class AlertLatestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[AlertEventEntry]
    window: AlertEventWindow


class RuleLatest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_fired: AlertEventEntry | None
    latest_event: AlertEventEntry | None


class AccountAlertWithLatestResponse(AccountAlertResponse):
    last_fired: AlertEventEntry | None
    latest_event: AlertEventEntry | None
    latest_window: AlertEventWindow | None


def require_alert_jwt(client: SolwynClient) -> None:
    if not isinstance(client.current_credential, JwtCredential):
        raise JwtRequiredError(
            "Account alert reads and latest firing require JWT. Run solwyn login."
        )


def activity_params(time_range: str, event_type: str | None) -> dict[str, str]:
    if time_range not in {"7d", "30d", "90d"}:
        raise AlertQueryError("--range must be 7d, 30d, or 90d.")
    if event_type is not None and event_type not in {
        "fired",
        "snoozed",
        "unsnoozed",
        "paused",
        "resumed",
    }:
        raise AlertQueryError("Invalid alert activity event type.")
    return {
        "range": time_range,
        **({"event_type": event_type} if event_type is not None else {}),
    }


def request_account_alert_page(
    *,
    client: SolwynClient,
    pending: bool = False,
    time_range: str = "30d",
    event_type: str | None = None,
    cursor: str | None = None,
    all_pages: bool = False,
) -> AlertEventListResponse | AlertOpenResponse:
    params = {} if pending else activity_params(time_range, event_type)
    require_alert_jwt(client)
    path = "/api/v1/account/alerts/" + ("open-deliveries" if pending else "activity")
    model = AlertOpenResponse if pending else AlertEventListResponse
    seen: set[str] = set()
    events: list[AlertEventEntry] = []
    window = None
    while True:
        operation.page(cursor)
        if cursor is not None:
            if cursor in seen:
                raise ValueError("Alert pagination repeated a cursor.")
            seen.add(cursor)
        query = params | ({"cursor": cursor} if cursor is not None else {})
        page = model.model_validate(client.request("GET", path, params=query).json())
        events.extend(page.events)
        cursor = page.next_cursor
        values = dict(events=events, next_cursor=cursor)
        if isinstance(page, AlertEventListResponse):
            if window is None:
                window = page.window
            else:
                window.retention_clamped |= page.window.retention_clamped
            values["window"] = window
        data = model.model_validate(values)
        operation.retain(data, cursor=cursor)
        if not all_pages or cursor is None:
            return data


def _matches(event: AlertEventEntry, rule: AlertRuleLookup) -> bool:
    return event.project_id == rule.project_id and (
        event.rule_key == rule.rule_key
        or (
            rule.project_id is not None
            and rule.rule_type == "budget_threshold"
            and rule.percent is not None
            and event.rule_type == "budget_threshold"
            and event.metadata_json.get("percent") == rule.percent
        )
    )


def request_latest_events(
    *, client: SolwynClient, rules: list[AlertRuleLookup], account_id: str | None = None
) -> tuple[list[RuleLatest], AlertEventWindow | None]:
    require_alert_jwt(client)
    results = []
    window = None
    for offset in range(0, len(rules), 100):
        chunk = rules[offset : offset + 100]
        request = AlertLatestRequest(rules=chunk)
        page = AlertLatestResponse.model_validate(
            client.request(
                "POST",
                "/api/v1/account/alerts/latest-events",
                json=request.model_dump(mode="json"),
            ).json()
        )
        window = page.window
        if account_id is not None and any(
            event.account_id != account_id for event in page.events
        ):
            raise ValueError("Latest alert event account did not match.")
        if any(
            not any(_matches(event, rule) for rule in chunk) for event in page.events
        ):
            raise ValueError("Latest alert event did not match any selector.")
        for rule in chunk:
            matches = [event for event in page.events if _matches(event, rule)]
            fires = [
                event
                for event in matches
                if event.event_type == "fired"
                and (rule.period_keys is None or event.period_key in rule.period_keys)
            ]

            def key(event):
                return event.emitted_at, event.id

            results.append(
                RuleLatest(
                    last_fired=max(fires, key=key) if fires else None,
                    latest_event=max(matches, key=key)
                    if rule.project_id is None and matches
                    else None,
                )
            )
    return results, window


def request_account_alert(
    *, client: SolwynClient, with_last_fired: bool = False
) -> AccountAlertResponse | AccountAlertWithLatestResponse:
    require_alert_jwt(client)
    data = AccountAlertResponse.model_validate(
        client.request("GET", "/api/v1/account/alerts").json()
    )
    if not with_last_fired:
        return data
    latest, window = request_latest_events(
        client=client,
        account_id=data.account_id,
        rules=[
            AlertRuleLookup(
                rule_key=rule_key,
                project_id=None,
                rule_type="account_spend",
            )
            for rule_key in (data.account_id, "account_spend")
        ],
    )
    merged = {}
    for field in ("last_fired", "latest_event"):
        events = [event for result in latest if (event := getattr(result, field))]
        merged[field] = (
            max(events, key=lambda event: (event.emitted_at, event.id))
            if events
            else None
        )
    return AccountAlertWithLatestResponse(
        **data.model_dump(), **merged, latest_window=window
    )
