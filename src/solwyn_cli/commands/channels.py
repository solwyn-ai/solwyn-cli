"""Reusable account notification channels with safe secret inputs."""

import json
import os
import sys
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import ValidationError

from solwyn_cli._vendor.notification_wire import (
    ChannelType,
    NotificationChannelCreate,
    NotificationChannelUpdate,
)
from solwyn_cli.commands._notifications_common import (
    ChannelDeleted,
    ChannelList,
    DeliveryResponse,
    NotificationChannelResponse,
    SECRET_FIELDS,
    command_help,
    emit,
    emit_delivery,
    group_help,
    request,
    secret_input,
    usage,
)
from solwyn_cli.output import TableData

app = typer.Typer(
    help="Manage reusable account notification channels.", epilog=group_help("channels")
)
BASE = "/api/v1/account/notification-channels"
CONFIG_FIELDS = {
    "slack": {"webhook_url", "bot_token", "channel"},
    "pagerduty": {"routing_key", "api_token"},
    "teams": {"webhook_url"},
    "discord": {"webhook_url"},
    "telegram": {"bot_token", "chat_id"},
}
DEFAULT_SECRET = {
    "slack": "webhook_url",
    "pagerduty": "routing_key",
    "teams": "webhook_url",
    "discord": "webhook_url",
    "telegram": "bot_token",
}
SECRET_HELP = "Secrets only through --secret-stdin, --config-stdin JSON, or SOLWYN_CHANNEL_SECRET; never command arguments."


def path(channel_id: str) -> str:
    return BASE + "/" + quote(channel_id, safe="")


def channel_table(data: NotificationChannelResponse) -> TableData:
    rows = [
        ("ID", data.id),
        ("Account", data.account_id),
        ("Type", data.channel_type),
        ("Name", data.name),
    ]
    for kind in CONFIG_FIELDS:
        config = getattr(data, f"{kind}_config")
        if config is not None:
            rows.extend(
                (f"{kind}.{key}", value if value is not None else "—")
                for key, value in config.model_dump().items()
            )
    rows.extend(
        [
            ("Created", data.created_at.isoformat().replace("+00:00", "Z")),
            ("Updated", data.updated_at.isoformat().replace("+00:00", "Z")),
        ]
    )
    return TableData(columns=("Field", "Value"), rows=tuple(rows))


@app.command(
    "list", epilog=command_help("GET " + BASE, "solwyn.channels.list.v1", auth="JWT")
)
def list_channels(context: typer.Context) -> None:
    """List account channels with masked credentials."""
    data = request(context, "GET", BASE, ChannelList, list_response=True)
    table = TableData(
        columns=("ID", "Type", "Name"),
        rows=tuple((c.id, c.channel_type, c.name) for c in data.channels),
    )
    emit(context, "solwyn.channels.list.v1", data, table=table)


@app.command(
    "show",
    epilog=command_help(
        "GET " + BASE + "/{channel_id}", "solwyn.channels.v1", auth="JWT"
    ),
)
def show(context: typer.Context, channel_id: Annotated[str, typer.Argument()]) -> None:
    """Show a channel; secret fields retain only their final four characters."""
    data = request(context, "GET", path(channel_id), NotificationChannelResponse)
    emit(context, "solwyn.channels.v1", data, table=channel_table(data))


def config_input(
    context: typer.Context,
    *,
    kind: str,
    secret_stdin: bool,
    config_stdin: bool,
    secret_field: str | None,
    channel: str | None,
    chat_id: str | None,
    create: bool,
) -> tuple[dict, tuple[str, ...]]:
    if kind not in CONFIG_FIELDS:
        usage(context, "This channel type is not supported for configuration updates.")
    if config_stdin and (
        secret_stdin or secret_field or channel is not None or chat_id is not None
    ):
        usage(
            context,
            "--config-stdin cannot be combined with other configuration options.",
        )
    if config_stdin:
        try:
            config = json.loads(sys.stdin.read())
        except (ValueError, TypeError):
            usage(context, "Channel configuration stdin must contain a JSON object.")
        if not isinstance(config, dict):
            usage(context, "Channel configuration stdin must contain a JSON object.")
    else:
        config = {}
        secret = secret_input(context, stdin=secret_stdin, required=create)
        if secret is not None:
            config[secret_field or DEFAULT_SECRET[kind]] = secret
        elif secret_field:
            usage(context, "--secret-field requires a secret input.")
        if channel is not None:
            config["channel"] = channel
        if chat_id is not None:
            config["chat_id"] = chat_id
    if set(config) - CONFIG_FIELDS[kind] or any(
        value is not None and not isinstance(value, str) for value in config.values()
    ):
        usage(context, "Configuration fields do not match the channel type.")
    if secret_field is not None and secret_field not in SECRET_FIELDS:
        usage(context, "--secret-field must name a secret field for this channel type.")
    secrets = tuple(
        v for k, v in config.items() if k in SECRET_FIELDS and isinstance(v, str) and v
    )
    if create:
        if any(v.startswith("****") for v in secrets):
            usage(
                context,
                "Masked credentials cannot create a channel; supply the original secret.",
            )

        def usable(field: str) -> bool:
            return bool(config.get(field) and config[field].strip())

        if kind == "slack":
            valid = usable("webhook_url") or (usable("bot_token") and usable("channel"))
        elif kind == "telegram":
            valid = usable("bot_token") and usable("chat_id")
        else:
            valid = usable(DEFAULT_SECRET[kind])
        if not valid:
            usage(
                context,
                "Channel configuration is missing required credentials or destination fields.",
            )
    return config, secrets


@app.command(
    "create",
    epilog=command_help(
        "POST " + BASE,
        "solwyn.channels.v1",
        guidance=(
            SECRET_HELP,
            "Slack requires webhook_url or bot_token + channel; PagerDuty routing_key; Teams/Discord webhook_url; Telegram bot_token + chat_id. All types require Production+.",
        ),
    ),
)
def create(
    context: typer.Context,
    channel_type: Annotated[ChannelType, typer.Option("--type")],
    name: Annotated[str, typer.Option("--name")],
    secret_stdin: Annotated[bool, typer.Option("--secret-stdin")] = False,
    config_stdin: Annotated[
        bool,
        typer.Option(
            "--config-stdin",
            help="Read the type-specific config JSON object from stdin, including any optional secrets.",
        ),
    ] = False,
    secret_field: Annotated[
        str | None,
        typer.Option(
            "--secret-field",
            help="Secret field name only; defaults to the type's primary secret.",
        ),
    ] = None,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Slack destination ID for bot-token mode."),
    ] = None,
    chat_id: Annotated[
        str | None, typer.Option("--chat-id", help="Telegram destination ID.")
    ] = None,
) -> None:
    """Create an owner-managed channel using stdin or environment credentials."""
    if not name.strip():
        usage(context, "Channel name must not be blank.")
    config, secrets = config_input(
        context,
        kind=channel_type.value,
        secret_stdin=secret_stdin,
        config_stdin=config_stdin,
        secret_field=secret_field,
        channel=channel,
        chat_id=chat_id,
        create=True,
    )
    body = NotificationChannelCreate.model_validate(
        {
            "channel_type": channel_type,
            "name": name,
            f"{channel_type.value}_config": config,
        }
    )
    data = request(
        context, "POST", BASE, NotificationChannelResponse, body=body, secrets=secrets
    )
    emit(context, "solwyn.channels.v1", data, table=channel_table(data))


@app.command(
    "update",
    epilog=command_help(
        "GET then PUT " + BASE + "/{channel_id}",
        "solwyn.channels.v1",
        guidance=(
            SECRET_HELP,
            "Omitted values stay unchanged. Masked secrets retain stored credentials. --clear-config sends null; JSON null clears individual config fields. Type is immutable.",
        ),
    ),
)
def update(
    context: typer.Context,
    channel_id: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Option("--name")] = None,
    secret_stdin: Annotated[bool, typer.Option("--secret-stdin")] = False,
    config_stdin: Annotated[bool, typer.Option("--config-stdin")] = False,
    secret_field: Annotated[str | None, typer.Option("--secret-field")] = None,
    channel: Annotated[str | None, typer.Option("--channel")] = None,
    chat_id: Annotated[str | None, typer.Option("--chat-id")] = None,
    clear_config: Annotated[
        bool,
        typer.Option(
            "--clear-config",
            help="Clear this channel type's stored credentials and destination.",
        ),
    ] = False,
) -> None:
    """Update selected fields; Core retains masked secret values on write-back."""
    configuring = (
        secret_stdin
        or config_stdin
        or secret_field is not None
        or channel is not None
        or chat_id is not None
        or os.environ.get("SOLWYN_CHANNEL_SECRET") is not None
    )
    if clear_config and configuring:
        usage(
            context,
            "--clear-config cannot be combined with configuration inputs; unset SOLWYN_CHANNEL_SECRET.",
        )
    if name is not None and not name.strip():
        usage(context, "Channel name must not be blank.")
    if name is None and not configuring and not clear_config:
        usage(context, "Choose at least one field to update.")
    values = {"name": name} if name is not None else {}
    secrets = ()
    if configuring or clear_config:
        current = request(context, "GET", path(channel_id), NotificationChannelResponse)
        if clear_config:
            if current.channel_type not in CONFIG_FIELDS:
                usage(
                    context,
                    "This channel type is not supported for configuration updates.",
                )
            values[f"{current.channel_type}_config"] = None
        else:
            config, secrets = config_input(
                context,
                kind=current.channel_type,
                secret_stdin=secret_stdin,
                config_stdin=config_stdin,
                secret_field=secret_field,
                channel=channel,
                chat_id=chat_id,
                create=False,
            )
            values[f"{current.channel_type}_config"] = config
    try:
        body = NotificationChannelUpdate.model_validate(values)
    except ValidationError:
        usage(context, "Invalid channel update configuration.")
    data = request(
        context,
        "PUT",
        path(channel_id),
        NotificationChannelResponse,
        body=body,
        secrets=secrets,
    )
    emit(context, "solwyn.channels.v1", data, table=channel_table(data))


@app.command(
    "test",
    epilog=command_help(
        "POST " + BASE + "/{channel_id}/test",
        "solwyn.channels.test.v1",
        guidance=(
            "Sends a real test using the channel's stored credentials. In-band success:false exits 1.",
        ),
    ),
)
def test_channel(
    context: typer.Context, channel_id: Annotated[str, typer.Argument()]
) -> None:
    """Send a test through a stored channel (owner only)."""
    data = request(context, "POST", path(channel_id) + "/test", DeliveryResponse)
    emit_delivery(context, "solwyn.channels.test.v1", data)


@app.command(
    "delete",
    epilog=command_help(
        "DELETE " + BASE + "/{channel_id}",
        "solwyn.channels.delete.v1",
        guidance=(
            "Core removes the channel ID from project notification settings and every budget rule's channel_ids.",
        ),
    ),
)
def delete(
    context: typer.Context, channel_id: Annotated[str, typer.Argument()]
) -> None:
    """Delete a channel and remove its project and rule routing references."""
    data = request(
        context, "DELETE", path(channel_id), ChannelDeleted, deleted_id=channel_id
    )
    emit(context, "solwyn.channels.delete.v1", data)
