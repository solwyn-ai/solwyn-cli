"""Credential storage and class-based credential resolution.

Reads and writes use exactly one store, the effective mode:
``SOLWYN_CREDENTIAL_STORE``, else ``credential_store`` in config, else
``auto`` (the OS keychain on macOS and Windows, the credentials file on Linux).

The store is resolved under the credential lock. A keychain write that fails
never falls back: the command fails, naming the keychain and why, and nothing
is stored. ``solwyn login --store`` only saves the mode; nothing is ever copied
between stores. ``file`` mode never touches the keyring. Only logout looks in
both stores, whatever the mode, because a user who switched modes can have
entries in the store not in use.

Every transaction (a file read-modify-write, a multi-entry keyring update, a
mode change, the logout scan, revoke and delete) runs under the credential
lock, so concurrent processes never interleave.

Everything stored is keyed by API origin (see ``credential_store``), so a login
made against one API is never sent to another.
"""

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, TypeAlias

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
    API_KEY_ENTRY,
    ENTRIES,
    LOGIN_ENTRY,
    PENDING_DEVICE_ENTRY,
    STORE_ENV,
    STORE_MODES,
    FileStore,
    InsecureFileError,
    KeyringStore,
    KeyringTimeoutError,
    WriteUnconfirmedError,
    NoKeyringBackendError,
    MalformedFileError,
    StoreKind,
    StoreMode,
    StoreUnavailableError,
    credential_lock,
    credential_origin,
)

ALL_ENTRIES = ENTRIES
# CLI 0.3.0 wrote these keyring entries (service "solwyn-cli", no origin): a
# session pair and a project key. They are never used to authenticate; a
# terminal user is told about them once, and login and logout delete them.
LEGACY_LOGIN_ENTRIES = ("access_token", "refresh_token")
LEGACY_KEY_ENTRY = "api_key"
# Everything without an origin, including 0.4 pre-release names.
LEGACY_ENTRIES = (
    *LEGACY_LOGIN_ENTRIES,
    LEGACY_KEY_ENTRY,
    "cli_credential",
    "cli_credential_meta",
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


class MalformedCredentialFileError(CredentialStorageError):
    """The credentials file, or a keychain login, is not a valid credential."""


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
FILE_STORE_HINT = "Run solwyn login --store file, or set SOLWYN_CREDENTIAL_STORE=file."


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
    """Return the one store this mode reads and writes on this platform."""
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


def _keyring_hint() -> str:
    hint = FILE_STORE_HINT
    if credential_store.platform_name() == "linux":
        hint += (
            " To keep the keyring, an MCP host must pass DBUS_SESSION_BUS_ADDRESS "
            "and XDG_RUNTIME_DIR to the server."
        )
    return hint


ENV_KEY_HINT = (
    "To use SOLWYN_API_KEY without the keychain, set SOLWYN_AUTH=project_key."
)


def _keyring_error(error: StoreUnavailableError) -> KeyringUnavailableError:
    """The keychain in use did not answer: name it and why; never read empty.

    Whether a stored login exists cannot be known, so SOLWYN_API_KEY (which
    comes after the stored login) is not used either; the hint says how.
    """
    return KeyringUnavailableError(
        f"Could not read {credential_store.keyring_name()}: {error}",
        f"{_keyring_hint()} {ENV_KEY_HINT}",
    )


def _file_error(error: Exception) -> CredentialStorageError:
    if isinstance(error, InsecureFileError):
        return InsecureCredentialFileError(str(error))
    if isinstance(error, MalformedFileError):
        return MalformedCredentialFileError(str(error))
    return CredentialStorageError(str(error))


_FILE_ERRORS = (InsecureFileError, MalformedFileError, StoreUnavailableError)


@dataclass(frozen=True)
class Lookup:
    """Where a credential lookup looked; never a secret."""

    mode: StoreMode
    kind: StoreKind
    location: str

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
    if primary_kind(mode) == "file":
        return Lookup(mode=mode, kind="file", location=_file_store().describe())
    return Lookup(mode=mode, kind="keyring", location=credential_store.keyring_name())


# --- Reading ----------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """Entries read from the store in use."""

    values: dict[str, str | None]
    kind: StoreKind | None
    location: str | None


def _read(entries: tuple[str, ...], *, origin: str, mode: StoreMode) -> Found:
    """Read entries from the one store the mode selects."""
    if primary_kind(mode) == "file":
        file = _file_store()
        try:
            section = file.section(origin)
        except _FILE_ERRORS as error:
            raise _file_error(error) from None
        _LAST_LOOKUP.set(Lookup(mode, "file", file.describe()))
        return Found(
            {entry: section.get(entry) for entry in entries}, "file", file.describe()
        )
    keyring_store = _keyring_store()
    try:
        values = {entry: keyring_store.get(origin, entry) for entry in entries}
    except StoreUnavailableError as error:
        # The keychain is the store in use. No backend, locked, denied or
        # silent: none of these proves it empty, so the command fails.
        raise _keyring_error(error) from None
    _LAST_LOOKUP.set(Lookup(mode, "keyring", keyring_store.describe()))
    return Found(values, "keyring", keyring_store.describe())


# --- Writing ----------------------------------------------------------------


@dataclass(frozen=True)
class Written:
    """Where a write landed."""

    kind: StoreKind
    location: str


_LAST_WRITE: ContextVar[Written | None] = ContextVar("solwyn_last_write", default=None)


def last_write() -> Written | None:
    """Return where the last write in this context landed."""
    return _LAST_WRITE.get()


def _complete(entry: str, value: str) -> bool:
    """Whether a stored entry is a whole, valid credential."""
    try:
        if entry == LOGIN_ENTRY:
            _parse_login(value, "file", "")
        elif entry == PENDING_DEVICE_ENTRY:
            PendingDevice.model_validate_json(value)
        else:
            validate_project_key_format(value)
    except (ValidationError, ValueError, TypeError):
        return False
    return True


def _keyring_write_error(error: StoreUnavailableError) -> KeyringUnavailableError:
    """A keychain write failed: name the keychain and why.

    "Nothing was stored" is said only when that is known. A call that timed
    out may have committed before it stopped answering, and a write whose
    read-back failed may have committed too, so their outcome is unknown and
    the message says so and points at logout.
    """
    hint = _keyring_hint()
    if credential_store.stranded():
        hint = f"{credential_store.STRANDED_MESSAGE} {hint}"
    name = credential_store.keyring_name()
    if isinstance(error, (KeyringTimeoutError, WriteUnconfirmedError)):
        return KeyringUnavailableError(
            f"Could not confirm the write to {name}: {error} It may or may not "
            "have been stored. Run solwyn logout to clear this API's stored "
            "credentials.",
            hint,
        )
    return KeyringUnavailableError(
        f"Could not write to {name}: {error} Nothing was stored.", hint
    )


def _store(
    values: Mapping[str, str],
    *,
    origin: str,
    mode: StoreMode | None,
    expect_mode: StoreMode | None = None,
) -> Written:
    """Write entries to the one store in use. Never deletes, never falls back.

    The effective mode is resolved inside the lock, so a concurrent mode change
    cannot split this write from the reads that follow it. ``expect_mode`` is
    a store pinned when a command started: if the selector has changed since,
    nothing is written.
    """
    with _locked():
        if expect_mode is not None:
            current = store_mode()
            if current != expect_mode:
                raise CredentialStorageError(
                    f"The credential store changed from {expect_mode} to {current} "
                    "while this login was in progress. Nothing was stored.",
                    "Run solwyn login again.",
                )
            mode = expect_mode
        if mode is None:
            mode = store_mode()
        if primary_kind(mode) == "file":
            file = _file_store()
            try:
                file.set_many(origin, values)
            except _FILE_ERRORS as error:
                raise _file_error(error) from None
            written = Written("file", file.describe())
            _LAST_WRITE.set(written)
            return written
        keyring_store = _keyring_store()
        try:
            for entry, value in values.items():
                keyring_store.set(origin, entry, value)
        except StoreUnavailableError as error:
            # No fallback, ever: the credential goes to the store in use or
            # nowhere. A timed-out write has also stranded the process.
            raise _keyring_write_error(error) from None
        written = Written("keyring", keyring_store.describe())
        _LAST_WRITE.set(written)
        return written


def credential_selector_lock() -> Any:
    """The exclusive credential lock, for changing the store selector."""
    return _locked()


def _locked(*, shared: bool = False) -> Any:
    """The credential lock (shared for reads), with failures as storage errors."""
    return _LockGuard(shared)


class _LockGuard:
    def __init__(self, shared: bool) -> None:
        self._shared = shared

    def __enter__(self) -> None:
        self._context = credential_lock(shared=self._shared)
        try:
            self._context.__enter__()
        except InsecureFileError as error:
            raise _file_error(error) from None
        except StoreUnavailableError as error:
            raise CredentialStorageError(str(error)) from None

    def __exit__(self, *exc: object) -> None:
        self._context.__exit__(*exc)


def save_store_mode(mode: StoreMode) -> None:
    """Save credential_store to config (auto removes it). Copies nothing."""
    from solwyn_cli.config import (
        ConfigLoadError,
        ConfigWriteError,
        get_config_path,
        update_config_values,
    )

    with _locked():
        path = get_config_path()
        try:
            update_config_values({"credential_store": mode}, omit_default_api_url=True)
        except (ConfigLoadError, ConfigWriteError, ValueError, OSError):
            raise CredentialStorageError(
                f"Could not save credential_store = {mode} to {path}.",
                f"Fix {path}, or set {STORE_ENV}={mode}.",
            ) from None


def unused_store_holdings(api_url: str | None = None) -> Lookup | None:
    """Say whether the store not in use still holds this API's credentials.

    Read only, under the shared lock. Only the file is ever checked: when the
    file is in use the keyring is not probed (file mode never touches it, and
    on Linux it would add D-Bus probing to every whoami).
    """
    origin = current_origin(api_url)
    try:
        with _locked(shared=True):
            mode = store_mode()
            if primary_kind(mode) != "keyring":
                return None
            file = _file_store()
            section = file.section(origin)
    except (AuthError, *_FILE_ERRORS):
        return None
    if LOGIN_ENTRY in section or API_KEY_ENTRY in section:
        return Lookup(mode, "file", file.describe())
    return None


@dataclass(frozen=True)
class Cleared:
    """What one store reported at logout.

    ``cleared``: nothing of this API is left. ``unavailable``: no keyring
    backend is usable from this session and the keyring is not the store in
    use, so it was not checked (nothing is claimed about it). ``unreachable``:
    the store did not answer, or it is the store in use and no backend is
    usable; its entries were kept. ``unread``: an entry exists but could not be
    read (so not revoked); it was kept, and ``kept`` names it.
    """

    kind: StoreKind
    location: str
    status: Literal["cleared", "unavailable", "unreachable", "unread"] = "cleared"
    kept: tuple[str, ...] = ()

    @property
    def cleared(self) -> bool:
        return self.status == "cleared"

    @property
    def checked(self) -> bool:
        return self.status != "unavailable"

    @property
    def retained(self) -> bool:
        """Checked, and something of this API was kept: logout fails."""
        return self.checked and not self.cleared


def _keyring_in_use(mode: StoreMode) -> bool:
    return primary_kind(mode) == "keyring"


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
    """Read the stored login; a malformed one is an error, never skipped.

    Skipping it would fall through to a stored full-scope project key.
    """
    origin = current_origin(api_url)
    with _locked(shared=True):
        found = _read((LOGIN_ENTRY,), origin=origin, mode=store_mode())
    raw = found.values[LOGIN_ENTRY]
    if raw is None or found.kind is None or found.location is None:
        return None
    try:
        return _parse_login(raw, found.kind, found.location)
    except (ValidationError, ValueError, TypeError):
        raise MalformedCredentialFileError(
            f"The login stored in {found.location} for {origin} is not valid. "
            f"Delete the {credential_store.KEYRING_SERVICE!r} entry "
            f"{origin}/{LOGIN_ENTRY!s} there, then run solwyn login."
        ) from None


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
    with _locked(shared=True):
        found = _read(
            (API_KEY_ENTRY,), origin=current_origin(api_url), mode=store_mode()
        )
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
    # The store choice and every read (login, then key) see one consistent
    # state: no switch or write can land between them.
    with _locked(shared=True):
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
    if purpose == "metering":
        return METERING_KEY_HINT + looked
    return (
        "Run solwyn login, or set SOLWYN_TOKEN (a login) or SOLWYN_API_KEY "
        "(a project key) in the environment." + looked
    )


# --- Storing and clearing ---------------------------------------------------


def _login_document(token: str, meta: LoginMetadata) -> str:
    """The one entry a login occupies: token and metadata together."""
    return json.dumps({"token": token, "meta": meta.model_dump(mode="json")})


def _parse_login(raw: str, kind: StoreKind, location: str) -> LoginCredential:
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document) != {"token", "meta"}:
        raise ValueError("not a login document")
    return LoginCredential(
        token=document["token"],
        meta=LoginMetadata.model_validate(document["meta"]),
        source=kind,
        location=location,
    )


def store_project_key(
    project_key: str, *, mode: StoreMode | None = None, api_url: str | None = None
) -> ProjectKeyCredential:
    """Validate and store a project key; ``last_write()`` says where."""
    credential = _project_key_credential(project_key, "keyring")
    written = _store(
        {API_KEY_ENTRY: credential.bearer_value()},
        origin=current_origin(api_url),
        mode=mode,
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
    expect_mode: StoreMode | None = None,
) -> LoginCredential:
    """Store a CLI login as one entry, then forget the pending device login."""
    try:
        credential = LoginCredential(token=token, meta=meta)
    except ValidationError:
        raise CredentialStorageError("Received an invalid CLI credential.") from None
    origin = current_origin(api_url)
    with _locked():
        written = _store(
            {LOGIN_ENTRY: _login_document(credential.bearer_value(), meta)},
            origin=origin,
            mode=mode,
            expect_mode=expect_mode,
        )
        _forget(PENDING_DEVICE_ENTRY, origin=origin, kind=written.kind)
    _LAST_WRITE.set(written)
    return credential.model_copy(
        update={"source": written.kind, "location": written.location}
    )


def _forget(entry: str, *, origin: str, kind: StoreKind) -> None:
    """Delete one entry from the store in use (not a cross-store cleanup)."""
    try:
        if kind == "file":
            _file_store().delete(origin, (entry,))
        else:
            _keyring_store().delete(origin, entry)
    except (*_FILE_ERRORS, StoreUnavailableError, CredentialStorageError):
        pass


def clear_login(api_url: str | None = None) -> list[Cleared]:
    """Remove the stored CLI login for this API from the store in use."""
    origin = current_origin(api_url)
    with _locked():
        mode = store_mode()
        if primary_kind(mode) == "file":
            file = _file_store()
            try:
                file.delete(origin, (LOGIN_ENTRY,))
            except _FILE_ERRORS as error:
                raise _file_error(error) from None
            return [Cleared("file", file.describe())]
        keyring_store = _keyring_store()
        try:
            keyring_store.delete(origin, LOGIN_ENTRY)
        except StoreUnavailableError as error:
            if mode == "keyring":
                raise _keyring_error(error) from None
            return [Cleared("keyring", keyring_store.describe(), "unreachable")]
        return [Cleared("keyring", keyring_store.describe())]


def store_pending_device(
    pending: PendingDevice,
    *,
    api_url: str | None = None,
    expect_mode: StoreMode | None = None,
) -> None:
    """Keep an in-progress device login in the store in use, for --continue."""
    _store(
        {PENDING_DEVICE_ENTRY: pending.model_dump_json()},
        origin=current_origin(api_url),
        mode=None,
        expect_mode=expect_mode,
    )


def load_pending_device(api_url: str | None = None) -> PendingDevice | None:
    """Return the stored device login, or None when absent or corrupt."""
    with _locked(shared=True):
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
    with _locked():
        try:
            kind = primary_kind(store_mode())
        except AuthError:
            return
        _forget(PENDING_DEVICE_ENTRY, origin=current_origin(api_url), kind=kind)


@dataclass(frozen=True)
class LogoutReport:
    """What logout found, revoked and kept, store by store."""

    logins_found: int
    revoked: bool | None
    stores: list[Cleared]


@dataclass
class _Scan:
    """What one store holds for this API, read without changing anything."""

    kind: StoreKind
    location: str
    logins: list[LoginCredential]
    kept: list[str]
    status: Literal["cleared", "unavailable", "unreachable", "unread"] = "cleared"


def _scan_file(origin: str) -> _Scan:
    file = _file_store()
    try:
        section = file.section(origin)
    except _FILE_ERRORS:
        # Unreadable as a whole (malformed, insecure, I/O): keep it all.
        return _Scan("file", file.describe(), [], ["credentials file"], "unread")
    raw = section.get(LOGIN_ENTRY)
    if raw is None:
        return _Scan("file", file.describe(), [], [])
    # The document was validated whole, so a present login parses.
    return _Scan(
        "file", file.describe(), [_parse_login(raw, "file", file.describe())], []
    )


def _scan_keyring(origin: str, *, in_use: bool) -> _Scan:
    keyring_store = _keyring_store()
    where = keyring_store.describe()
    try:
        raw = keyring_store.get(origin, LOGIN_ENTRY)
    except NoKeyringBackendError:
        # Never proof of absence: a dead or missing session bus looks the same
        # as a machine without a keyring. The store in use must answer.
        return _Scan(
            "keyring", where, [], [], "unreachable" if in_use else "unavailable"
        )
    except StoreUnavailableError:
        return _Scan("keyring", where, [], [LOGIN_ENTRY], "unread")
    if raw is None:
        return _Scan("keyring", where, [], [])
    try:
        return _Scan("keyring", where, [_parse_login(raw, "keyring", where)], [])
    except (ValidationError, ValueError, TypeError):
        return _Scan("keyring", where, [], [LOGIN_ENTRY], "unread")


def _delete_file(origin: str, scan: _Scan) -> Cleared:
    if scan.status == "unread":
        return Cleared("file", scan.location, "unread", tuple(scan.kept))
    try:
        _file_store().delete(origin, ENTRIES)
    except _FILE_ERRORS:
        return Cleared("file", scan.location, "unreachable", ENTRIES)
    return Cleared("file", scan.location)


def _delete_keyring(origin: str, scan: _Scan) -> Cleared:
    if scan.status in ("unavailable", "unreachable"):
        return Cleared("keyring", scan.location, scan.status)
    keyring_store = _keyring_store()
    kept = list(scan.kept)
    failed = False
    for entry in ENTRIES:
        if entry in kept:
            continue  # Never delete what could not be read (or revoked).
        try:
            keyring_store.delete(origin, entry)
        except StoreUnavailableError:
            failed = True
            kept.append(entry)
    for entry in LEGACY_ENTRIES:
        try:
            keyring_store.delete_raw(entry)
        except StoreUnavailableError:
            failed = True
    status: Literal["cleared", "unreachable", "unread"] = (
        "unreachable" if failed else "unread" if kept else "cleared"
    )
    return Cleared("keyring", scan.location, status, tuple(kept))


def logout_everywhere(
    revoke: Callable[[str], bool], api_url: str | None = None
) -> LogoutReport:
    """Revoke and clear this API's logins in both stores, whatever the mode.

    The one exception to store isolation, run as one transaction under the
    exclusive credential lock: read both stores without changing anything,
    revoke every login found, then delete. A kill at any point before the
    delete leaves every not-yet-revoked token stored. An entry that could not
    be read is never deleted or revoked; a login that was read is cleared even
    if its revoke fails.
    """
    origin = current_origin(api_url)
    with _locked():
        in_use = primary_kind(store_mode()) == "keyring"
        scans = [_scan_file(origin), _scan_keyring(origin, in_use=in_use)]
        logins: list[LoginCredential] = []
        for scan in scans:
            for login in scan.logins:
                if all(login.bearer_value() != seen.bearer_value() for seen in logins):
                    logins.append(login)
        results = [revoke(login.bearer_value()) for login in logins]
        stores = [_delete_file(origin, scans[0]), _delete_keyring(origin, scans[1])]
    return LogoutReport(
        logins_found=len(logins),
        revoked=all(results) if results else None,
        stores=stores,
    )


def stored_entry_status(
    api_url: str | None = None,
) -> Literal["found", "missing", "incomplete", "unavailable"]:
    """Classify what is stored for this API, for diagnostics."""
    try:
        with _locked(shared=True):
            found = _read(
                (API_KEY_ENTRY, LOGIN_ENTRY),
                origin=current_origin(api_url),
                mode=store_mode(),
            )
    except AuthError:
        return "unavailable"
    raw = found.values[LOGIN_ENTRY]
    if raw is not None and not _complete(LOGIN_ENTRY, raw):
        return "incomplete"
    return "found" if found.values[API_KEY_ENTRY] or raw else "missing"


# --- Entries from 0.3 and earlier -------------------------------------------


def _legacy_keyring_allowed(mode: StoreMode) -> bool:
    """Delete old entries after a login only where the keyring is in use.

    Linux auto and file mode never touch the keyring on a write; logout
    clears the 0.3 entries there instead.
    """
    return _keyring_in_use(mode)


def delete_legacy_entries(mode: StoreMode | None = None) -> bool:
    """Delete 0.3 keyring entries after a login, best effort; return success."""
    try:
        with _locked():
            selected = store_mode() if mode is None else mode
            if not _legacy_keyring_allowed(selected):
                return False
            keyring_store = _keyring_store()
            for entry in LEGACY_ENTRIES:
                keyring_store.delete_raw(entry)
    except (StoreUnavailableError, CredentialStorageError):
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
    if not _interactive():
        return None
    try:
        with _locked(shared=True):
            if store_mode() == "file":
                return None
            keyring_store = _keyring_store()
            login = any(keyring_store.get_raw(entry) for entry in LEGACY_LOGIN_ENTRIES)
            key = bool(keyring_store.get_raw(LEGACY_KEY_ENTRY))
    except (StoreUnavailableError, AuthError):
        return None
    notices = [LEGACY_LOGIN_NOTICE] if login else []
    if key:
        notices.append(LEGACY_KEY_NOTICE)
    return " ".join(notices) or None
