"""JWT-authenticated webhook target commands."""

import re
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from solwyn_cli._vendor.wire import WebhookCreateRequest
from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OneTimeSecretPanel, OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


_DELIVERY_GUIDANCE = (
    "Verify X-Solwyn-Signature: sha256=<hex HMAC-SHA256 of the raw body>.",
    "Use Idempotency-Key to deduplicate retries.",
    "No timestamp header or replay window is defined.",
)
_SIGNING_SECRET_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PREFIX_PATTERN = r"^[0-9a-f]{8}\.\.\.$"

app = typer.Typer(
    help="Manage project webhook targets.",
    epilog=help_contract(
        endpoint="composite router for webhook target subcommands",
        auth="JWT",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
        guidance=(
            "A signing secret is shown exactly once on create or rotate.",
            "If the signing secret is lost, rotate the webhook.",
            *_DELIVERY_GUIDANCE,
        ),
    ),
)


class WebhookResponse(BaseModel):
    """Secret-masked webhook metadata returned by list and create."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    events: str
    is_active: bool
    secret_prefix: str = Field(pattern=_SECRET_PREFIX_PATTERN)
    created_at: AwareDatetime


class WebhookCreateResponse(WebhookResponse):
    """Created webhook with its one-time signing secret."""

    signing_secret: SecretStr

    @field_validator("signing_secret")
    @classmethod
    def validate_signing_secret(cls, value: SecretStr) -> SecretStr:
        if _SIGNING_SECRET_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("Invalid webhook signing secret")
        return value

    @model_validator(mode="after")
    def validate_secret_prefix(self) -> "WebhookCreateResponse":
        if self.secret_prefix != self.signing_secret.get_secret_value()[:8] + "...":
            raise ValueError("Webhook signing secret prefix does not match")
        return self

    @field_serializer("signing_secret")
    def serialize_signing_secret(self, value: SecretStr) -> str:
        return value.get_secret_value()


class WebhookRotateResponse(BaseModel):
    """Rotated webhook key with its one-time signing secret."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    key_version: int = Field(ge=1)
    signing_secret: SecretStr
    secret_prefix: str = Field(pattern=_SECRET_PREFIX_PATTERN)

    @field_validator("signing_secret")
    @classmethod
    def validate_signing_secret(cls, value: SecretStr) -> SecretStr:
        if _SIGNING_SECRET_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("Invalid webhook signing secret")
        return value

    @model_validator(mode="after")
    def validate_secret_prefix(self) -> "WebhookRotateResponse":
        if self.secret_prefix != self.signing_secret.get_secret_value()[:8] + "...":
            raise ValueError("Webhook signing secret prefix does not match")
        return self

    @field_serializer("signing_secret")
    def serialize_signing_secret(self, value: SecretStr) -> str:
        return value.get_secret_value()


class _WebhookListData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    webhooks: list[WebhookResponse]


class _WebhookDeleteData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    webhook_id: str
    deleted: bool = True


_WEBHOOK_LIST_ADAPTER = TypeAdapter(list[WebhookResponse])


def _date_time(value: AwareDatetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _validated_identifier(
    context: typer.Context,
    value: str | None,
    *,
    label: str,
) -> str:
    if value is None or not value.strip():
        hint = (
            "Choose a project with --project, SOLWYN_PROJECT_ID, or "
            "config default_project."
            if label == "project"
            else "Provide a nonblank webhook identifier."
        )
        exit_with_error(
            context,
            OutputError(
                code=f"{label}_required",
                message=f"A {label.replace('_', ' ')} is required.",
                hint=hint,
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return value.strip()


def _jwt_credential(context: typer.Context, *, operation: str) -> JwtCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            f"Unset SOLWYN_API_KEY, run solwyn login, then retry webhooks {operation}."
            if credential.source == "environment"
            else (
                f"Run solwyn logout, then solwyn login, and retry webhooks {operation}."
            )
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to manage webhook targets.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def _actionable_api_error(error: ApiError) -> ApiError:
    hints = {
        404: "Verify the project or webhook identifier and retry.",
        410: "Create a new webhook target; a deactivated target cannot be rotated.",
    }
    hint = error.hint or hints.get(error.status_code)
    if hint == error.hint:
        return error
    return ApiError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        hint=hint,
        detail=error.detail,
    )


def _invalid_response(context: typer.Context) -> None:
    exit_with_error(
        context,
        OutputError(
            code="invalid_webhooks_response",
            message="The webhook response was invalid.",
        ),
    )


def _validate_list_response(payload: object) -> list[WebhookResponse]:
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) or "signing_secret" in item for item in payload
    ):
        raise ValueError("Invalid webhook list response")
    return _WEBHOOK_LIST_ADAPTER.validate_python(payload)


def _list_table(data: _WebhookListData) -> TableData:
    return TableData(
        columns=("ID", "URL", "Events", "Secret prefix"),
        rows=tuple(
            (webhook.id, webhook.url, webhook.events, webhook.secret_prefix)
            for webhook in data.webhooks
        ),
    )


def _create_table(data: WebhookCreateResponse) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("ID", data.id),
            ("URL", data.url),
            ("Events", data.events),
            ("Active", str(data.is_active).lower()),
            ("Secret prefix", data.secret_prefix),
            ("Created", _date_time(data.created_at)),
        ),
        right_align=("Value",),
    )


def _rotate_table(data: WebhookRotateResponse) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("ID", data.id),
            ("Key version", str(data.key_version)),
            ("Secret prefix", data.secret_prefix),
        ),
        right_align=("Value",),
    )


def _delete_table(data: _WebhookDeleteData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Webhook ID", data.webhook_id),
            ("Deleted", "true"),
        ),
        right_align=("Value",),
    )


@app.command(
    "list",
    epilog=help_contract(
        endpoint="GET /api/v1/projects/{project}/webhooks",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.webhooks-list.v1",
        guidance=(
            "Lists secret_prefix only; it never returns a signing secret.",
            "If the signing secret is lost, rotate the webhook.",
        ),
    ),
)
def list_webhooks(context: typer.Context) -> None:
    """List active webhook targets with masked secret prefixes."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    credential = _jwt_credential(context, operation="list")
    project_path = quote(project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "GET", f"/api/v1/projects/{project_path}/webhooks"
            )
        if response.status_code != 200:
            raise ValueError("Webhook list did not return 200")
        data = _WebhookListData(webhooks=_validate_list_response(response.json()))
    except ApiError as error:
        exit_with_error(context, _actionable_api_error(error))
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    output_engine(context).write_success(
        schema="solwyn.webhooks-list.v1",
        data=data,
        table=_list_table(data),
    )


@app.command(
    "create",
    epilog=help_contract(
        endpoint="POST /api/v1/projects/{project}/webhooks",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.webhooks-create.v1",
        guidance=(
            "The signing secret is shown exactly once; store it securely.",
            *_DELIVERY_GUIDANCE,
        ),
    ),
)
def create_webhook(
    context: typer.Context,
    url: Annotated[
        str,
        typer.Option("--url", help="Destination URI; Core requires public HTTPS."),
    ],
    events: Annotated[
        str,
        typer.Option(
            "--events",
            help="Comma-separated event names, or * for all events.",
        ),
    ] = "*",
) -> None:
    """Create a webhook target and reveal its signing secret once."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    try:
        request = WebhookCreateRequest(url=url, events=events)
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_webhook_create_request",
                message="The webhook create request is invalid.",
                hint="Provide a valid destination URI with --url.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _jwt_credential(context, operation="create")
    project_path = quote(project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                f"/api/v1/projects/{project_path}/webhooks",
                json=request.model_dump(mode="json"),
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 201:
            raise ValueError("Webhook creation did not return 201")
        data = WebhookCreateResponse.model_validate(response.json())
    except ApiError as error:
        exit_with_error(context, _actionable_api_error(error))
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    output_engine(context).write_success(
        schema="solwyn.webhooks-create.v1",
        data=data,
        table=_create_table(data),
        one_time_secret=OneTimeSecretPanel(
            title="One-time webhook signing secret",
            value=data.signing_secret,
        ),
    )


@app.command(
    "rotate",
    epilog=help_contract(
        endpoint="POST /api/v1/projects/{project}/webhooks/{webhook_id}/rotate",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.webhooks-rotate.v1",
        guidance=(
            "The signing secret is shown exactly once; the old secret stops working immediately.",
            *_DELIVERY_GUIDANCE,
        ),
    ),
)
def rotate_webhook(
    context: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="Webhook target identifier.")],
) -> None:
    """Rotate a target's signing secret and reveal the replacement once."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    validated_webhook_id = _validated_identifier(
        context, webhook_id, label="webhook_id"
    )
    credential = _jwt_credential(context, operation="rotate")
    project_path = quote(project_id, safe="")
    webhook_path = quote(validated_webhook_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                f"/api/v1/projects/{project_path}/webhooks/{webhook_path}/rotate",
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 200:
            raise ValueError("Webhook rotation did not return 200")
        data = WebhookRotateResponse.model_validate(response.json())
    except ApiError as error:
        exit_with_error(context, _actionable_api_error(error))
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    output_engine(context).write_success(
        schema="solwyn.webhooks-rotate.v1",
        data=data,
        table=_rotate_table(data),
        one_time_secret=OneTimeSecretPanel(
            title="One-time webhook signing secret",
            value=data.signing_secret,
        ),
    )


@app.command(
    "delete",
    epilog=help_contract(
        endpoint="DELETE /api/v1/projects/{project}/webhooks/{webhook_id}",
        auth="JWT",
        exits="0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        schema="solwyn.webhooks-delete.v1 (CLI output after bodyless 204)",
        guidance=("Deactivates the webhook target and returns no API body.",),
    ),
)
def delete_webhook(
    context: typer.Context,
    webhook_id: Annotated[str, typer.Argument(help="Webhook target identifier.")],
) -> None:
    """Deactivate a webhook target."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    validated_webhook_id = _validated_identifier(
        context, webhook_id, label="webhook_id"
    )
    credential = _jwt_credential(context, operation="delete")
    project_path = quote(project_id, safe="")
    webhook_path = quote(validated_webhook_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "DELETE",
                f"/api/v1/projects/{project_path}/webhooks/{webhook_path}",
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 204 or response.content:
            raise ValueError("Webhook deletion did not return a bodyless 204")
    except ApiError as error:
        exit_with_error(context, _actionable_api_error(error))
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        _invalid_response(context)

    data = _WebhookDeleteData(
        project_id=project_id,
        webhook_id=validated_webhook_id,
    )
    output_engine(context).write_success(
        schema="solwyn.webhooks-delete.v1",
        data=data,
        table=_delete_table(data),
    )
