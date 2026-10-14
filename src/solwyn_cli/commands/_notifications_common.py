"""Shared notification transport, secret inputs and strict read models."""

import os
import sys
from typing import Any, TypeVar
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from solwyn_cli._vendor.notification_wire import (
    DiscordConfig,
    PagerDutyConfig,
    SlackConfig,
    TeamsConfig,
    TelegramConfig,
    Severity,
)
from solwyn_cli.auth import AuthError, ProjectKeyCredential, resolve_credential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

EXITS = "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found"
SECRET_FIELDS = {"webhook_url", "bot_token", "routing_key", "api_token"}
T = TypeVar("T", bound=BaseModel)


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def mask_secret(value: str) -> str:
    if value.startswith("****"):
        return value
    return "****" + (value[-4:] if len(value) > 4 else "")


class NotificationChannelResponse(ReadModel):
    id: str
    account_id: str
    channel_type: str
    name: str
    slack_config: SlackConfig | None = None
    pagerduty_config: PagerDutyConfig | None = None
    teams_config: TeamsConfig | None = None
    discord_config: DiscordConfig | None = None
    telegram_config: TelegramConfig | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="before")
    @classmethod
    def mask_config_secrets(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for key, config in result.items():
            if key.endswith("_config") and isinstance(config, dict):
                result[key] = {
                    k: mask_secret(v)
                    if k in SECRET_FIELDS and isinstance(v, str)
                    else v
                    for k, v in config.items()
                }
        return result


class ChannelList(ReadModel):
    channels: list[NotificationChannelResponse]


class SmsConfig(ReadModel):
    enabled: bool = False


class EmailConfig(ReadModel):
    enabled: bool = True


class NotificationCapabilitiesResponse(ReadModel):
    available_channels: list[str]
    sms_allowed_events: list[str]
    email_allowed_events: list[str]


class NotificationConfigResponse(ReadModel):
    channel_ids: list[str] = Field(default_factory=list)
    channels: list[NotificationChannelResponse] = Field(default_factory=list)
    sms_config: SmsConfig | None = None
    email: EmailConfig
    severity_threshold: Severity = Severity.WARNING
    enabled_event_types: list[str] = Field(default_factory=list)
    capabilities: NotificationCapabilitiesResponse


class DeliveryResponse(ReadModel):
    success: bool
    message: str


class EmailTestResponse(ReadModel):
    delivered: bool


class AlertEmailResponse(ReadModel):
    effective_email: str
    override: str | None = None


class ChannelDeleted(ReadModel):
    id: str
    deleted: bool = True


def command_help(
    endpoint: str,
    schema: str,
    *,
    auth: str = "JWT; owner for mutations",
    guidance: tuple[str, ...] = (),
) -> str:
    return help_contract(
        endpoint=endpoint, auth=auth, exits=EXITS, schema=schema, guidance=guidance
    )


def group_help(area: str) -> str:
    return help_contract(
        endpoint=f"composite router for {area} subcommands",
        auth="varies by subcommand",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    )


def usage(context: typer.Context, message: str) -> None:
    exit_with_error(
        context,
        OutputError(code="invalid_notification_request", message=message),
        semantic=ErrorSemantic.USAGE,
    )


def secret_input(context: typer.Context, *, stdin: bool, required: bool) -> str | None:
    # A flag opts into stdin, so interactive invocations never block unexpectedly.
    value = (
        sys.stdin.read().rstrip("\r\n")
        if stdin
        else os.environ.get("SOLWYN_CHANNEL_SECRET")
    )
    if value is not None and not value.strip():
        usage(context, "Channel secret must not be empty.")
    if required and value is None:
        usage(
            context, "Supply a secret through --secret-stdin or SOLWYN_CHANNEL_SECRET."
        )
    return value


def project_path(context: typer.Context) -> str:
    project = command_context(context).project
    if project is None:
        usage(context, "Select a project with --project or SOLWYN_PROJECT_ID.")
    return f"/api/v1/projects/{quote(project, safe='')}/notifications"


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if not secret.startswith("****"):
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def request(
    context: typer.Context,
    method: str,
    path: str,
    model: type[T],
    *,
    body: BaseModel | None = None,
    jwt: bool = True,
    secrets: tuple[str, ...] = (),
    list_response: bool = False,
    deleted_id: str | None = None,
) -> T:
    credential = required_credential(context, resolve=resolve_credential)
    if jwt and isinstance(credential, ProjectKeyCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="This operation requires user login.",
                hint="Unset SOLWYN_API_KEY and run solwyn login; mutations require the account owner.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    try:
        options: dict[str, Any] = {}
        if body is not None:
            options["json"] = body.model_dump(mode="json", exclude_unset=True)
            if (
                "enabled_event_types" in options["json"]
                and options["json"]["enabled_event_types"] is not None
            ):
                options["json"]["enabled_event_types"] = sorted(
                    options["json"]["enabled_event_types"]
                )
        # Preserve the client's bounded 429 and JWT-refresh defaults. Core
        # rejects these requests before entering the mutation/delivery handler.
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            response = client.request(method, path, **options)
        if deleted_id is not None:
            if response.status_code != 204 or response.content:
                raise ValueError("Expected bodyless delete response")
            payload = {"id": deleted_id, "deleted": True}
        else:
            payload = response.json()
        if list_response:
            payload = {"channels": payload}
        # Do not redact channel credential fields before their last-four masking;
        # mask them by parsing, then remove any server echo in other strings.
        parsed = model.model_validate(payload)
        return model.model_validate(redact(parsed.model_dump(mode="json"), secrets))
    except ApiError as error:
        exit_with_error(
            context,
            ApiError(
                status_code=error.status_code,
                code=redact(error.code, secrets),
                message=redact(error.message, secrets),
                hint=redact(error.hint, secrets),
                detail=redact(error.detail, secrets),
            ),
        )
    except AuthError:
        exit_with_error(
            context,
            OutputError(
                code="authentication_failed",
                message="Authentication failed. Run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_notifications_response",
                message="The notification response was invalid.",
            ),
        )
    raise AssertionError("exit_with_error must exit")


def fields_table(data: BaseModel) -> TableData:
    rows = []
    for field, value in data.model_dump(mode="json").items():
        if value is None:
            rendered = "—"
        elif isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, (list, dict)):
            rendered = str(value)
        else:
            rendered = str(value)
        rows.append((field.replace("_", " ").capitalize(), rendered))
    return TableData(columns=("Field", "Value"), rows=tuple(rows))


def emit(
    context: typer.Context,
    schema: str,
    data: BaseModel,
    *,
    table: TableData | None = None,
) -> None:
    output_engine(context).write_success(
        schema=schema, data=data, table=table or fields_table(data)
    )


def emit_delivery(
    context: typer.Context, schema: str, data: DeliveryResponse | EmailTestResponse
) -> None:
    emit(context, schema, data)
    if not (data.delivered if isinstance(data, EmailTestResponse) else data.success):
        raise typer.Exit(code=1)
