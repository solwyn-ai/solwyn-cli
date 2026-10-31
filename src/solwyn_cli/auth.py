"""Credential storage and class-based credential resolution."""

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar, Literal, TypeAlias

import keyring
from keyring.errors import NoKeyringError, PasswordDeleteError
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
)

from solwyn_cli._vendor.validation import validate_project_key_format

KEYRING_SERVICE = "solwyn-cli"
API_KEY_ENTRY = "api_key"
CLI_CREDENTIAL_ENTRY = "cli_credential"
CLI_CREDENTIAL_META_ENTRY = "cli_credential_meta"
PENDING_DEVICE_ENTRY = "pending_device"
# Pre-device-flow session tokens. They are never read, only deleted.
LEGACY_ENTRIES = ("access_token", "refresh_token")
AUTH_EXIT_CODE = 3
AUTH_SELECTION_ENV = "SOLWYN_AUTH"
OAUTH_CLIENT_ID = "solwyn-cli"
CLI_TOKEN_PATTERN = re.compile(r"solwyn_cli_[0-9a-f]{64}")
DEVICE_CODE_PATTERN = re.compile(r"solwyn_dc_[0-9a-f]{64}")

CredentialClass: TypeAlias = Literal["user", "metering"]
AuthSelection: TypeAlias = Literal["login", "project_key"]


class AuthError(RuntimeError):
    """Authentication failed without exposing credential material."""

    exit_code: ClassVar[int] = AUTH_EXIT_CODE
    # A terminal auth failure cannot erase an earlier ingest attempt's uncertainty.
    ingest_outcome_uncertain: bool = False


class CredentialStorageError(AuthError):
    """The operating-system credential store could not be updated."""


class LoginRequiredError(AuthError):
    """A user-plane operation resolved a project key instead of a Solwyn login."""


class MeteringKeyRequiredError(AuthError):
    """A metering operation found no project key; the login is never used."""


LOGIN_REQUIRED_HINT = (
    "This needs a Solwyn login. Run solwyn login and retry. "
    "If SOLWYN_AUTH=project_key is set, unset it."
)
METERING_KEY_HINT = (
    "Metering needs a project key. Run solwyn login --with-key or set SOLWYN_API_KEY."
)


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


class LoginMetadata(BaseModel):
    """Non-secret facts about the stored CLI login, kept beside the token."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(min_length=1)
    label: str
    scope: str
    expires_at: AwareDatetime
    account_id: str
    account_name: str
    email: str
    role: Literal["owner", "admin", "member"]

    @property
    def scopes(self) -> list[str]:
        """Return the granted OAuth scopes in their stored order."""
        return self.scope.split()


class LoginCredential(BaseModel):
    """A scoped, expiring CLI credential whose repr remains redacted."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: SecretStr
    meta: LoginMetadata
    source: Literal["keyring"] = "keyring"

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        """Accept only the distinctive CLI credential format."""
        if CLI_TOKEN_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("invalid CLI credential")
        return value

    def bearer_value(self) -> str:
        """Return the token at the request boundary."""
        return self.token.get_secret_value()


class PendingDevice(BaseModel):
    """A device authorization that is waiting for browser approval."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    device_code: SecretStr
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: AwareDatetime
    interval: int = Field(ge=1, le=60)

    @field_validator("device_code")
    @classmethod
    def validate_device_code(cls, value: SecretStr) -> SecretStr:
        """Accept only the distinctive device code format."""
        if DEVICE_CODE_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("invalid device code")
        return value

    @field_serializer("device_code")
    def serialize_device_code(self, value: SecretStr) -> str:
        """Reveal the device code only for keychain storage."""
        return value.get_secret_value()


ResolvedCredential: TypeAlias = ProjectKeyCredential | LoginCredential


def _now() -> datetime:
    return datetime.now(UTC)


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


def auth_selection(environ: Mapping[str, str] | None = None) -> AuthSelection | None:
    """Return the SOLWYN_AUTH override, or raise ValueError for an unknown value."""
    environment = os.environ if environ is None else environ
    value = environment.get(AUTH_SELECTION_ENV, "")
    if value == "":
        return None
    if value not in ("login", "project_key"):
        raise ValueError("SOLWYN_AUTH must be login or project_key.")
    return value


def _stored_login() -> LoginCredential | None:
    """Read the stored login, or None when absent, unreadable or corrupt."""
    available, token = _get_password(CLI_CREDENTIAL_ENTRY)
    if not available or token is None:
        return None
    available, raw_meta = _get_password(CLI_CREDENTIAL_META_ENTRY)
    if not available or raw_meta is None:
        return None
    try:
        return LoginCredential(
            token=token, meta=LoginMetadata.model_validate_json(raw_meta)
        )
    except ValidationError:
        return None


def stored_login(*, include_expired: bool = False) -> LoginCredential | None:
    """Return the stored login while it is locally unexpired, unless asked."""
    login = _stored_login()
    if login is None or (not include_expired and login.meta.expires_at <= _now()):
        return None
    return login


def expired_login_date() -> datetime | None:
    """Return the expiry of a stored login that has already expired."""
    login = _stored_login()
    if login is None or login.meta.expires_at > _now():
        return None
    return login.meta.expires_at


def _project_key(environment: Mapping[str, str]) -> ProjectKeyCredential | None:
    if "SOLWYN_API_KEY" in environment:
        return _project_key_credential(environment["SOLWYN_API_KEY"], "environment")
    available, stored_key = _get_password(API_KEY_ENTRY)
    if not available or stored_key is None:
        return None
    return _project_key_credential(stored_key, "keyring")


def stored_project_key() -> ProjectKeyCredential | None:
    """Return the keychain project key only, never the environment key."""
    available, stored_key = _get_password(API_KEY_ENTRY)
    if not available or stored_key is None:
        return None
    return _project_key_credential(stored_key, "keyring")


def resolve_credential(
    environ: Mapping[str, str] | None = None,
    *,
    purpose: CredentialClass = "user",
) -> ResolvedCredential | None:
    """Resolve one credential for a command class.

    Metering never uses the login: environment key, then keychain key. User
    commands prefer the login, then the environment key, then the keychain key.
    SOLWYN_AUTH=project_key skips the login; SOLWYN_AUTH=login requires it.
    """
    environment = os.environ if environ is None else environ
    if purpose == "metering":
        return _project_key(environment)
    try:
        selection = auth_selection(environment)
    except ValueError as error:
        raise AuthError(str(error)) from None
    if selection != "project_key":
        login = stored_login()
        if login is not None or selection == "login":
            return login
    return _project_key(environment)


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


def _delete_entries(entries: tuple[str, ...]) -> None:
    results = [_delete_password(entry) for entry in entries]
    if not all(results):
        raise CredentialStorageError(
            "Could not clear credentials from the operating-system keychain."
        )


def store_project_key(project_key: str) -> ProjectKeyCredential:
    """Validate and store a project key only in the OS keychain."""
    credential = _project_key_credential(project_key, "keyring")
    _set_password(API_KEY_ENTRY, credential.bearer_value())
    return credential


def store_login(token: str, meta: LoginMetadata) -> LoginCredential:
    """Store a CLI login and drop the pending device and legacy session entries."""
    try:
        credential = LoginCredential(token=token, meta=meta)
    except ValidationError:
        raise CredentialStorageError("Received an invalid CLI credential.") from None
    # Metadata first: a token without metadata is treated as absent.
    _set_password(CLI_CREDENTIAL_META_ENTRY, meta.model_dump_json())
    _set_password(CLI_CREDENTIAL_ENTRY, credential.bearer_value())
    for entry in (PENDING_DEVICE_ENTRY, *LEGACY_ENTRIES):
        _delete_password(entry)
    return credential


def clear_login() -> None:
    """Remove only the stored CLI login."""
    _delete_entries((CLI_CREDENTIAL_ENTRY, CLI_CREDENTIAL_META_ENTRY))


def store_pending_device(pending: PendingDevice) -> None:
    """Keep an in-progress device login so login --continue can resume it."""
    _set_password(PENDING_DEVICE_ENTRY, json.dumps(pending.model_dump(mode="json")))


def load_pending_device() -> PendingDevice | None:
    """Return the stored device login, or None when absent or corrupt."""
    available, raw = _get_password(PENDING_DEVICE_ENTRY)
    if not available or raw is None:
        return None
    try:
        return PendingDevice.model_validate_json(raw)
    except ValidationError:
        return None


def clear_pending_device() -> None:
    """Forget an in-progress device login. Best effort."""
    _delete_password(PENDING_DEVICE_ENTRY)


def delete_legacy_entries() -> None:
    """Remove pre-device-flow session tokens. Best effort."""
    for entry in LEGACY_ENTRIES:
        _delete_password(entry)


def clear_credentials() -> None:
    """Remove every supported and legacy credential entry from the OS keychain."""
    _delete_entries(
        (
            API_KEY_ENTRY,
            CLI_CREDENTIAL_ENTRY,
            CLI_CREDENTIAL_META_ENTRY,
            PENDING_DEVICE_ENTRY,
            *LEGACY_ENTRIES,
        )
    )
