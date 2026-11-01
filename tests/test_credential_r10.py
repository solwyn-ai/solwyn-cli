"""Review r9 findings 1 to 8: the reason sanitizer, keyring writes and deletes
that are only trusted once read back, the selector and legacy helpers under
the credential lock, one store per login command, honest timeout wording,
relative XDG roots and non-regular credentials files.

Adapted from the reviewer's probes. The keyring is in memory, or the installed
KWallet backend over a fault-injected service interface; the OS is simulated by
pinning the platform family.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from keyring.errors import PasswordDeleteError
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.commands import login as login_module
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
if os.name != "nt":  # Windows has no fcntl; its tests skip below.
    import fcntl

KEY = "sk_proj_" + "a1" * 32
NEW = "solwyn_cli_" + "2b" * 32
SERVICE = "solwyn-cli"
# Captured at import, before tests/conftest.py points these at temp dirs.
UNPATCHED_LOCK_DIRECTORY = credential_store.lock_directory
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX only")


class Backend:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.entries[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise PasswordDeleteError("already missing")
        del self.entries[(service, username)]


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Backend, list[str], Path]:
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    monkeypatch.setattr(
        credential_store, "platform_name", lambda platform=None: "macos"
    )
    monkeypatch.setattr(auth, "_interactive", lambda: False)
    monkeypatch.setattr(login_module, "sleep", lambda seconds: None)
    backend = Backend()
    monkeypatch.setattr(credential_store, "backend_factory", lambda: backend)
    revoked: list[str] = []
    monkeypatch.setattr(
        login_module, "revoke_token", lambda url, token: revoked.append(token) or True
    )
    return backend, revoked, config_path


def _document(token: str = LOGIN_TOKEN) -> str:
    return json.dumps({"token": token, "meta": login_meta().model_dump(mode="json")})


def _issue(monkeypatch: pytest.MonkeyPatch, before: Any = None) -> None:
    """Skip the browser: the device flow issues NEW (after ``before`` runs)."""

    def device_token(*args: Any, **kwargs: Any) -> Any:
        if before is not None:
            before()
        return NEW, login_meta(label="new")

    monkeypatch.setattr(login_module, "_device_token", device_token)


# --- 1. The reason sanitizer strips first, then redacts --------------------


@pytest.mark.parametrize("separator", ["\x00", "\x1b", "​", "⁠", "‮"])
def test_a_key_split_by_invisible_characters_never_reaches_the_error(
    env: Any, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    backend, _, _ = env
    split = separator.join(KEY[i : i + 16] for i in range(0, len(KEY), 16))

    def denied(service: str, username: str, value: str) -> None:
        raise RuntimeError("denied for " + split)

    monkeypatch.setattr(backend, "set_password", denied)

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert "RuntimeError: denied for [redacted]" in error["message"]
    assert KEY not in result.stdout + result.stderr
    assert KEY[8:24] not in result.stdout + result.stderr


def test_the_final_reason_is_redacted_including_the_class_name() -> None:
    long_name = type("A" * 30, (Exception,), {})
    reason = credential_store.backend_reason(long_name("x"))
    assert "A" * 30 not in reason


# --- 2. A keyring write is trusted only once read back ----------------------


class WalletInterface:
    """KWallet's D-Bus service, refusing writes and deletes with -1."""

    def __init__(self, entries: dict[tuple[str, str], str]) -> None:
        self.entries = entries

    def hasEntry(self, handle: int, service: str, username: str, appid: str) -> bool:
        return (service, username) in self.entries

    def readPassword(self, handle: int, service: str, username: str, appid: str) -> str:
        return self.entries[(service, username)]

    def writePassword(
        self, handle: int, service: str, username: str, value: str, appid: str
    ) -> int:
        return -1

    def removeEntry(self, handle: int, service: str, username: str, appid: str) -> int:
        return -1


def _kwallet(monkeypatch: pytest.MonkeyPatch, entries: dict[tuple[str, str], str]):
    from keyring.backends.kwallet import DBusKeyring

    backend = DBusKeyring()
    backend.handle = 1
    backend.connected = lambda service: True  # type: ignore[method-assign]
    backend.iface = WalletInterface(entries)
    monkeypatch.setattr(credential_store, "backend_factory", lambda: backend)
    monkeypatch.setattr(
        credential_store, "platform_name", lambda platform=None: "linux"
    )
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")


def test_a_kwallet_write_it_did_not_keep_fails_and_revokes_the_login(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, revoked, _ = env
    entries = {(SERVICE, f"{PROD}/api_key"): KEY}
    _kwallet(monkeypatch, entries)
    _issue(monkeypatch)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert "did not keep the new value" in error["message"]
    assert revoked == [NEW]
    assert entries == {(SERVICE, f"{PROD}/api_key"): KEY}


def test_a_write_that_returns_but_keeps_nothing_is_a_failed_write(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _, _ = env
    monkeypatch.setattr(backend, "set_password", lambda *args: None)

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].endswith("Nothing was stored.")
    assert KEY not in result.stdout + result.stderr


# --- 3. A delete is trusted only once a read finds the entry gone -----------


def test_a_delete_that_returns_but_keeps_the_entry_is_unreachable(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _, _ = env
    backend.entries[(SERVICE, f"{PROD}/api_key")] = KEY
    monkeypatch.setattr(backend, "delete_password", lambda service, username: None)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3, result.stdout
    detail = json.loads(result.stderr)["detail"]
    assert detail["cleared"] is False
    keychain = detail["stores"][1]
    assert keychain["status"] == "unreachable"
    assert "api_key" in keychain["kept"]
    assert backend.entries[(SERVICE, f"{PROD}/api_key")] == KEY


def test_a_kwallet_delete_it_refused_is_never_reported_cleared(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = {(SERVICE, f"{PROD}/api_key"): KEY}
    _kwallet(monkeypatch, entries)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3, result.stdout
    assert json.loads(result.stderr)["detail"]["stores"][1]["status"] == "unreachable"
    assert entries[(SERVICE, f"{PROD}/api_key")] == KEY


# --- 4. The selector and legacy helpers hold the credential lock ------------


def _hold(shared: bool) -> int:
    lock = credential_store.lock_path()
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    # A separate open file description, as another process would hold it.
    fcntl.flock(holder, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    return holder


@posix_only
def test_config_set_credential_store_waits_for_a_reader(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, config_path = env
    monkeypatch.setattr(credential_store, "LOCK_TIMEOUT_SECONDS", 0.2)
    holder = _hold(shared=True)  # A credential read is in progress.
    try:
        selector = runner.invoke(app, ["config", "set", "credential_store", "file"])
        other = runner.invoke(app, ["config", "set", "default_project", "proj_x"])
    finally:
        os.close(holder)

    assert selector.exit_code == 3
    assert "Another Solwyn process is changing stored credentials" in selector.stderr
    assert "credential_store" not in (
        config_path.read_text() if config_path.exists() else ""
    )
    assert other.exit_code == 0, other.stderr  # Other keys never take the lock.


def _locked_now() -> bool:
    probe = os.open(credential_store.lock_path(), os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False
    finally:
        os.close(probe)


@posix_only
@pytest.mark.parametrize("operation", ["notice", "login-note"])
def test_legacy_keyring_reads_hold_the_credential_lock(
    env: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    backend, _, _ = env
    backend.entries[(SERVICE, "access_token")] = "legacy"
    with credential_store.credential_lock():
        pass
    seen: list[bool] = []
    real_get, real_delete = backend.get_password, backend.delete_password

    def get(service: str, username: str) -> str | None:
        seen.append(_locked_now())
        return real_get(service, username)

    def delete(service: str, username: str) -> None:
        seen.append(_locked_now())
        real_delete(service, username)

    monkeypatch.setattr(backend, "get_password", get)
    monkeypatch.setattr(backend, "delete_password", delete)
    if operation == "notice":
        monkeypatch.setattr(auth, "_interactive", lambda: True)
        assert auth.legacy_login_notice() == auth.LEGACY_LOGIN_NOTICE
    else:
        assert auth.legacy_entries_note() is not None
    assert seen and all(seen), seen


# --- 5. One login command, one store ----------------------------------------


@pytest.mark.parametrize(
    ("argv", "start", "switch_to"),
    [
        (["--json", "login"], "auto", "file"),
        (["--json", "login", "--store", "file"], "file", "keyring"),
    ],
)
def test_a_selector_change_during_the_browser_wait_fails_and_revokes(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
    credentials_file: Path,
    argv: list[str],
    start: str,
    switch_to: str,
) -> None:
    backend, revoked, _ = env
    backend.entries[(SERVICE, f"{PROD}/api_key")] = KEY
    _issue(monkeypatch, before=lambda: auth.save_store_mode(switch_to))  # type: ignore[arg-type]

    result = runner.invoke(app, argv)

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"] == (
        f"The credential store changed from {start} to {switch_to} while this "
        "login was in progress. Nothing was stored."
    )
    assert revoked == [NEW]
    assert NEW not in str(backend.entries)
    assert not credentials_file.exists() or NEW not in credentials_file.read_text()


def test_the_pending_code_goes_to_the_pinned_store(
    env: Any, monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    from solwyn_cli import oauth

    backend, _, _ = env
    authorization = {
        "device_code": "solwyn_dc_" + "4d" * 32,
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://app.solwyn.ai/device",
        "verification_uri_complete": "https://app.solwyn.ai/device?user_code=BCDF-GHJK",
        "expires_in": 600,
        "interval": 5,
    }

    def start(client: Any, **kwargs: Any) -> Any:
        auth.save_store_mode("file")  # A peer switches before the code is kept.
        return oauth.DeviceAuthorization.model_validate(authorization)

    monkeypatch.setattr(login_module, "start_device_authorization", start)

    result = runner.invoke(app, ["--json", "login", "--wait", "0"])

    assert result.exit_code == 3, result.stderr
    assert "changed from auto to file" in json.loads(result.stderr)["message"]
    assert not backend.entries
    assert not credentials_file.exists()


# --- 6. A timed-out write does not claim nothing was stored -----------------


def _commit_then_hang(monkeypatch: pytest.MonkeyPatch, backend: Backend) -> Any:
    release, done = threading.Event(), threading.Event()
    real_set = backend.set_password

    def committed(service: str, username: str, value: str) -> None:
        real_set(service, username, value)
        release.wait(3)
        done.set()

    monkeypatch.setattr(backend, "set_password", committed)
    monkeypatch.setattr(credential_store, "KEYRING_TIMEOUT_SECONDS", 0.05)
    return release, done


@pytest.mark.parametrize("kind", ["key", "login"])
def test_a_timed_out_write_says_the_outcome_is_unknown(
    env: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    backend, revoked, _ = env
    release, done = _commit_then_hang(monkeypatch, backend)
    _issue(monkeypatch)
    try:
        result = (
            runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)
            if kind == "key"
            else runner.invoke(app, ["--json", "login"])
        )
    finally:
        release.set()
        assert done.wait(3)

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith("Could not confirm the write to the macOS ")
    assert "It may or may not have been stored." in error["message"]
    assert "Run solwyn logout" in error["message"]
    assert "Nothing was stored" not in result.stderr
    assert revoked == ([NEW] if kind == "login" else [])
    assert KEY not in result.stdout + result.stderr


class KeychainDenied(Exception):
    """What the macOS backend raises for a denied or locked keychain."""


@pytest.mark.parametrize("kind", ["key", "login"])
def test_a_write_whose_read_back_fails_says_the_outcome_is_unknown(
    env: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    backend, revoked, _ = env
    real_set = backend.set_password

    def set_then_deny_reads(service: str, username: str, value: str) -> None:
        real_set(service, username, value)

        def denied(*args: Any) -> None:
            raise KeychainDenied("User interaction is not allowed.")

        monkeypatch.setattr(backend, "get_password", denied)

    monkeypatch.setattr(backend, "set_password", set_then_deny_reads)
    _issue(monkeypatch)

    result = (
        runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)
        if kind == "key"
        else runner.invoke(app, ["--json", "login"])
    )

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith("Could not confirm the write to the macOS ")
    assert "could not be read back" in error["message"]
    assert "It may or may not have been stored." in error["message"]
    assert "Run solwyn logout" in error["message"]
    assert "Nothing was stored" not in result.stderr
    assert backend.entries  # The write did land: the old wording would lie.
    assert revoked == ([NEW] if kind == "login" else [])
    assert KEY not in result.stdout + result.stderr


class SessionDropped(Exception):
    """What SecretStorage raises when the session bus drops mid-call."""


def _commit_then_raise(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, *, reads_fail: bool
) -> None:
    """Secret Service: CreateItem stores, then the setter's own label read fails."""
    real_set = backend.set_password

    def committed(service: str, username: str, value: str) -> None:
        real_set(service, username, value)
        if reads_fail:

            def gone(*args: Any) -> None:
                raise SessionDropped("org.freedesktop.DBus.Error.NoReply")

            monkeypatch.setattr(backend, "get_password", gone)
        raise SessionDropped("org.freedesktop.DBus.Error.NoReply")

    monkeypatch.setattr(backend, "set_password", committed)


def _login_args(kind: str) -> list[str]:
    return {
        "key": ["--json", "login", "--with-key"],
        "login": ["--json", "login"],
        "pending": ["--json", "login", "--wait", "0"],
    }[kind]


@pytest.mark.parametrize("kind", ["key", "login"])
def test_a_setter_that_fails_after_committing_is_unconfirmed(
    env: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    backend, revoked, _ = env
    _commit_then_raise(monkeypatch, backend, reads_fail=True)
    _issue(monkeypatch)

    result = runner.invoke(app, _login_args(kind), input=KEY)

    assert result.exit_code == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith("Could not confirm the write to the macOS ")
    assert "It may or may not have been stored." in error["message"]
    assert "Run solwyn logout" in error["message"]
    assert "Nothing was stored" not in result.stderr
    assert backend.entries  # The value landed before the setter raised.
    assert revoked == ([NEW] if kind == "login" else [])
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["key", "login"])
def test_a_setter_that_fails_after_committing_counts_once_read_back(
    env: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    backend, revoked, _ = env
    _commit_then_raise(monkeypatch, backend, reads_fail=False)
    _issue(monkeypatch)

    result = runner.invoke(app, _login_args(kind), input=KEY)

    assert result.exit_code == 0, result.stderr
    assert revoked == []
    assert backend.entries


def test_a_pending_code_whose_setter_failed_after_committing_is_unconfirmed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from solwyn_cli import oauth

    backend, _, _ = env
    _commit_then_raise(monkeypatch, backend, reads_fail=True)
    authorization = {
        "device_code": "solwyn_dc_" + "4d" * 32,
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://app.solwyn.ai/device",
        "verification_uri_complete": "https://app.solwyn.ai/device?user_code=BCDF-GHJK",
        "expires_in": 600,
        "interval": 5,
    }
    monkeypatch.setattr(
        login_module,
        "start_device_authorization",
        lambda client, **kwargs: oauth.DeviceAuthorization.model_validate(
            authorization
        ),
    )

    result = runner.invoke(app, _login_args("pending"))

    assert result.exit_code == 3, result.stderr
    message = json.loads(result.stderr.strip().splitlines()[-1])["message"]
    assert "It may or may not have been stored." in message
    assert "Nothing was stored" not in result.stderr
    assert "BCDF-GHJK" not in result.stdout  # No code shown for a failed write.
    assert backend.entries


def test_a_setter_that_fails_before_committing_still_stored_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _, _ = env

    def refused(*args: Any) -> None:
        raise SessionDropped("org.freedesktop.Secret.Error.IsLocked")

    monkeypatch.setattr(backend, "set_password", refused)

    result = runner.invoke(app, _login_args("key"), input=KEY)

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["message"].endswith("Nothing was stored.")
    assert backend.entries == {}


# --- 7. A relative XDG root is ignored --------------------------------------


@pytest.mark.parametrize(
    ("variable", "path_of", "default"),
    [
        ("XDG_STATE_HOME", "lock", ".local/state/solwyn/credentials.lock"),
        ("XDG_DATA_HOME", "credentials", ".local/share/solwyn/credentials.json"),
        ("XDG_CONFIG_HOME", "config", ".config/solwyn/config.toml"),
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux XDG defaults")
def test_a_relative_xdg_root_is_ignored_in_every_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variable: str,
    path_of: str,
    default: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(credential_store.FILE_ENV, raising=False)
    monkeypatch.setenv(variable, "state")

    def resolve() -> Path:
        if path_of == "lock":
            return UNPATCHED_LOCK_DIRECTORY() / credential_store.LOCK_NAME
        if path_of == "credentials":
            return credential_store.credentials_path()
        return config.user_directory(
            "XDG_CONFIG_HOME", lambda: str(home / "unused")
        ) / ("config.toml")

    seen = set()
    for name in ("cwd-a", "cwd-b"):
        (tmp_path / name).mkdir()
        monkeypatch.chdir(tmp_path / name)
        seen.add(resolve())

    assert seen == {home / default}
    if path_of != "config":  # The config case stubs platformdirs above.
        monkeypatch.setenv(variable, str(tmp_path / "absolute"))
        assert str(resolve()).startswith(str(tmp_path / "absolute"))


# --- 8. A non-regular credentials file is refused without blocking ----------


@posix_only
def test_a_credentials_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    fifo = private / "credentials.json"
    os.mkfifo(fifo, 0o600)
    environment = dict(
        os.environ,
        SOLWYN_CREDENTIAL_STORE="file",
        SOLWYN_CREDENTIALS_FILE=str(fifo),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        TERM="dumb",
        COLUMNS="240",
    )
    environment.pop("SOLWYN_API_KEY", None)
    environment.pop("SOLWYN_TOKEN", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solwyn_cli.main import app; app()",
            "--json",
            "whoami",
        ],
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
    )

    assert result.returncode == 3, result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert f"{fifo} is not a regular file" in error["message"]
