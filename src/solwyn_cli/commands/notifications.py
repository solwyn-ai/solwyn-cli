"""Project notification routing settings and delivery tests."""

from enum import StrEnum
from typing import Annotated

import typer

from solwyn_cli._vendor.notification_wire import (
    NotificationConfigUpdate,
    PagerDutyTestRequest,
    Severity,
    SlackTestRequest,
)
from solwyn_cli.commands._notifications_common import (
    DeliveryResponse,
    EmailTestResponse,
    NotificationConfigResponse,
    command_help,
    emit,
    emit_delivery,
    group_help,
    project_path,
    request,
    secret_input,
    usage,
)
from solwyn_cli.output import TableData

app = typer.Typer(
    help="Manage project notification routing.", epilog=group_help("notifications")
)
SCHEMA = "solwyn.notifications.v1"
TEST_SCHEMA = "solwyn.notifications.test.v1"


class TestChannel(StrEnum):
    SLACK = "slack"
    PAGERDUTY = "pagerduty"
    SMS = "sms"
    EMAIL = "email"


def config_table(data: NotificationConfigResponse) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Channels", ", ".join(data.channel_ids) or "—"),
            ("Severity", data.severity_threshold.value),
            ("Events", ", ".join(sorted(data.enabled_event_types)) or "all events"),
            ("Email", str(data.email.enabled).lower()),
            ("SMS", str(data.sms_config.enabled if data.sms_config else False).lower()),
            ("Available channels", ", ".join(data.capabilities.available_channels)),
            ("SMS allowed events", ", ".join(data.capabilities.sms_allowed_events)),
            ("Email allowed events", ", ".join(data.capabilities.email_allowed_events)),
        ),
    )


@app.command(
    "show",
    epilog=command_help(
        "GET /api/v1/projects/{project}/notifications", SCHEMA, auth="any"
    ),
)
def show(context: typer.Context) -> None:
    """Show routing, delivery preferences and tier capabilities."""
    data = request(
        context, "GET", project_path(context), NotificationConfigResponse, jwt=False
    )
    emit(context, SCHEMA, data, table=config_table(data))


@app.command(
    "set",
    epilog=command_help(
        "PUT /api/v1/projects/{project}/notifications",
        SCHEMA,
        auth="any; full-scope key for writes",
        guidance=(
            "Omitted options leave settings unchanged. Clear channel/event lists with []; reset email/SMS configs with null.",
            "Channel enablement follows Core capabilities; SMS requires Production, verified phone and consent.",
        ),
    ),
)
def set_config(
    context: typer.Context,
    channel: Annotated[
        list[str] | None,
        typer.Option(
            "--channel", help="Channel ID; repeat to replace the reference list."
        ),
    ] = None,
    clear_channels: Annotated[
        bool, typer.Option("--clear-channels", help="Remove every channel reference.")
    ] = False,
    severity: Annotated[Severity | None, typer.Option("--severity")] = None,
    event: Annotated[
        list[str] | None,
        typer.Option(
            "--event", help="Event type; repeat. Core accepts future event names."
        ),
    ] = None,
    all_events: Annotated[
        bool, typer.Option("--all-events", help="Allow all event types.")
    ] = False,
    email: Annotated[bool | None, typer.Option("--email/--no-email")] = None,
    sms: Annotated[bool | None, typer.Option("--sms/--no-sms")] = None,
    reset_email: Annotated[
        bool, typer.Option("--reset-email", help="Reset email to its enabled default.")
    ] = False,
    reset_sms: Annotated[
        bool, typer.Option("--reset-sms", help="Reset SMS to its disabled default.")
    ] = False,
) -> None:
    """Change only the notification settings explicitly selected."""
    if (
        (channel and clear_channels)
        or (event and all_events)
        or (email is not None and reset_email)
        or (sms is not None and reset_sms)
    ):
        usage(context, "A setting and its clear/reset option cannot be combined.")
    body = {}
    if channel is not None or clear_channels:
        body["channel_ids"] = [] if clear_channels else channel
    if event is not None or all_events:
        body["enabled_event_types"] = [] if all_events else event
    if severity is not None:
        body["severity_threshold"] = severity
    if email is not None or reset_email:
        body["email_config"] = None if reset_email else {"enabled": email}
    if sms is not None or reset_sms:
        body["sms_config"] = None if reset_sms else {"enabled": sms}
    if not body:
        usage(context, "Choose at least one setting to update.")
    data = request(
        context,
        "PUT",
        project_path(context),
        NotificationConfigResponse,
        body=NotificationConfigUpdate.model_validate(body),
        jwt=False,
    )
    emit(context, SCHEMA, data, table=config_table(data))


@app.command(
    "test",
    epilog=command_help(
        "POST /api/v1/projects/{project}/notifications/test-{channel}",
        TEST_SCHEMA,
        auth="any; Slack/PagerDuty require JWT or full-scope key; email requires JWT",
        guidance=(
            "SMS checks eligibility only (no message sent). Slack, PagerDuty and email send a real test. Slack/PagerDuty require --secret-stdin or SOLWYN_CHANNEL_SECRET; use channels test ID for stored credentials.",
            "Core enforces per-channel capabilities; SMS is Production+ and requires phone verification and consent.",
        ),
    ),
)
def test_notification(
    context: typer.Context,
    channel: Annotated[TestChannel, typer.Argument()],
    secret_stdin: Annotated[
        bool,
        typer.Option(
            "--secret-stdin",
            help="Read Slack webhook URL or PagerDuty routing key from stdin.",
        ),
    ] = False,
) -> None:
    """Test delivery or SMS eligibility; failure exits 1, including HTTP 200 failures."""
    secret = None
    body = None
    if channel in (TestChannel.SLACK, TestChannel.PAGERDUTY):
        secret = secret_input(context, stdin=secret_stdin, required=True)
        body = (
            SlackTestRequest(webhook_url=secret)
            if channel == TestChannel.SLACK
            else PagerDutyTestRequest(routing_key=secret)
        )
    elif secret_stdin:
        usage(
            context,
            "Email and SMS tests use stored account settings; no secret input is needed.",
        )
    data = request(
        context,
        "POST",
        project_path(context) + "/test-" + channel.value,
        EmailTestResponse if channel == TestChannel.EMAIL else DeliveryResponse,
        body=body,
        jwt=channel == TestChannel.EMAIL,
        secrets=(secret,) if secret else (),
    )
    emit_delivery(context, TEST_SCHEMA, data)
