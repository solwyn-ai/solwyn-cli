"""RFC 8628 device authorization, token polling and RFC 7009 revocation."""

from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from solwyn_cli import __version__
from solwyn_cli.auth import (
    CLI_TOKEN_PATTERN,
    DEVICE_CODE_PATTERN,
    OAUTH_CLIENT_ID,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import safe_absolute_url

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REVOKE_TIMEOUT_SECONDS = 5.0
USER_CODE_PATTERN = r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$"
_OAUTH_ERROR_STATUSES = frozenset({400, 401})


class OAuthError(RuntimeError):
    """An RFC 6749 section 5.2 error answer from an OAuth endpoint."""

    def __init__(self, error: str, status_code: int) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code


class _SafeUrlModel(BaseModel):
    @field_validator(
        "verification_uri", "verification_uri_complete", check_fields=False
    )
    @classmethod
    def validate_url(cls, value: str) -> str:
        if safe_absolute_url(value) is None:
            raise ValueError("invalid verification URL")
        return value


class DeviceAuthorization(_SafeUrlModel):
    """A validated device authorization answer."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    device_code: SecretStr
    user_code: str = Field(pattern=USER_CODE_PATTERN)
    verification_uri: str
    verification_uri_complete: str
    expires_in: int = Field(gt=0, le=3600)
    interval: int = Field(default=5, ge=1, le=60)

    @field_validator("device_code")
    @classmethod
    def validate_device_code(cls, value: SecretStr) -> SecretStr:
        if DEVICE_CODE_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("invalid device code")
        return value


class IssuedCredential(BaseModel):
    """The non-secret Solwyn extension of a token answer."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    label: str
    expires_at: AwareDatetime
    account_id: str
    account_name: str
    email: str
    role: Literal["owner", "admin", "member"]


class TokenResponse(BaseModel):
    """A validated device-grant token answer. There is never a refresh token."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    access_token: SecretStr
    token_type: str
    expires_in: int = Field(gt=0)
    scope: str = Field(min_length=1)
    solwyn_credential: IssuedCredential

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: SecretStr) -> SecretStr:
        if CLI_TOKEN_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("invalid CLI credential")
        return value

    @field_validator("token_type")
    @classmethod
    def validate_token_type(cls, value: str) -> str:
        if value.lower() != "bearer":
            raise ValueError("unsupported token type")
        return value


def _oauth_error(response: httpx.Response) -> OAuthError:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, str) or not error.isascii() or not error:
        error = "invalid_response"
    return OAuthError(error[:64], response.status_code)


def start_device_authorization(
    client: SolwynClient, *, scope: str, device_label: str | None
) -> DeviceAuthorization:
    """Ask for a device code and user code. Raises OAuthError or ApiError."""
    form = {"client_id": OAUTH_CLIENT_ID, "scope": scope}
    if device_label:
        form["device_label"] = device_label
    response = client.request_unauthenticated(
        "POST",
        "/oauth/device_authorization",
        data=form,
        accepted_statuses=_OAUTH_ERROR_STATUSES,
    )
    if response.status_code != 200:
        raise _oauth_error(response)
    return DeviceAuthorization.model_validate(response.json())


def poll_token(client: SolwynClient, *, device_code: str) -> TokenResponse:
    """Exchange an approved device code once. Raises OAuthError or ApiError."""
    response = client.request_unauthenticated(
        "POST",
        "/oauth/token",
        data={
            "grant_type": DEVICE_GRANT_TYPE,
            "device_code": device_code,
            "client_id": OAUTH_CLIENT_ID,
        },
        accepted_statuses=_OAUTH_ERROR_STATUSES,
    )
    if response.status_code != 200:
        raise _oauth_error(response)
    return TokenResponse.model_validate(response.json())


def _revocation_client(api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{api_url.rstrip('/')}/",
        headers={"User-Agent": f"solwyn-cli/{__version__}"},
        timeout=REVOKE_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


revocation_client: Callable[[str], httpx.Client] = _revocation_client


def revoke_token(api_url: str, token: str) -> bool:
    """Revoke a CLI credential once, best effort. Only a 200 counts as revoked."""
    try:
        with revocation_client(api_url) as client:
            response = client.post(
                "oauth/revoke",
                data={
                    "token": token,
                    "token_type_hint": "access_token",
                    "client_id": OAUTH_CLIENT_ID,
                },
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 200
