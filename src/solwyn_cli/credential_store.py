"""Where stored Solwyn credentials live: the OS keyring or one private file.

Every entry is keyed by API origin, so a login made against a local API is
never sent to production. Two stores share one entry vocabulary:

- ``FileStore``: ``credentials.json`` in the user data directory, directory
  0700, file 0600, replaced atomically. A file other users can read is refused,
  the way ssh refuses a private key.
- ``KeyringStore``: the platform keyring, instantiated directly (macOS
  Keychain, Windows Credential Manager, Secret Service or KWallet). Global
  keyring configuration (``PYTHON_KEYRING_BACKEND``, ``keyringrc.cfg``) is
  ignored, plaintext ``keyrings.alt`` backends are refused, and every call is
  bounded by a 5 second timeout.

This module never prints. Callers turn its results into messages.
"""

import json
import os
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypeVar
from urllib.parse import urlsplit

from platformdirs import user_data_dir

from solwyn_cli.config import _config_lock, _fsync_parent_directory

StoreKind: TypeAlias = Literal["keyring", "file"]
StoreMode: TypeAlias = Literal["auto", "keyring", "file"]
STORE_MODES: tuple[StoreMode, ...] = ("auto", "keyring", "file")

STORE_ENV = "SOLWYN_CREDENTIAL_STORE"
FILE_ENV = "SOLWYN_CREDENTIALS_FILE"
KEYRING_SERVICE = "solwyn-cli"
KEYRING_TIMEOUT_SECONDS = 5.0
FILE_VERSION = 1
# Entries whose values are JSON documents; the file keeps them as objects.
JSON_ENTRIES = frozenset({"cli_credential_meta", "pending_device"})

T = TypeVar("T")


class StoreUnavailableError(Exception):
    """The store cannot be used here; the message is fixed and secret-free."""


class InsecureFileError(Exception):
    """The credentials file is readable or owned by someone else."""

    def __init__(self, path: Path, reason: Literal["mode", "owner"]) -> None:
        self.path = path
        super().__init__(
            f"{path} is readable by other users. Run chmod 600 {path}."
            if reason == "mode"
            else f"{path} is owned by another user. Remove it and run solwyn login."
        )


def credential_origin(api_url: str) -> str:
    """Return the key a credential is stored under: scheme, host, port, path."""
    parsed = urlsplit(api_url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default_port = {"http": 80, "https": 443}.get(scheme)
    authority = host if port in (None, default_port) else f"{host}:{port}"
    return f"{scheme}://{authority}{parsed.path.rstrip('/')}"


def platform_name(platform: str | None = None) -> Literal["macos", "windows", "linux"]:
    """Collapse sys.platform to the three storage families."""
    current = sys.platform if platform is None else platform
    if current == "darwin":
        return "macos"
    if current in ("win32", "cygwin"):
        return "windows"
    return "linux"


def credentials_path() -> Path:
    """Return the credentials file: SOLWYN_CREDENTIALS_FILE, else the data dir."""
    override = os.environ.get(FILE_ENV, "")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise StoreUnavailableError(f"{FILE_ENV} must be an absolute path.")
        return path
    # Non-roaming on Windows (%LOCALAPPDATA%); XDG_DATA_HOME on Linux.
    return Path(user_data_dir("solwyn", appauthor=False, roaming=False)) / (
        "credentials.json"
    )


class FileStore:
    """One versioned JSON document keyed by API origin."""

    kind: StoreKind = "file"

    def __init__(self, path: Path | None = None) -> None:
        self.path = credentials_path() if path is None else path

    def describe(self) -> str:
        return str(self.path)

    def _check_private(self) -> None:
        if os.name == "nt":
            return  # The profile ACL protects %LOCALAPPDATA%.
        status = self.path.stat()
        if status.st_uid != os.getuid():
            raise InsecureFileError(self.path, "owner")
        if status.st_mode & 0o077:
            raise InsecureFileError(self.path, "mode")

    def _load(self) -> dict[str, Any]:
        try:
            self._check_private()
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": FILE_VERSION, "origins": {}}
        except OSError:
            raise StoreUnavailableError(
                f"Could not read the credentials file at {self.path}."
            ) from None
        try:
            document = json.loads(raw)
        except ValueError:
            document = None
        if (
            not isinstance(document, dict)
            or document.get("version") != FILE_VERSION
            or not isinstance(document.get("origins"), dict)
        ):
            # Unreadable content never yields a credential; a write replaces it.
            return {"version": FILE_VERSION, "origins": {}}
        return document

    def has_origin(self, origin: str) -> bool:
        section = self._load()["origins"].get(origin)
        return isinstance(section, dict) and bool(section)

    def get(self, origin: str, entry: str) -> str | None:
        section = self._load()["origins"].get(origin)
        if not isinstance(section, dict) or entry not in section:
            return None
        value = section[entry]
        if entry in JSON_ENTRIES:
            return json.dumps(value) if isinstance(value, dict) else None
        return value if isinstance(value, str) else None

    def set_many(self, origin: str, values: Mapping[str, str]) -> None:
        def change(origins: dict[str, Any]) -> None:
            section = origins.setdefault(origin, {})
            for entry, value in values.items():
                section[entry] = json.loads(value) if entry in JSON_ENTRIES else value

        self._update(change)

    def delete(self, origin: str, entries: Iterable[str]) -> None:
        names = list(entries)

        def change(origins: dict[str, Any]) -> None:
            section = origins.get(origin)
            if not isinstance(section, dict):
                return
            for entry in names:
                section.pop(entry, None)
            if not section:
                origins.pop(origin, None)

        if self.path.exists():
            self._update(change)

    def _update(self, change: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._ensure_directory()
            with _config_lock(self.path):
                document = self._load()
                change(document["origins"])
                if document["origins"]:
                    self._write(document)
                else:
                    self.path.unlink(missing_ok=True)
        except InsecureFileError:
            raise
        except (OSError, StoreUnavailableError):
            raise StoreUnavailableError(
                f"Could not write the credentials file at {self.path}."
            ) from None

    def _ensure_directory(self) -> None:
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(directory, 0o700)

    def _write(self, document: Mapping[str, Any]) -> None:
        content = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        # O_EXCL with 0600 from the start: no window where the file is readable.
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_parent_directory(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _with_timeout(call: Callable[[], T]) -> T:
    """Run one keyring call, giving up after KEYRING_TIMEOUT_SECONDS."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller.
            outcome["error"] = error

    # A daemon thread: a hung D-Bus call or unlock prompt cannot block exit.
    worker = threading.Thread(target=run, name="solwyn-keyring", daemon=True)
    worker.start()
    worker.join(KEYRING_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise StoreUnavailableError("The system keyring did not answer within 5 s.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# Backends that only pretend to be a keyring, or store plaintext.
_REFUSED_MODULES = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null")


def _platform_backend() -> Any:
    """Instantiate this platform's keyring directly, ignoring global config."""
    family = platform_name()
    if family == "macos":
        from keyring.backends import macOS

        return macOS.Keyring()
    if family == "windows":
        from keyring.backends import Windows

        backend = Windows.WinVaultKeyring()
        # The default (enterprise) persistence roams with domain profiles.
        backend.persist = "local machine"
        return backend
    from keyring.backends import SecretService, kwallet

    for candidate in (SecretService.Keyring, kwallet.DBusKeyring):
        try:
            candidate.priority  # noqa: B018 - raises when unusable (no D-Bus).
        except Exception:  # noqa: BLE001 - any failure means "not here".
            continue
        return candidate()
    raise StoreUnavailableError(
        "The system keyring is not available (no D-Bus session)."
    )


# Injectable for tests: a function returning a keyring backend.
backend_factory: Callable[[], Any] = _platform_backend


def keyring_name(platform: str | None = None) -> str:
    """Return the user-facing name of this platform's keyring."""
    return {
        "macos": "the macOS Keychain",
        "windows": "Windows Credential Manager",
        "linux": "the system keyring (Secret Service)",
    }[platform_name(platform)]


class KeyringStore:
    """One keyring entry per secret, named ``<origin>/<entry>``."""

    kind: StoreKind = "keyring"

    def __init__(self) -> None:
        self._backend: Any = None

    def describe(self) -> str:
        return keyring_name()

    def _get_backend(self) -> Any:
        if self._backend is None:
            backend = _with_timeout(backend_factory)
            module = type(backend).__module__
            if module.startswith(_REFUSED_MODULES):
                raise StoreUnavailableError("The system keyring is not available.")
            self._backend = backend
        return self._backend

    def _call(self, operation: Callable[[Any], T]) -> T:
        try:
            backend = self._get_backend()
            return _with_timeout(lambda: operation(backend))
        except StoreUnavailableError:
            raise
        except Exception:  # noqa: BLE001 - locked, denied, missing daemon.
            raise StoreUnavailableError(
                "The system keyring is not available."
            ) from None

    def get_raw(self, username: str) -> str | None:
        return self._call(
            lambda backend: backend.get_password(KEYRING_SERVICE, username)
        )

    def set_raw(self, username: str, value: str) -> None:
        self._call(
            lambda backend: backend.set_password(KEYRING_SERVICE, username, value)
        )

    def delete_raw(self, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        def delete(backend: Any) -> None:
            try:
                backend.delete_password(KEYRING_SERVICE, username)
            except PasswordDeleteError:
                pass  # Already absent.

        self._call(delete)

    def get(self, origin: str, entry: str) -> str | None:
        return self.get_raw(f"{origin}/{entry}")

    def has_origin(self, origin: str) -> bool:
        return False  # Never consulted: the file decides auto reads.

    def set_many(self, origin: str, values: Mapping[str, str]) -> None:
        for entry, value in values.items():
            self.set_raw(f"{origin}/{entry}", value)

    def delete(self, origin: str, entries: Iterable[str]) -> None:
        for entry in entries:
            self.delete_raw(f"{origin}/{entry}")
