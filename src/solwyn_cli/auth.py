"""Credential resolution and bounded JWT refresh for authenticated requests."""

import os
from collections.abc import Callable, Mapping
from typing import ClassVar, Literal, TypeAlias

import httpx
import keyring
from keyring.errors import NoKeyringError, PasswordDeleteError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
)

from solwyn_cli._vendor.validation import validate_project_key_format
from solwyn_cli._vendor.wire import RefreshRequest

KEYRING_SERVICE = "solwyn-cli"
API_KEY_ENTRY = "api_key"
ACCESS_TOKEN_ENTRY = "access_token"
REFRESH_TOKEN_ENTRY = "refresh_token"
AUTH_EXIT_CODE = 3


class AuthError(RuntimeError):
    """Authentication failed without exposing credential material."""

    exit_code: ClassVar[int] = AUTH_EXIT_CODE


class CredentialStorageError(AuthError):
    """The operating-system credential store could not be updated."""


class ProjectKeyCredential(BaseModel):
    """A validated project API key with an explicitly retrievable secret."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: SecretStr
    source: Literal["environment", "keyring"]

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        """Keep direct model construction inside the project-key invariant."""
        validate_project_key_format(value.get_secret_value())
        return value

    def bearer_value(self) -> str:
        """Return the key for an Authorization header at the request boundary."""
        return self.api_key.get_secret_value()


class JwtCredential(BaseModel):
    """A complete access/refresh pair whose repr remains redacted."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)

    def bearer_value(self) -> str:
        """Return the access token at the request boundary."""
        return self.access_token.get_secret_value()

    def refresh_value(self) -> str:
        """Return the refresh token only for the refresh request boundary."""
        return self.refresh_token.get_secret_value()


class _TokenResponse(BaseModel):
    """Validated token rotation response from the auth API."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    token_type: Literal["bearer"] = "bearer"


class _RefreshRequest(RefreshRequest):
    """Refresh request whose in-memory representation redacts its token."""

    refresh_token: SecretStr

    @field_serializer("refresh_token")
    def serialize_refresh_token(self, value: SecretStr) -> str:
        """Reveal the token only while constructing the authenticated POST body."""
        return value.get_secret_value()


ResolvedCredential: TypeAlias = ProjectKeyCredential | JwtCredential
AuthenticatedRequest: TypeAlias = Callable[[ResolvedCredential], httpx.Response]
CredentialRotation: TypeAlias = Callable[[JwtCredential], None]


def _project_key_credential(
    value: str, source: Literal["environment", "keyring"]
) -> ProjectKeyCredential:
    try:
        validated = validate_project_key_format(value)
    except ValueError:
        raise AuthError("Configured project key is invalid.") from None
    return ProjectKeyCredential(api_key=validated, source=source)


def _get_password(entry: str) -> tuple[bool, str | None]:
    """Read one entry, reporting an unavailable backend as a safe outcome."""
    try:
        return True, keyring.get_password(KEYRING_SERVICE, entry)
    except NoKeyringError:
        return False, None
    except Exception:
        return False, None


def resolve_credential(
    environ: Mapping[str, str] | None = None,
) -> ResolvedCredential | None:
    """Resolve credentials in the frozen env, API-key, JWT precedence order."""
    environment = os.environ if environ is None else environ
    if "SOLWYN_API_KEY" in environment:
        return _project_key_credential(environment["SOLWYN_API_KEY"], "environment")

    available, stored_key = _get_password(API_KEY_ENTRY)
    if not available:
        return None
    if stored_key is not None:
        return _project_key_credential(stored_key, "keyring")

    available, access_token = _get_password(ACCESS_TOKEN_ENTRY)
    if not available:
        return None
    available, refresh_token = _get_password(REFRESH_TOKEN_ENTRY)
    if not available:
        return None
    if not access_token or not refresh_token:
        return None
    return JwtCredential(
        access_token=access_token,
        refresh_token=refresh_token,
    )


def _set_password(entry: str, value: str) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, entry, value)
    except NoKeyringError:
        raise CredentialStorageError(
            "Could not update credentials in the operating-system keychain."
        ) from None
    except Exception:
        raise CredentialStorageError(
            "Could not update credentials in the operating-system keychain."
        ) from None


def _confirm_entry_absent(entry: str) -> bool:
    available, retained_value = _get_password(entry)
    return available and retained_value is None


def _delete_password(entry: str) -> bool:
    try:
        keyring.delete_password(KEYRING_SERVICE, entry)
    except PasswordDeleteError:
        return _confirm_entry_absent(entry)
    except NoKeyringError:
        return _confirm_entry_absent(entry)
    except Exception:
        return _confirm_entry_absent(entry)
    return True


def store_project_key(project_key: str) -> ProjectKeyCredential:
    """Validate and store a project key only in the OS keychain."""
    credential = _project_key_credential(project_key, "keyring")
    _set_password(API_KEY_ENTRY, credential.bearer_value())
    return credential


def store_jwt_pair(access_token: str, refresh_token: str) -> JwtCredential:
    """Store or rotate a complete JWT pair only in the OS keychain."""
    try:
        credential = JwtCredential(
            access_token=access_token,
            refresh_token=refresh_token,
        )
    except ValidationError:
        raise CredentialStorageError("Received an invalid token pair.") from None
    _set_password(REFRESH_TOKEN_ENTRY, credential.refresh_value())
    _set_password(ACCESS_TOKEN_ENTRY, credential.bearer_value())
    return credential


def clear_credentials() -> None:
    """Remove every supported credential entry from the OS keychain."""
    all_entries_cleared = True
    for entry in (API_KEY_ENTRY, ACCESS_TOKEN_ENTRY, REFRESH_TOKEN_ENTRY):
        if not _delete_password(entry):
            all_entries_cleared = False
    if not all_entries_cleared:
        raise CredentialStorageError(
            "Could not clear credentials from the operating-system keychain."
        )


def request_with_refresh_retry(
    *,
    client: httpx.Client,
    api_url: str,
    credential: ResolvedCredential,
    request: AuthenticatedRequest,
    on_credential_rotated: CredentialRotation | None = None,
) -> httpx.Response:
    """Run a request and make at most one JWT refresh plus one retry on 401."""
    response = request(credential)
    if response.status_code != httpx.codes.UNAUTHORIZED:
        return response
    if not isinstance(credential, JwtCredential):
        raise AuthError("Authentication failed. Check your credentials.")

    refresh_request = _RefreshRequest(refresh_token=credential.refresh_value())
    try:
        refresh_response = client.post(
            f"{api_url.rstrip('/')}/api/v1/auth/refresh",
            json=refresh_request.model_dump(),
        )
        if not refresh_response.is_success:
            raise AuthError("Authentication refresh failed. Log in again.")
        token_response = _TokenResponse.model_validate(refresh_response.json())
    except AuthError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, ValidationError):
        raise AuthError("Authentication refresh failed. Log in again.") from None

    rotated = store_jwt_pair(
        token_response.access_token.get_secret_value(),
        token_response.refresh_token.get_secret_value(),
    )
    if on_credential_rotated is not None:
        on_credential_rotated(rotated)
    retry_response = request(rotated)
    if retry_response.status_code == httpx.codes.UNAUTHORIZED:
        raise AuthError("Authentication failed after refresh. Log in again.")
    return retry_response
