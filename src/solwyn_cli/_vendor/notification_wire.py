"""Request mirrors for notification and account channel routes."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NotificationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ChannelType(StrEnum):
    SLACK = "slack"
    PAGERDUTY = "pagerduty"
    TEAMS = "teams"
    DISCORD = "discord"
    TELEGRAM = "telegram"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    WARNING = "warning"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SlackConfig(NotificationWire):
    webhook_url: str | None = None
    bot_token: str | None = None
    channel: str | None = None


class PagerDutyConfig(NotificationWire):
    routing_key: str | None = None
    api_token: str | None = None


class TeamsConfig(NotificationWire):
    webhook_url: str | None = None


class DiscordConfig(NotificationWire):
    webhook_url: str | None = None


class TelegramConfig(NotificationWire):
    bot_token: str | None = None
    chat_id: str | None = None


class SmsConfigUpdate(NotificationWire):
    enabled: bool | None = None


class EmailConfigUpdate(NotificationWire):
    enabled: bool | None = None


class NotificationConfigUpdate(NotificationWire):
    channel_ids: list[str] | None = None
    sms_config: SmsConfigUpdate | None = None
    email_config: EmailConfigUpdate | None = None
    severity_threshold: Severity | None = None
    enabled_event_types: set[str] | None = None


def _channel_create_schema(schema: dict) -> None:
    schema["oneOf"] = [
        {
            "properties": {
                "channel_type": {"const": kind.value},
                "name": {"type": "string"},
                f"{kind.value}_config": schema["properties"][f"{kind.value}_config"][
                    "anyOf"
                ][0],
            },
            "required": ["channel_type", "name", f"{kind.value}_config"],
        }
        for kind in ChannelType
    ]


class NotificationChannelCreate(NotificationWire):
    # Keep Core's conditional required schema using Pydantic's own config refs.
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        json_schema_extra=_channel_create_schema,
    )
    channel_type: ChannelType
    name: str
    slack_config: SlackConfig | None = None
    pagerduty_config: PagerDutyConfig | None = None
    teams_config: TeamsConfig | None = None
    discord_config: DiscordConfig | None = None
    telegram_config: TelegramConfig | None = None


class NotificationChannelUpdate(NotificationWire):
    name: str | None = None
    slack_config: SlackConfig | None = None
    pagerduty_config: PagerDutyConfig | None = None
    teams_config: TeamsConfig | None = None
    discord_config: DiscordConfig | None = None
    telegram_config: TelegramConfig | None = None


class SlackTestRequest(NotificationWire):
    webhook_url: str | None = None


class PagerDutyTestRequest(NotificationWire):
    routing_key: str | None = None


class AlertEmailUpdate(NotificationWire):
    # Core's EmailStr remains the final validator; keep the declared email
    # format without adding an email-validator runtime dependency.
    override: str = Field(json_schema_extra={"format": "email"})


TestChannel = Literal["slack", "pagerduty", "sms", "email"]
