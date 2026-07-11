"""Project key recovery commands."""

from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from solwyn_cli._vendor.validation import validate_project_key_format
from solwyn_cli._vendor.wire import ProjectKeyRotateRequest
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
from solwyn_cli.output import OneTimeSecretPanel, OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


app = typer.Typer(help="Recover from leaked or expired project keys.")


class _ProjectKeyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    key_prefix: str = Field(pattern=r"^sk_proj_[a-f0-9]{16}$")
    name: str = Field(min_length=1)
    last_used_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    rotation_grace_until: AwareDatetime | None
    created_at: AwareDatetime


class _ProjectKeyRotateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    key: SecretStr
    key_prefix: str = Field(pattern=r"^sk_proj_[a-f0-9]{16}$")
    rotation_grace_until: AwareDatetime

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        validate_project_key_format(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def validate_key_prefix(self) -> "_ProjectKeyRotateResponse":
        if self.key_prefix != self.key.get_secret_value()[:24]:
            raise ValueError("Invalid project key prefix")
        return self

    @field_serializer("key")
    def serialize_key(self, value: SecretStr) -> str:
        return value.get_secret_value()


class _ProjectKeyRevokeData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    key_id: str
    revoked: bool = True


def _date_time(value: AwareDatetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat().replace("+00:00", "Z")


def _metadata_table(data: _ProjectKeyMetadata) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("ID", data.id),
            ("Key prefix", data.key_prefix),
            ("Name", data.name),
            ("Last used", _date_time(data.last_used_at)),
            ("Expires", _date_time(data.expires_at)),
            ("Rotation grace until", _date_time(data.rotation_grace_until)),
            ("Created", _date_time(data.created_at)),
        ),
        right_align=("Value",),
    )


def _rotate_table(data: _ProjectKeyRotateResponse) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("ID", data.id),
            ("Key prefix", data.key_prefix),
            ("Rotation grace until", _date_time(data.rotation_grace_until)),
        ),
        right_align=("Value",),
    )


def _revoke_table(data: _ProjectKeyRevokeData) -> TableData:
    return TableData(
        columns=("Field", "Value"),
        rows=(
            ("Project", data.project_id),
            ("Key ID", data.key_id),
            ("Revoked", "true"),
        ),
        right_align=("Value",),
    )


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
            else "Provide a nonblank key identifier with --key-id."
        )
        exit_with_error(
            context,
            OutputError(
                code=f"{label}_required",
                message=f"A {label} is required.",
                hint=hint,
            ),
            semantic=ErrorSemantic.USAGE,
        )
    return value.strip()


def _jwt_credential(context: typer.Context, *, operation: str) -> JwtCredential:
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ProjectKeyCredential):
        hint = (
            f"Unset SOLWYN_API_KEY, run solwyn login, then retry keys {operation}."
            if credential.source == "environment"
            else (f"Run solwyn logout, then solwyn login, and retry keys {operation}.")
        )
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="User login is required to manage project keys.",
                hint=hint,
            ),
            semantic=ErrorSemantic.AUTH,
        )
    return credential


def _actionable_api_error(error: ApiError) -> ApiError:
    hints = {
        404: "Verify the project or key identifier and retry.",
        409: "A key rotation may already be in progress; inspect the active key before retrying.",
        410: "The project key is inactive; inspect current key metadata before retrying.",
        429: "Retry later after the project-key rate limit resets.",
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
            code="invalid_keys_response",
            message="The project key response was invalid.",
        ),
    )


@app.command("show")
def show_key(context: typer.Context) -> None:
    """Show active project-key metadata without revealing key material."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    credential = _jwt_credential(context, operation="show")
    path = quote(project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request("GET", f"/api/v1/projects/{path}/key")
        if response.status_code != 200:
            raise ValueError("Key metadata did not return 200")
        data = _ProjectKeyMetadata.model_validate(response.json())
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
        schema="solwyn.keys-show.v1",
        data=data,
        table=_metadata_table(data),
    )


@app.command("rotate")
def rotate_key(
    context: typer.Context,
    grace_seconds: Annotated[
        int | None,
        typer.Option(
            "--grace-seconds",
            help="Seconds the old key remains valid (0 through 604800).",
        ),
    ] = None,
) -> None:
    """Rotate the active key and reveal its replacement exactly once."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    try:
        request = ProjectKeyRotateRequest(grace_seconds=grace_seconds)
    except ValidationError:
        exit_with_error(
            context,
            OutputError(
                code="invalid_key_rotation_request",
                message="The key rotation request is invalid.",
                hint="Use --grace-seconds with a value from 0 through 604800.",
            ),
            semantic=ErrorSemantic.USAGE,
        )
    credential = _jwt_credential(context, operation="rotate")
    path = quote(project_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "POST",
                f"/api/v1/projects/{path}/key/rotate",
                json=request.model_dump(mode="json"),
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 200:
            raise ValueError("Key rotation did not return 200")
        data = _ProjectKeyRotateResponse.model_validate(response.json())
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
        schema="solwyn.keys-rotate.v1",
        data=data,
        table=_rotate_table(data),
        one_time_secret=OneTimeSecretPanel(
            title="One-time replacement project key",
            value=data.key,
        ),
    )


@app.command("revoke")
def revoke_key(
    context: typer.Context,
    key_id: Annotated[
        str,
        typer.Option("--key-id", help="Identifier of the key to hard-revoke."),
    ],
) -> None:
    """Hard-revoke a project key without minting a replacement."""
    runtime = command_context(context)
    project_id = _validated_identifier(context, runtime.project, label="project")
    validated_key_id = _validated_identifier(context, key_id, label="key_id")
    credential = _jwt_credential(context, operation="revoke")
    project_path = quote(project_id, safe="")
    key_path = quote(validated_key_id, safe="")
    try:
        with SolwynClient(api_url=runtime.api_url, credential=credential) as client:
            response = client.request(
                "DELETE",
                f"/api/v1/projects/{project_path}/key/{key_path}",
                retry_rate_limit=False,
                retry_auth=False,
            )
        if response.status_code != 204 or response.content:
            raise ValueError("Key revocation did not return a bodyless 204")
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

    data = _ProjectKeyRevokeData(
        project_id=project_id,
        key_id=validated_key_id,
    )
    output_engine(context).write_success(
        schema="solwyn.keys-revoke.v1",
        data=data,
        table=_revoke_table(data),
    )
