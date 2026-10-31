"""Credential storage and class-based credential resolution.

Stored credentials live in one store chosen by ``SOLWYN_CREDENTIAL_STORE``,
then ``credential_store`` in config, then ``auto``:

- ``auto``: the OS keychain on macOS and Windows, falling back to the file when
  a keychain write fails; the file on every Linux. An existing file entry wins
  on read, and every write deletes the stale copy in the other store.
- ``keyring``: the OS keychain or a loud failure, never a fallback.
- ``file``: the file only; the keyring is never touched, except by logout,
  which always looks in and clears both stores.

Everything stored is keyed by API origin (see ``credential_store``), so a login
made against one API is never sent to another.
"""

import os
import re
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Literal, TypeAlias

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

from solwyn_cli import credential_store
from solwyn_cli._vendor.validation import validate_project_key_format
from solwyn_cli.credential_store import (
    STORE_ENV,
    KeyringAbsentError,
    STORE_MODES,
    FileStore,
    InsecureFileError,
    KeyringStore,
    StoreKind,
    StoreMode,
    StoreUnavailableError,
    credential_origin,
)

API_KEY_ENTRY = "api_key"
CLI_CREDENTIAL_ENTRY = "cli_credential"
CLI_CREDENTIAL_META_ENTRY = "cli_credential_meta"
PENDING_DEVICE_ENTRY = "pending_device"
LOGIN_ENTRIES = (CLI_CREDENTIAL_ENTRY, CLI_CREDENTIAL_META_ENTRY)
ALL_ENTRIES = (API_KEY_ENTRY, *LOGIN_ENTRIES, PENDING_DEVICE_ENTRY)
# CLI 0.3.0 wrote these keyring entries (service "solwyn-cli", no origin): a
# session pair and a project key. They are never used to authenticate; a
# terminal user is told about them once, and login and logout delete them.
LEGACY_LOGIN_ENTRIES = ("access_token", "refresh_token")
LEGACY_KEY_ENTRY = "api_key"
# Everything without an origin, including 0.4 pre-release names.
LEGACY_ENTRIES = (
    *LEGACY_LOGIN_ENTRIES,
    LEGACY_KEY_ENTRY,
    CLI_CREDENTIAL_ENTRY,
    CLI_CREDENTIAL_META_ENTRY,
    PENDING_DEVICE_ENTRY,
)
AUTH_EXIT_CODE = 3
AUTH_SELECTION_ENV = "SOLWYN_AUTH"
TOKEN_ENV = "SOLWYN_TOKEN"
API_KEY_ENV = "SOLWYN_API_KEY"
OAUTH_CLIENT_ID = "solwyn-cli"
CLI_TOKEN_PATTERN = re.compile(r"solwyn_cli_[0-9a-f]{64}")
DEVICE_CODE_PATTERN = re.compile(r"solwyn_dc_[0-9a-f]{64}")

CredentialClass: TypeAlias = Literal["user", "metering"]
AuthSelection: TypeAlias = Literal["login", "project_key"]
CredentialSource: TypeAlias = Literal["environment", "keyring", "file"]


class AuthError(RuntimeError):
    """Authentication failed without exposing credential material."""

    exit_code: ClassVar[int] = AUTH_EXIT_CODE
    # A terminal auth failure cannot erase an earlier ingest attempt's uncertainty.
    ingest_outcome_uncertain: bool = False
    hint: str | None = None


class CredentialStorageError(AuthError):
    """The credential store could not be read or updated."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class KeyringUnavailableError(CredentialStorageError):
    """keyring mode was selected, and the OS keychain cannot be used."""


class InsecureCredentialFileError(CredentialStorageError):
    """The credentials file is readable by, or owned by, another user."""


class LoginRequiredError(AuthError):
    """A user-plane operation resolved a project key instead of a Solwyn login."""


class MeteringKeyRequiredError(AuthError):
    """A metering operation found no project key; the login is never used."""


LOGIN_REQUIRED_HINT = (
    "This needs a Solwyn login. Run solwyn login, or set SOLWYN_TOKEN, and retry. "
    "If SOLWYN_AUTH=project_key is set, unset it."
)
METERING_KEY_HINT = (
    "Metering needs a project key. Run solwyn login --with-key or set SOLWYN_API_KEY."
)
FILE_STORE_HINT = (
    "Set SOLWYN_CREDENTIAL_STORE=file or run solwyn login --store file to use "
    "the credentials file instead."
)


class ProjectKeyCredential(BaseModel):
    """A validated project API key with an explicitly retrievable secret."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: SecretStr
    source: CredentialSource
    # Where it came from: a file path, the keychain's name, or the env var.
    location: str | None = None

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
    """A scoped, expiring CLI credential whose repr remains redacted.

    A SOLWYN_TOKEN login has no local metadata; the API knows its expiry.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: SecretStr
    meta: LoginMetadata | None
    source: CredentialSource = "keyring"
    location: str | None = None

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
    interval: int = Field(ge=1)

    @field_validator("device_code")
    @classmethod
    def validate_device_code(cls, value: SecretStr) -> SecretStr:
        """Accept only the distinctive device code format."""
        if DEVICE_CODE_PATTERN.fullmatch(value.get_secret_value()) is None:
            raise ValueError("invalid device code")
        return value

    @field_serializer("device_code")
    def serialize_device_code(self, value: SecretStr) -> str:
        """Reveal the device code only for credential storage."""
        return value.get_secret_value()


ResolvedCredential: TypeAlias = ProjectKeyCredential | LoginCredential


def _now() -> datetime:
    return datetime.now(UTC)


# --- API origin -------------------------------------------------------------

_ACTIVE_API_URL: ContextVar[str | None] = ContextVar(
    "solwyn_active_api_url", default=None
)


def use_api_url(api_url: str) -> Callable[[], None]:
    """Scope stored-credential access to one API; return the matching reset."""
    token = _ACTIVE_API_URL.set(api_url)
    return lambda: _ACTIVE_API_URL.reset(token)


def _default_api_url() -> str:
    from solwyn_cli.config import Config, ConfigLoadError, load_config

    try:
        configured = load_config().api_url
    except ConfigLoadError:
        configured = Config().api_url
    candidate = os.environ.get("SOLWYN_API_URL") or configured
    try:
        return Config(api_url=candidate).api_url
    except ValidationError:
        return Config().api_url


def current_origin(api_url: str | None = None) -> str:
    """Return the origin stored credentials are read and written under."""
    return credential_origin(api_url or _ACTIVE_API_URL.get() or _default_api_url())


# --- Store selection --------------------------------------------------------


def store_mode(environ: Mapping[str, str] | None = None) -> StoreMode:
    """Return SOLWYN_CREDENTIAL_STORE, then config credential_store, then auto."""
    environment = os.environ if environ is None else environ
    value = environment.get(STORE_ENV, "")
    if value:
        if value not in STORE_MODES:
            raise CredentialStorageError(
                f"{STORE_ENV} must be auto, keyring or file.",
                "Unset it, or set it to auto, keyring or file.",
            )
        return value  # type: ignore[return-value]
    from solwyn_cli.config import ConfigLoadError, load_config

    try:
        return load_config().credential_store
    except ConfigLoadError:
        return "auto"


def primary_kind(mode: StoreMode, platform: str | None = None) -> StoreKind:
    """Return the store a write tries first in this mode on this platform."""
    if mode != "auto":
        return mode
    family = credential_store.platform_name(platform)
    return "keyring" if family in ("macos", "windows") else "file"


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _file_store() -> FileStore:
    try:
        return FileStore()
    except StoreUnavailableError as error:
        raise CredentialStorageError(str(error)) from None


def _keyring_store() -> KeyringStore:
    return KeyringStore()


def _keyring_error(error: StoreUnavailableError) -> KeyringUnavailableError:
    hint = FILE_STORE_HINT
    if credential_store.platform_name() == "linux":
        hint += (
            " To keep the keyring, an MCP host must pass DBUS_SESSION_BUS_ADDRESS "
            "and XDG_RUNTIME_DIR to the server."
        )
    return KeyringUnavailableError(str(error), hint)


def _file_error(error: Exception) -> CredentialStorageError:
    if isinstance(error, InsecureFileError):
        return InsecureCredentialFileError(str(error))
    return CredentialStorageError(str(error))


@dataclass(frozen=True)
class Lookup:
    """Where a credential lookup looked; never a secret."""

    mode: StoreMode
    kind: StoreKind
    location: str
    keyring_unavailable: bool = False

    def describe(self) -> str:
        """Return e.g. 'file, /home/a/.local/share/solwyn/credentials.json'."""
        return f"{self.kind}, {self.location}"


_LAST_LOOKUP: ContextVar[Lookup | None] = ContextVar("solwyn_last_lookup", default=None)


def last_lookup() -> Lookup | None:
    """Return where the last lookup in this context looked."""
    return _LAST_LOOKUP.get()


def where_stored(environ: Mapping[str, str] | None = None) -> Lookup:
    """Describe the store this context reads and writes, without touching it."""
    mode = store_mode(environ)
    kind = primary_kind(mode)
    if kind == "file":
        return Lookup(mode=mode, kind="file", location=_file_store().describe())
    return Lookup(mode=mode, kind="keyring", location=credential_store.keyring_name())


def _record(lookup: Lookup) -> None:
    _LAST_LOOKUP.set(lookup)


# --- Reading ----------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """Entries read from one store."""

    values: dict[str, str | None]
    kind: StoreKind | None
    location: str | None


def _read(entries: tuple[str, ...], *, origin: str, mode: StoreMode) -> Found:
    """Read entries from one store: the file if it holds this API, else keyring."""
    if mode in ("file", "auto"):
        file = _file_store()
        try:
            if (
                mode == "file"
                or primary_kind(mode) == "file"
                or file.has_origin(origin)
            ):
                values = {entry: file.get(origin, entry) for entry in entries}
                _record(Lookup(mode, "file", file.describe()))
                return Found(values, "file", file.describe())
        except (InsecureFileError, StoreUnavailableError) as error:
            raise _file_error(error) from None
    keyring_store = _keyring_store()
    try:
        values = {entry: keyring_store.get(origin, entry) for entry in entries}
    except StoreUnavailableError as error:
        if mode == "keyring":
            raise _keyring_error(error) from None
        # auto on macOS or Windows: an unusable keychain reads as empty.
        _record(Lookup(mode, "keyring", keyring_store.describe(), True))
        return Found(dict.fromkeys(entries), None, None)
    _record(Lookup(mode, "keyring", keyring_store.describe()))
    return Found(values, "keyring", keyring_store.describe())


# --- Writing ----------------------------------------------------------------


@dataclass(frozen=True)
class Written:
    """Where a write landed, and whether it fell back from the keychain."""

    kind: StoreKind
    location: str
    fell_back: bool = False


_LAST_WRITE: ContextVar[Written | None] = ContextVar("solwyn_last_write", default=None)


def last_write() -> Written | None:
    """Return where the last write in this context landed."""
    return _LAST_WRITE.get()


def _keyring_section(keyring_store: KeyringStore, origin: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in ALL_ENTRIES:
        value = keyring_store.get(origin, entry)
        if value is not None:
            values[entry] = value
    return values


def _write(
    values: Mapping[str, str],
    *,
    origin: str,
    mode: StoreMode,
    clear_first: tuple[str, ...] = (),
) -> Written:
    """Write entries for one origin, keeping the whole origin in one store.

    An origin lives in exactly one store. A keychain write carries the
    origin's other file entries into the keychain and then removes the origin
    from the file; a fallback write to the file carries what it can read from
    the keychain and then deletes the origin's keychain entries. Nothing is
    dropped, and a stale key in one store can never shadow a login in the other.
    """
    names = (*clear_first, *values)
    if primary_kind(mode) != "keyring":
        # file mode never touches the keyring, and Linux auto never writes it.
        return _write_file(values, origin=origin, names=names, fell_back=False)
    keyring_store = _keyring_store()
    file = _file_store()
    try:
        carried = {
            entry: value
            for entry, value in file.section(origin).items()
            if entry not in names
        }
    except (InsecureFileError, StoreUnavailableError) as error:
        raise _file_error(error) from None
    try:
        # Drop the old token first: metadata without a token reads as no
        # login, so a failed write never pairs old and new halves.
        keyring_store.delete(origin, clear_first)
        keyring_store.set_many(origin, {**carried, **values})
    except StoreUnavailableError as error:
        if mode == "keyring":
            raise _keyring_error(error) from None
        # auto: fall back to the file, carrying what the keychain still answers.
        try:
            from_keyring = {
                entry: value
                for entry, value in _keyring_section(keyring_store, origin).items()
                if entry not in names and entry not in carried
            }
        except StoreUnavailableError:
            from_keyring = {}
        written = _write_file(
            {**from_keyring, **values}, origin=origin, names=names, fell_back=True
        )
        try:
            keyring_store.delete(origin, ALL_ENTRIES)
        except StoreUnavailableError:
            pass  # The file holds the origin now and is read first.
        return written
    try:
        file.delete(origin, ALL_ENTRIES)
    except (InsecureFileError, StoreUnavailableError) as error:
        # The file would still be read first and hide this write: say so.
        raise _file_error(error) from None
    written = Written("keyring", keyring_store.describe())
    _LAST_WRITE.set(written)
    return written


def _write_file(
    values: Mapping[str, str], *, origin: str, names: tuple[str, ...], fell_back: bool
) -> Written:
    file = _file_store()
    try:
        file.delete(origin, [name for name in names if name not in values])
        file.set_many(origin, values)
    except (InsecureFileError, StoreUnavailableError) as error:
        raise _file_error(error) from None
    written = Written("file", file.describe(), fell_back)
    _LAST_WRITE.set(written)
    return written


ClearStatus: TypeAlias = Literal["cleared", "absent", "unreachable"]


@dataclass(frozen=True)
class Cleared:
    """What one store reported when entries were deleted.

    ``absent``: no keyring exists here, so nothing can be stored in it.
    ``unreachable``: the keyring exists but did not answer; entries were kept.
    """

    kind: StoreKind
    location: str
    status: ClearStatus = "cleared"

    @property
    def cleared(self) -> bool:
        return self.status != "unreachable"


def _keyring_in_use(mode: StoreMode) -> bool:
    return mode == "keyring" or (mode == "auto" and primary_kind(mode) == "keyring")


def _delete(entries: tuple[str, ...], *, origin: str, mode: StoreMode) -> list[Cleared]:
    """Delete entries from the file and, where it is in use, the keychain."""
    results: list[Cleared] = []
    file = _file_store()
    try:
        file.delete(origin, entries)
        results.append(Cleared("file", file.describe()))
    except (InsecureFileError, StoreUnavailableError) as error:
        raise _file_error(error) from None
    if _keyring_in_use(mode):
        keyring_store = _keyring_store()
        try:
            keyring_store.delete(origin, entries)
            results.append(Cleared("keyring", keyring_store.describe()))
        except StoreUnavailableError as error:
            if mode == "keyring":
                raise _keyring_error(error) from None
            results.append(Cleared("keyring", keyring_store.describe(), "unreachable"))
    return results


# --- Resolution -------------------------------------------------------------


def _project_key_credential(
    value: str, source: CredentialSource, location: str | None = None
) -> ProjectKeyCredential:
    try:
        validated = validate_project_key_format(value)
    except ValueError:
        raise AuthError("Configured project key is invalid.") from None
    return ProjectKeyCredential(api_key=validated, source=source, location=location)


def auth_selection(environ: Mapping[str, str] | None = None) -> AuthSelection | None:
    """Return the SOLWYN_AUTH override, or raise ValueError for an unknown value."""
    environment = os.environ if environ is None else environ
    value = environment.get(AUTH_SELECTION_ENV, "")
    if value == "":
        return None
    if value not in ("login", "project_key"):
        raise ValueError("SOLWYN_AUTH must be login or project_key.")
    return value


def _environment_login(environment: Mapping[str, str]) -> LoginCredential | None:
    if TOKEN_ENV not in environment:
        return None
    try:
        return LoginCredential(
            token=environment[TOKEN_ENV],
            meta=None,
            source="environment",
            location=TOKEN_ENV,
        )
    except ValidationError:
        raise AuthError(f"{TOKEN_ENV} is not a valid Solwyn CLI token.") from None


def _stored_login(api_url: str | None = None) -> LoginCredential | None:
    """Read the stored login, or None when absent or corrupt."""
    mode = store_mode()
    found = _read(LOGIN_ENTRIES, origin=current_origin(api_url), mode=mode)
    token = found.values[CLI_CREDENTIAL_ENTRY]
    raw_meta = found.values[CLI_CREDENTIAL_META_ENTRY]
    if token is None or raw_meta is None or found.kind is None:
        return None
    try:
        return LoginCredential(
            token=token,
            meta=LoginMetadata.model_validate_json(raw_meta),
            source=found.kind,
            location=found.location,
        )
    except ValidationError:
        return None


def stored_login(
    *, include_expired: bool = False, api_url: str | None = None
) -> LoginCredential | None:
    """Return the stored login while it is locally unexpired, unless asked."""
    login = _stored_login(api_url)
    if login is None or login.meta is None:
        return None
    if not include_expired and login.meta.expires_at <= _now():
        return None
    return login


def expired_login_date(api_url: str | None = None) -> datetime | None:
    """Return the expiry of a stored login that has already expired."""
    try:
        login = _stored_login(api_url)
    except AuthError:
        return None
    if login is None or login.meta is None or login.meta.expires_at > _now():
        return None
    return login.meta.expires_at


def stored_project_key(api_url: str | None = None) -> ProjectKeyCredential | None:
    """Return the stored project key only, never the environment key."""
    found = _read((API_KEY_ENTRY,), origin=current_origin(api_url), mode=store_mode())
    stored_key = found.values[API_KEY_ENTRY]
    if stored_key is None or found.kind is None:
        return None
    return _project_key_credential(stored_key, found.kind, found.location)


def _project_key(
    environment: Mapping[str, str], api_url: str | None
) -> ProjectKeyCredential | None:
    if API_KEY_ENV in environment:
        return _project_key_credential(
            environment[API_KEY_ENV], "environment", API_KEY_ENV
        )
    return stored_project_key(api_url)


def resolve_credential(
    environ: Mapping[str, str] | None = None,
    *,
    purpose: CredentialClass = "user",
    api_url: str | None = None,
) -> ResolvedCredential | None:
    """Resolve one credential for a command class.

    Metering never uses a login: SOLWYN_API_KEY, then the stored key. User
    commands: SOLWYN_TOKEN, the stored login, SOLWYN_API_KEY, the stored key.
    SOLWYN_AUTH=project_key skips both logins; SOLWYN_AUTH=login requires one.
    """
    environment = os.environ if environ is None else environ
    _LAST_LOOKUP.set(None)
    if purpose == "metering":
        return _project_key(environment, api_url)
    try:
        selection = auth_selection(environment)
    except ValueError as error:
        raise AuthError(str(error)) from None
    if selection != "project_key":
        login = _environment_login(environment) or stored_login(api_url=api_url)
        if login is not None or selection == "login":
            return login
    return _project_key(environment, api_url)


def missing_credential_hint(purpose: CredentialClass = "user") -> str:
    """Return a fixed hint naming where the lookup looked."""
    lookup = last_lookup()
    try:
        where = lookup or where_stored()
    except AuthError:
        where = None
    looked = f" Looked in: {where.describe()}." if where is not None else ""
    if where is not None and where.keyring_unavailable:
        looked = f" {where.location} was not available." + (
            f" {FILE_STORE_HINT}" if where.mode != "file" else ""
        )
    if purpose == "metering":
        return METERING_KEY_HINT + looked
    return (
        "Run solwyn login, or set SOLWYN_TOKEN (a login) or SOLWYN_API_KEY "
        "(a project key) in the environment." + looked
    )


# --- Storing and clearing ---------------------------------------------------


def store_project_key(
    project_key: str, *, mode: StoreMode | None = None, api_url: str | None = None
) -> ProjectKeyCredential:
    """Validate and store a project key; ``last_write()`` says where."""
    credential = _project_key_credential(project_key, "keyring")
    written = _write(
        {API_KEY_ENTRY: credential.bearer_value()},
        origin=current_origin(api_url),
        mode=store_mode() if mode is None else mode,
    )
    return credential.model_copy(
        update={"source": written.kind, "location": written.location}
    )


def store_login(
    token: str,
    meta: LoginMetadata,
    *,
    mode: StoreMode | None = None,
    api_url: str | None = None,
) -> LoginCredential:
    """Store a CLI login and drop the pending device; ``last_write()`` says where."""
    try:
        credential = LoginCredential(token=token, meta=meta)
    except ValidationError:
        raise CredentialStorageError("Received an invalid CLI credential.") from None
    origin = current_origin(api_url)
    selected = store_mode() if mode is None else mode
    written = _write(
        {
            CLI_CREDENTIAL_META_ENTRY: meta.model_dump_json(),
            CLI_CREDENTIAL_ENTRY: credential.bearer_value(),
        },
        origin=origin,
        mode=selected,
        clear_first=(CLI_CREDENTIAL_ENTRY,),
    )
    try:
        _delete((PENDING_DEVICE_ENTRY,), origin=origin, mode=selected)
    except CredentialStorageError:
        pass
    return credential.model_copy(
        update={"source": written.kind, "location": written.location}
    )


def clear_login(api_url: str | None = None) -> list[Cleared]:
    """Remove only the stored CLI login for this API."""
    return _delete(LOGIN_ENTRIES, origin=current_origin(api_url), mode=store_mode())


def store_pending_device(pending: PendingDevice, *, api_url: str | None = None) -> None:
    """Keep an in-progress device login so login --continue can resume it."""
    _write(
        {PENDING_DEVICE_ENTRY: pending.model_dump_json()},
        origin=current_origin(api_url),
        mode=store_mode(),
    )


def load_pending_device(api_url: str | None = None) -> PendingDevice | None:
    """Return the stored device login, or None when absent or corrupt."""
    found = _read(
        (PENDING_DEVICE_ENTRY,), origin=current_origin(api_url), mode=store_mode()
    )
    raw = found.values[PENDING_DEVICE_ENTRY]
    if raw is None:
        return None
    try:
        return PendingDevice.model_validate_json(raw)
    except ValidationError:
        return None


def clear_pending_device(api_url: str | None = None) -> None:
    """Forget an in-progress device login. Best effort."""
    try:
        _delete(
            (PENDING_DEVICE_ENTRY,), origin=current_origin(api_url), mode=store_mode()
        )
    except CredentialStorageError:
        pass


def _login_from(
    values: Mapping[str, str | None], kind: StoreKind, location: str
) -> LoginCredential | None:
    token = values.get(CLI_CREDENTIAL_ENTRY)
    raw_meta = values.get(CLI_CREDENTIAL_META_ENTRY)
    if token is None or raw_meta is None:
        return None
    try:
        return LoginCredential(
            token=token,
            meta=LoginMetadata.model_validate_json(raw_meta),
            source=kind,
            location=location,
        )
    except ValidationError:
        return None


def logins_in_every_store(api_url: str | None = None) -> list[LoginCredential]:
    """Return the logins stored for this API in the file and in the keychain.

    The one exception to store isolation: logout looks in both stores whatever
    SOLWYN_CREDENTIAL_STORE or --store says, so no login is left behind live.
    """
    origin = current_origin(api_url)
    logins: list[LoginCredential] = []
    file = _file_store()
    try:
        login = _login_from(file.section(origin), "file", file.describe())
    except (InsecureFileError, StoreUnavailableError) as error:
        raise _file_error(error) from None
    if login is not None:
        logins.append(login)
    keyring_store = _keyring_store()
    try:
        login = _login_from(
            _keyring_section(keyring_store, origin), "keyring", keyring_store.describe()
        )
    except StoreUnavailableError:
        login = None  # Reported by clear_credentials.
    if login is not None and all(
        login.bearer_value() != found.bearer_value() for found in logins
    ):
        logins.append(login)
    return logins


def clear_credentials(api_url: str | None = None) -> list[Cleared]:
    """Remove this API's credentials from both stores, and 0.3 keyring entries.

    Like logins_in_every_store, this ignores the selected store: logout clears
    the file and the keychain. A keychain that does not exist here is
    ``absent``; one that exists but does not answer is ``unreachable``, and its
    entries are retained.
    """
    origin = current_origin(api_url)
    file = _file_store()
    try:
        file.delete(origin, ALL_ENTRIES)
    except (InsecureFileError, StoreUnavailableError) as error:
        raise _file_error(error) from None
    results = [Cleared("file", file.describe())]
    keyring_store = _keyring_store()
    try:
        keyring_store.delete(origin, ALL_ENTRIES)
        for entry in LEGACY_ENTRIES:
            keyring_store.delete_raw(entry)
        results.append(Cleared("keyring", keyring_store.describe()))
    except KeyringAbsentError:
        results.append(Cleared("keyring", keyring_store.describe(), "absent"))
    except StoreUnavailableError:
        results.append(Cleared("keyring", keyring_store.describe(), "unreachable"))
    return results


def stored_entry_status(
    api_url: str | None = None,
) -> Literal["found", "missing", "incomplete", "unavailable"]:
    """Classify what is stored for this API, for diagnostics."""
    try:
        found = _read(
            (API_KEY_ENTRY, *LOGIN_ENTRIES),
            origin=current_origin(api_url),
            mode=store_mode(),
        )
    except AuthError:
        return "unavailable"
    lookup = last_lookup()
    if lookup is not None and lookup.keyring_unavailable:
        return "unavailable"
    key = found.values[API_KEY_ENTRY]
    token = found.values[CLI_CREDENTIAL_ENTRY]
    meta = found.values[CLI_CREDENTIAL_META_ENTRY]
    if (token is None) != (meta is None):
        return "incomplete"
    return "found" if key is not None or token is not None else "missing"


# --- Entries from 0.3 and earlier -------------------------------------------


def _legacy_keyring_allowed(mode: StoreMode) -> bool:
    """Touch the keyring for old entries only where it is safe to.

    file mode never touches the keyring. On Linux in auto mode the keyring may
    hang on D-Bus or prompt, so only an interactive terminal probes it.
    """
    if mode == "file":
        return False
    if _keyring_in_use(mode):
        return True
    return _interactive()


def delete_legacy_entries(mode: StoreMode | None = None) -> bool:
    """Delete 0.3 keyring entries after a login, best effort; return success."""
    selected = store_mode() if mode is None else mode
    if not _legacy_keyring_allowed(selected):
        return False
    keyring_store = _keyring_store()
    try:
        for entry in LEGACY_ENTRIES:
            keyring_store.delete_raw(entry)
    except StoreUnavailableError:
        return False
    return True


LEGACY_LOGIN_NOTICE = (
    "Your login from Solwyn CLI 0.3 is still in the system keyring, but it no "
    "longer works. Run solwyn login again."
)
LEGACY_KEY_NOTICE = (
    "A project key saved by Solwyn CLI 0.3 is in the system keyring and is no "
    "longer read. Run solwyn login --with-key again, or set SOLWYN_API_KEY."
)


def legacy_login_notice() -> str | None:
    """At a terminal, point at 0.3 keyring entries; never authenticate with them.

    Only presence is checked. file mode never probes the keyring, and nothing
    is probed without a terminal (MCP servers, CI).
    """
    try:
        mode = store_mode()
    except AuthError:
        return None
    if mode == "file" or not _interactive():
        return None
    keyring_store = _keyring_store()
    try:
        login = any(keyring_store.get_raw(entry) for entry in LEGACY_LOGIN_ENTRIES)
        key = bool(keyring_store.get_raw(LEGACY_KEY_ENTRY))
    except StoreUnavailableError:
        return None
    notices = [LEGACY_LOGIN_NOTICE] if login else []
    if key:
        notices.append(LEGACY_KEY_NOTICE)
    return " ".join(notices) or None
