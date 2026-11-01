"""Where stored Solwyn credentials live: the OS keyring or one private file.

Every entry is keyed by API origin, so a login made against a local API is
never sent to production. Three entries exist per origin:

- ``login``: one JSON document holding the CLI token and its metadata, so a
  token can never be paired with metadata from another write;
- ``pending_device``: one JSON document for an in-progress device login;
- ``api_key``: a project key.

Two stores hold them:

- ``FileStore``: ``credentials.json`` in the user data directory, directory
  0700, file 0600, replaced atomically. The file is read through one
  descriptor that is checked before it is read, and the whole document is
  validated; anything else is refused with an error naming the file.
- ``KeyringStore``: the platform keyring, instantiated directly (macOS
  Keychain, Windows Credential Manager, Secret Service or KWallet). Global
  keyring configuration (``PYTHON_KEYRING_BACKEND``, ``keyringrc.cfg``) is
  ignored, plaintext ``keyrings.alt`` backends are refused, and every call is
  bounded by a 5 second timeout.

Every credential transaction runs under ``credential_lock``: an exclusive
advisory lock on a file in the credentials directory, so concurrent CLI and MCP
processes never interleave a read-modify-write, a multi-entry update or a
logout.

This module never prints. Callers turn its results into messages.
"""

import errno
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypeVar
from urllib.parse import urlsplit

from platformdirs import user_data_dir, user_state_dir

from solwyn_cli.config import _fsync_parent_directory, user_directory

if os.name == "nt":
    import msvcrt
else:
    import fcntl

StoreKind: TypeAlias = Literal["keyring", "file"]
StoreMode: TypeAlias = Literal["auto", "keyring", "file"]
STORE_MODES: tuple[StoreMode, ...] = ("auto", "keyring", "file")

STORE_ENV = "SOLWYN_CREDENTIAL_STORE"
FILE_ENV = "SOLWYN_CREDENTIALS_FILE"
KEYRING_SERVICE = "solwyn-cli"
KEYRING_TIMEOUT_SECONDS = 5.0
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_NAME = "credentials.lock"
FILE_VERSION = 1

LOGIN_ENTRY = "login"
PENDING_DEVICE_ENTRY = "pending_device"
API_KEY_ENTRY = "api_key"
ENTRIES = (LOGIN_ENTRY, API_KEY_ENTRY, PENDING_DEVICE_ENTRY)
# Entries whose values are JSON documents; the file keeps them as objects.
JSON_ENTRIES = frozenset({LOGIN_ENTRY, PENDING_DEVICE_ENTRY})

T = TypeVar("T")


class StoreUnavailableError(Exception):
    """The store cannot be used here; the message is fixed and secret-free.

    For the keyring this means it exists but did not answer: locked, denied,
    a refused D-Bus connection, or a timeout ("unreachable").
    """


class NoKeyringBackendError(StoreUnavailableError):
    """No keyring backend is usable from this session.

    keyring reported no usable backend (its fail or null backend, or a refused
    plaintext one), the Secret Service or KWallet libraries are not installed,
    or the D-Bus session is missing or dead. This cannot tell "this machine
    never had a keyring" from "a keyring exists but this session cannot reach
    it", so it never proves absence: logout reports the keyring ``unavailable``
    (not checked) when it is not the store in use, and ``unreachable`` when it
    is. Reads and writes in keyring mode fail with a storage error.
    """


class LockTimeoutError(StoreUnavailableError):
    """Another process held the credential lock past the bounded wait."""


class KeyringTimeoutError(StoreUnavailableError):
    """A keyring call did not finish within KEYRING_TIMEOUT_SECONDS."""


class WriteUnconfirmedError(StoreUnavailableError):
    """A keyring write returned, but the read that checks it failed.

    The value may be stored, so nobody may say "nothing was stored".
    """


STRANDED_MESSAGE = (
    "A system keyring call did not finish, so this process stopped changing "
    "stored credentials. Run the command again."
)


InsecureReason: TypeAlias = Literal[
    "mode", "owner", "symlink", "not_file", "directory_owner"
]


class InsecureFileError(Exception):
    """The credentials file or its directory is not private to this user."""

    def __init__(self, path: Path, reason: InsecureReason) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            {
                "mode": f"{path} is readable by other users. Run chmod 600 {path}.",
                "owner": f"{path} is owned by another user. Remove it and run "
                "solwyn login.",
                "symlink": f"{path} is a symbolic link. Replace it with a regular "
                "file, or remove it and run solwyn login.",
                "not_file": f"{path} is not a regular file. Remove it and run "
                "solwyn login.",
                "directory_owner": f"The credentials directory {path} is owned by "
                "another user. Set SOLWYN_CREDENTIALS_FILE to a path in a "
                "directory you own.",
            }[reason]
        )


class MalformedFileError(Exception):
    """The credentials file is not a valid Solwyn credentials document."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"{path} is not a valid Solwyn credentials file. Remove it, or move "
            "it aside, then run solwyn login."
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
    return user_directory(
        "XDG_DATA_HOME",
        lambda: user_data_dir("solwyn", appauthor=False, roaming=False),
    ) / ("credentials.json")


# POSIX only. On Windows the file lives in %LOCALAPPDATA%, protected by the
# profile ACL; os.getuid, O_NOFOLLOW and POSIX modes do not apply there, so the
# ownership and mode checks are skipped and the ACL is relied on.


def private_directory(directory: Path, *, create: bool) -> bool:
    """Require a private directory; return whether it exists.

    A missing directory is created 0700 when ``create``. An existing one must
    be owned by this user; group or world bits are removed.
    """
    if not directory.exists():
        if not create:
            return False
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        return True
    status = os.stat(directory)
    if status.st_uid != os.getuid():
        raise InsecureFileError(directory, "directory_owner")
    if stat.S_IMODE(status.st_mode) & 0o077:
        os.chmod(directory, 0o700)
    return True


# --- The credential lock ---------------------------------------------------

# One process-wide lock (threads: MCP tool calls run in worker threads) plus
# the cross-process advisory lock. Re-entrant within a thread, so a transaction
# may call helpers that take the lock again. Reads take it shared, writes
# exclusive (Windows has no shared mode, so reads are exclusive there too).
_PROCESS_LOCK = threading.RLock()
_HELD = threading.local()
# Set once a keyring write or delete timed out: its worker thread may still
# change the keyring, so this process never releases the lock again and every
# later transaction fails. The thread dies with the process.
_STRANDED = False


def stranded() -> bool:
    """Whether a keyring write timed out in this process (see above)."""
    return _STRANDED


def lock_directory() -> Path:
    """One stable per-user directory for the lock.

    Never derived from SOLWYN_CREDENTIALS_FILE: every process touching this
    user's keychain, config or credentials file must meet on the same lock.
    """
    return user_directory(
        "XDG_STATE_HOME",
        lambda: user_state_dir("solwyn", appauthor=False, roaming=False),
    )


def lock_path() -> Path:
    return lock_directory() / LOCK_NAME


def _open_lock(path: Path, *, shared: bool) -> int | None:
    """Open the lock file; None only for a read where no writer could exist."""
    flags = getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        private_directory(path.parent, create=True)
    except InsecureFileError:
        raise
    except OSError:
        if shared and not path.parent.exists():
            # Nobody can create the lock here, so no writer can hold it.
            return None
        raise StoreUnavailableError(
            f"Could not open the credential lock {path}. Check that "
            f"{path.parent} is writable."
        ) from None
    try:
        try:
            return os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o600)
        except PermissionError:
            # flock works on a read-only descriptor of an existing lock file.
            return os.open(path, os.O_RDONLY | flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StoreUnavailableError(
                f"The credential lock {path} is a symbolic link. Remove it."
            ) from None
        raise StoreUnavailableError(
            f"Could not open the credential lock {path}. Check that "
            f"{path.parent} is writable."
        ) from None


def _try_lock(descriptor: int, *, shared: bool) -> bool:
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (
            errno.EAGAIN,
            errno.EACCES,
            errno.EWOULDBLOCK,
            errno.EDEADLK,
        ):
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def credential_lock(*, shared: bool = False) -> Iterator[None]:
    """Hold the credential lock (shared for reads), waiting a bounded time."""
    if _STRANDED:
        raise KeyringTimeoutError(STRANDED_MESSAGE)
    if getattr(_HELD, "depth", 0):
        if not shared and getattr(_HELD, "shared", False):
            raise RuntimeError("cannot upgrade a shared credential lock")
        _HELD.depth += 1
        try:
            yield
        finally:
            _HELD.depth -= 1
        return
    path = lock_path()
    timeout_error = LockTimeoutError(
        "Another Solwyn process is changing stored credentials; gave up after "
        f"{LOCK_TIMEOUT_SECONDS:g} s waiting for {path}. Retry when it finishes."
    )
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    if not _PROCESS_LOCK.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        raise timeout_error
    try:
        descriptor = _open_lock(path, shared=shared)
        if descriptor is None:
            _HELD.depth, _HELD.shared = 1, True
            try:
                yield
            finally:
                _HELD.depth = 0
            return
        release = True
        try:
            while True:
                try:
                    if _try_lock(descriptor, shared=shared and os.name != "nt"):
                        break
                except OSError:
                    raise StoreUnavailableError(f"Could not lock {path}.") from None
                if time.monotonic() >= deadline:
                    raise timeout_error
                time.sleep(0.02)
            _HELD.depth, _HELD.shared = 1, shared
            try:
                yield
            finally:
                _HELD.depth = 0
                # A keyring write that timed out may still land: keep the lock
                # (and its descriptor) until the process exits.
                release = not _STRANDED
                if release:
                    try:
                        _unlock(descriptor)
                    except OSError:
                        pass  # Closing the descriptor releases it as well.
        finally:
            if release:
                os.close(descriptor)
    finally:
        _PROCESS_LOCK.release()


# --- The file ---------------------------------------------------------------


def _valid_section(section: Any) -> bool:
    """Every entry present is complete and well typed; null is never valid."""
    if not isinstance(section, dict) or not set(section) <= set(ENTRIES):
        return False
    from pydantic import ValidationError

    from solwyn_cli import auth
    from solwyn_cli._vendor.validation import validate_project_key_format

    try:
        if LOGIN_ENTRY in section:
            login = section[LOGIN_ENTRY]
            if not isinstance(login, dict) or set(login) != {"token", "meta"}:
                return False
            if not isinstance(login["token"], str) or not isinstance(
                login["meta"], dict
            ):
                return False
            auth.LoginCredential(
                token=login["token"],
                meta=auth.LoginMetadata.model_validate(login["meta"]),
            )
        if PENDING_DEVICE_ENTRY in section:
            pending = section[PENDING_DEVICE_ENTRY]
            if not isinstance(pending, dict):
                return False
            auth.PendingDevice.model_validate(pending)
        if API_KEY_ENTRY in section:
            key = section[API_KEY_ENTRY]
            if not isinstance(key, str):
                return False
            validate_project_key_format(key)
    except (ValidationError, ValueError, TypeError):
        return False
    return True


def _valid_document(document: Any) -> bool:
    return (
        isinstance(document, dict)
        and set(document) == {"version", "origins"}
        and document["version"] == FILE_VERSION
        and isinstance(document["origins"], dict)
        and all(
            isinstance(origin, str) and _valid_section(section)
            for origin, section in document["origins"].items()
        )
    )


class FileStore:
    """One versioned JSON document keyed by API origin."""

    kind: StoreKind = "file"

    def __init__(self, path: Path | None = None) -> None:
        self.path = credentials_path() if path is None else path

    def describe(self) -> str:
        return str(self.path)

    def _read_bytes(self) -> bytes | None:
        """Read the file through one descriptor that is checked before reading."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            # O_NONBLOCK: a FIFO or device must not block the open (and the
            # lock) before fstat can refuse it.
            flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise InsecureFileError(self.path, "symlink") from None
            raise
        with os.fdopen(descriptor, "rb") as handle:
            if os.name != "nt":
                # The checks and the read use the same descriptor, so the file
                # cannot be swapped between them.
                status = os.fstat(handle.fileno())
                if not stat.S_ISREG(status.st_mode):
                    raise InsecureFileError(self.path, "not_file")
                if status.st_uid != os.getuid():
                    raise InsecureFileError(self.path, "owner")
                if stat.S_IMODE(status.st_mode) & 0o077:
                    raise InsecureFileError(self.path, "mode")
            return handle.read()

    def load(self, *, create_directory: bool = False) -> dict[str, Any]:
        """Return the validated document; a missing file is an empty one."""
        empty: dict[str, Any] = {"version": FILE_VERSION, "origins": {}}
        try:
            if not private_directory(self.path.parent, create=create_directory):
                return empty
            raw = self._read_bytes()
        except InsecureFileError:
            raise
        except OSError:
            raise StoreUnavailableError(
                f"Could not read the credentials file at {self.path}."
            ) from None
        if raw is None:
            return empty
        try:
            document = json.loads(raw.decode("utf-8"))
        except ValueError:  # Includes UnicodeDecodeError.
            raise MalformedFileError(self.path) from None
        if not _valid_document(document):
            raise MalformedFileError(self.path)
        return document

    def section(self, origin: str) -> dict[str, str]:
        """Return every entry stored for one origin, as strings."""
        section = self.load()["origins"].get(origin) or {}
        return {
            entry: json.dumps(value) if entry in JSON_ENTRIES else value
            for entry, value in section.items()
        }

    def get(self, origin: str, entry: str) -> str | None:
        return self.section(origin).get(entry)

    def has_origin(self, origin: str) -> bool:
        return bool(self.section(origin))

    def set_many(self, origin: str, values: Mapping[str, str]) -> None:
        def change(origins: dict[str, Any]) -> None:
            section = origins.setdefault(origin, {})
            for entry, value in values.items():
                section[entry] = json.loads(value) if entry in JSON_ENTRIES else value

        self._update(change)

    def delete(self, origin: str, entries: tuple[str, ...] = ENTRIES) -> None:
        def change(origins: dict[str, Any]) -> None:
            section = origins.get(origin)
            if section is None:
                return
            for entry in entries:
                section.pop(entry, None)
            if not section:
                origins.pop(origin, None)

        if self.path.exists() or self.path.is_symlink():
            self._update(change)

    def _update(self, change: Callable[[dict[str, Any]], None]) -> None:
        with credential_lock():
            try:
                private_directory(self.path.parent, create=True)
                document = self.load()
                change(document["origins"])
                if not _valid_document(document):
                    raise MalformedFileError(self.path)
                if document["origins"]:
                    self._write(document)
                else:
                    self.path.unlink(missing_ok=True)
            except (InsecureFileError, MalformedFileError):
                raise
            except (OSError, StoreUnavailableError):
                raise StoreUnavailableError(
                    f"Could not write the credentials file at {self.path}."
                ) from None

    def _write(self, document: Mapping[str, Any]) -> None:
        content = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        # A new 0600 file in the same directory (O_EXCL, never a symlink), then
        # fsync and an atomic rename: never a readable or partial file.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            flags |= os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_parent_directory(self.path)
        finally:
            temporary.unlink(missing_ok=True)


# --- The keyring ------------------------------------------------------------


def _with_timeout(call: Callable[[], T], *, may_write: bool = False) -> T:
    """Run one keyring call, giving up after KEYRING_TIMEOUT_SECONDS.

    A write or delete that times out strands the process (see ``_STRANDED``):
    its worker may still change the keyring, so the lock is never released.
    """
    global _STRANDED
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
        if may_write:
            _STRANDED = True
        raise KeyringTimeoutError(
            f"The system keyring did not answer within {KEYRING_TIMEOUT_SECONDS:g} s."
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


# Backends that only pretend to be a keyring, or store plaintext.
_REFUSED_MODULES = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null")


def _linux_backend() -> Any:
    """Secret Service, then KWallet: the first viable one, as keyring picks.

    When neither is viable, keyring itself resolves its fail backend (checked
    on Linux: no D-Bus session address, or an address with no bus behind it).
    That raises NoKeyringBackendError, which proves nothing about whether a
    keyring exists. A viable backend whose calls then fail is "unreachable",
    whatever the cause (locked, refused, missing session).
    """
    from keyring.backends import SecretService, kwallet

    for candidate in (SecretService.Keyring, kwallet.DBusKeyring):
        try:
            candidate.priority  # noqa: B018 - raises when not viable.
        except Exception:  # noqa: BLE001 - not viable here.
            continue
        if candidate is SecretService.Keyring and not os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS"
        ):
            # Resolved, but this process lost the session bus: the keyring
            # still exists and may hold entries.
            raise StoreUnavailableError(
                "The system keyring is not reachable from this process (no "
                "DBUS_SESSION_BUS_ADDRESS)."
            )
        return candidate()
    raise NoKeyringBackendError("The system keyring is not available.")


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
    return _linux_backend()


# Injectable for tests: a function returning a keyring backend.
backend_factory: Callable[[], Any] = _platform_backend


def keyring_name(platform: str | None = None) -> str:
    """Return the user-facing name of this platform's keyring."""
    return {
        "macos": "the macOS Keychain",
        "windows": "Windows Credential Manager",
        "linux": "the system keyring (Secret Service)",
    }[platform_name(platform)]


_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")


def backend_reason(error: BaseException) -> str:
    """A short, safe reason from a keyring backend error.

    The exception class and the first line of its message, capped at 160
    characters. Nonprintable and zero-width characters are dropped first, so
    they cannot split a secret past the redactor; then anything token-like (24
    or more key characters) in the final string is replaced. Never a secret.
    """
    lines = str(error).strip().splitlines()
    message = "".join(ch for ch in (lines[0] if lines else "") if ch.isprintable())
    message = _TOKEN_LIKE.sub("[redacted]", message)[:160].rstrip(" .")
    name = type(error).__name__
    return _TOKEN_LIKE.sub("[redacted]", f"{name}: {message}" if message else name)


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
            if type(backend).__module__.startswith(_REFUSED_MODULES):
                raise NoKeyringBackendError("The system keyring is not available.")
            self._backend = backend
        return self._backend

    def _call(self, operation: Callable[[Any], T], *, may_write: bool = False) -> T:
        from keyring.errors import NoKeyringError

        try:
            backend = self._get_backend()
            return _with_timeout(lambda: operation(backend), may_write=may_write)
        except StoreUnavailableError:
            raise
        except NoKeyringError:
            raise NoKeyringBackendError(
                "The system keyring is not available."
            ) from None
        except Exception as error:  # noqa: BLE001 - locked, denied, refused.
            raise StoreUnavailableError(
                f"The system keyring is not available ({backend_reason(error)})."
            ) from None

    def get_raw(self, username: str) -> str | None:
        return self._call(
            lambda backend: backend.get_password(KEYRING_SERVICE, username)
        )

    def set_raw(self, username: str, value: str) -> None:
        """Write one entry, then read it back: a returned call is not proof.

        KWallet's backend ignores writePassword's failure code, so the value is
        only trusted once a read returns it. A setter that raised is not proof
        either way: Secret Service stores the item and then reads its label, so
        it can fail after the value landed. Only a read that does not return
        the new value proves nothing was stored. Callers hold the exclusive lock.
        """
        try:
            self._call(
                lambda backend: backend.set_password(KEYRING_SERVICE, username, value),
                may_write=True,
            )
        except (KeyringTimeoutError, NoKeyringBackendError):
            raise
        except StoreUnavailableError as error:
            try:
                kept = self.get_raw(username)
            except StoreUnavailableError:
                raise WriteUnconfirmedError(
                    f"{error} The new value could not be read back."
                ) from None
            if kept != value:
                raise error from None
            return
        try:
            kept = self.get_raw(username)
        except StoreUnavailableError as error:
            raise WriteUnconfirmedError(
                f"{error} The new value could not be read back."
            ) from None
        if kept != value:
            raise StoreUnavailableError(
                "The system keyring did not keep the new value."
            )

    def delete_raw(self, username: str) -> None:
        """Delete one entry; it is gone only once a read then finds nothing.

        Neither outcome of the call is proof: PasswordDeleteError also means
        denied or cancelled (macOS, KWallet), KWallet ignores removeEntry's
        failure code, and Secret Service deletes only the first matching item.
        """
        from keyring.errors import PasswordDeleteError

        def delete(backend: Any) -> None:
            try:
                backend.delete_password(KEYRING_SERVICE, username)
            except PasswordDeleteError:
                pass

        self._call(delete, may_write=True)
        if self.get_raw(username) is not None:
            raise StoreUnavailableError(
                "The system keyring did not delete a Solwyn entry."
            )

    def get(self, origin: str, entry: str) -> str | None:
        return self.get_raw(f"{origin}/{entry}")

    def set(self, origin: str, entry: str, value: str) -> None:
        self.set_raw(f"{origin}/{entry}", value)

    def delete(self, origin: str, entry: str) -> None:
        self.delete_raw(f"{origin}/{entry}")
