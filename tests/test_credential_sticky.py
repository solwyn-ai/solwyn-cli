"""Review r7 and r9: one effective store, no fallback (a failed keychain write
fails and stores nothing), nothing copied between stores, the credential lock, absent vs unreachable keyrings, logout read failures,
and whole-document validation.

The OS is simulated by pinning the platform family; the keyring is an
in-memory backend shared with forked processes for the concurrency tests.
"""

import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from keyring.errors import NoKeyringError, PasswordDeleteError
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.commands import login as login_module
from solwyn_cli.credential_store import FileStore, credential_origin
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
KEY = "sk_proj_" + "a1" * 32
NEW_KEY = "sk_proj_" + "b2" * 32
NEW = "solwyn_cli_" + "2b" * 32
SERVICE = "solwyn-cli"
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
forking = pytest.mark.skipif(
    "fork" not in mp.get_all_start_methods(), reason="needs fork"
)


class Backend:
    """A keyring backend over a (possibly shared) dict, with fault injection."""

    def __init__(self, entries: Any = None) -> None:
        self.entries = {} if entries is None else entries
        self.locked = False
        self.fail_set: set[str] = set()
        self.fail_get: set[str] = set()

    def get_password(self, service: str, username: str) -> str | None:
        if self.locked or username in self.fail_get:
            raise RuntimeError("keychain locked")
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        if self.locked or username in self.fail_set:
            raise RuntimeError("keychain locked")
        self.entries[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        if self.locked:
            raise RuntimeError("keychain locked")
        if (service, username) not in self.entries:
            raise PasswordDeleteError(username)
        del self.entries[(service, username)]


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(login_module, "sleep", lambda seconds: None)
    monkeypatch.setattr(auth, "_interactive", lambda: False)
    return path


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> Backend:
    store = Backend()
    monkeypatch.setattr(credential_store, "backend_factory", lambda: store)
    _platform(monkeypatch, "macos")
    return store


def _platform(monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)


def _device(monkeypatch: pytest.MonkeyPatch, token: str = NEW, label: str = "new"):
    """Skip the browser: the device flow returns this login."""
    revoked: list[str] = []
    monkeypatch.setattr(
        login_module, "_device_token", lambda *a, **kw: (token, login_meta(label=label))
    )
    monkeypatch.setattr(
        login_module,
        "revoke_token",
        lambda api_url, value: revoked.append(value) or True,
    )
    return revoked


def _login_document(token: str, label: str = "dev-laptop") -> str:
    return json.dumps(
        {"token": token, "meta": login_meta(label=label).model_dump(mode="json")}
    )


def _keyring_login(backend: Backend, token: str, label: str = "dev-laptop") -> None:
    backend.entries[(SERVICE, f"{PROD}/login")] = _login_document(token, label)


AUTHORIZATION = {
    "device_code": "solwyn_dc_" + "4d" * 32,
    "user_code": "BCDF-GHJK",
    "verification_uri": "https://app.solwyn.ai/device",
    "verification_uri_complete": "https://app.solwyn.ai/device?user_code=BCDF-GHJK",
    "expires_in": 600,
    "interval": 5,
}


def _authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the server: device authorization returns a fixed pending login."""
    from solwyn_cli import oauth

    monkeypatch.setattr(
        login_module,
        "start_device_authorization",
        lambda client, **kw: oauth.DeviceAuthorization.model_validate(AUTHORIZATION),
    )


# --- A. No fallback: a failed keychain write fails and stores nothing -------

HINT = "Run solwyn login --store file, or set SOLWYN_CREDENTIAL_STORE=file."


@pytest.mark.parametrize(
    ("family", "name"),
    [("macos", "the macOS Keychain"), ("windows", "Windows Credential Manager")],
)
def test_a_failed_keychain_write_fails_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
    config_path: Path,
    credentials_file: Path,
    family: str,
    name: str,
) -> None:
    _platform(monkeypatch, family)
    backend.locked = True

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith(f"Could not write to {name}: ")
    assert error["message"].endswith("Nothing was stored.")
    assert error["hint"] == HINT
    assert not credentials_file.exists()
    assert not config_path.exists()  # No mode switch either.
    assert not backend.entries
    assert KEY not in result.stdout + result.stderr
    # Nothing sticks: with the keychain healthy again, logins use it.
    backend.locked = False
    _device(monkeypatch)
    login = runner.invoke(app, ["--json", "login"])
    assert login.exit_code == 0, login.stderr
    assert json.loads(login.stdout)["store"] == "keyring"
    assert not credentials_file.exists()


def test_a_browser_login_that_cannot_reach_the_keychain_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
    config_path: Path,
    credentials_file: Path,
) -> None:
    revoked = _device(monkeypatch)
    backend.locked = True

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith("Could not write to the macOS Keychain: ")
    assert error["hint"] == HINT
    assert revoked == [NEW]  # Issued server-side, kept nowhere: revoked.
    assert not credentials_file.exists()
    assert not config_path.exists()
    assert NEW not in result.stdout + result.stderr


def test_a_pending_login_that_cannot_reach_the_keychain_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
    config_path: Path,
    credentials_file: Path,
) -> None:
    _authorization(monkeypatch)
    backend.locked = True

    result = runner.invoke(app, ["--json", "login", "--wait", "0"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"].startswith("Could not write to the macOS Keychain: ")
    assert error["hint"] == HINT
    assert "BCDF-GHJK" not in result.stdout + result.stderr  # No instructions.
    assert not credentials_file.exists()
    assert not config_path.exists()


@pytest.mark.parametrize("forced", ["keyring", "auto"])
def test_an_environment_store_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
    config_path: Path,
    credentials_file: Path,
    forced: str,
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", forced)
    backend.locked = True

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"
    assert not credentials_file.exists()
    assert not config_path.exists()


def test_store_only_saves_the_mode_and_copies_nothing(
    monkeypatch: pytest.MonkeyPatch,
    backend: Backend,
    config_path: Path,
    credentials_file: Path,
) -> None:
    _keyring_login(backend, LOGIN_TOKEN)

    to_file = runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "file"], input=KEY
    )

    assert to_file.exit_code == 0, to_file.stderr
    assert json.loads(to_file.stdout)["store"] == "file"
    assert 'credential_store = "file"' in config_path.read_text()
    section = FileStore().section(credential_origin(PROD))
    assert set(section) == {auth.API_KEY_ENTRY}  # The keychain login stayed put.
    assert json.loads(backend.entries[(SERVICE, f"{PROD}/login")])["token"] == (
        LOGIN_TOKEN
    )
    assert "Copied" not in to_file.stderr

    _device(monkeypatch)
    back = runner.invoke(app, ["--json", "login", "--store", "auto"])

    assert back.exit_code == 0, back.stderr
    assert json.loads(back.stdout)["store"] == "keyring"
    assert "credential_store" not in config_path.read_text()  # auto is the default.
    assert (SERVICE, f"{PROD}/api_key") not in backend.entries  # Not copied back.
    assert FileStore().section(credential_origin(PROD)) == section  # Untouched.
    assert "Copied" not in back.stderr
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential) and found.bearer_value() == NEW


def test_a_mode_that_cannot_be_saved_fails_before_any_login(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, credentials_file: Path
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise config.ConfigWriteError("read-only config")

    monkeypatch.setattr(config, "update_config_values", refuse)

    result = runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "file"], input=KEY
    )

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert "Could not save credential_store = file" in error["message"]
    assert not credentials_file.exists()
    assert not backend.entries
    assert KEY not in result.stdout + result.stderr


# --- B. Stores are isolated -------------------------------------------------


def test_a_failed_keychain_write_keeps_the_existing_keychain_login(
    backend: Backend, credentials_file: Path
) -> None:
    # r7 repro: a failed key write with an unreadable pending entry must not
    # lose the only login.
    _keyring_login(backend, LOGIN_TOKEN)
    backend.fail_set.add(f"{PROD}/api_key")
    backend.fail_get.add(f"{PROD}/pending_device")

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"
    assert not credentials_file.exists()
    assert (SERVICE, f"{PROD}/login") in backend.entries  # Never deleted.
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)
    assert found.bearer_value() == LOGIN_TOKEN


def test_a_copy_in_the_store_not_in_use_is_never_read(
    backend: Backend, credentials_file: Path
) -> None:
    # r7 repro (cleanup failure): a stale file key never shadows a keychain login.
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: KEY})

    auth.store_login(NEW, login_meta(), api_url=PROD)

    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential) and found.bearer_value() == NEW
    assert auth.stored_project_key(api_url=PROD) is None  # The file is not read.
    assert FileStore().section(credential_origin(PROD))  # And not deleted either.


@posix_only
def test_a_read_only_credentials_directory_cannot_promote_a_key(
    backend: Backend, credentials_file: Path
) -> None:
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: KEY})
    credentials_file.parent.chmod(0o500)
    try:
        written = auth.store_login(NEW, login_meta(), api_url=PROD)
        found = auth.resolve_credential({}, api_url=PROD)
    finally:
        credentials_file.parent.chmod(0o700)

    assert written.source == "keyring"
    assert isinstance(found, auth.LoginCredential)


def test_pending_logins_live_in_the_effective_store(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, credentials_file: Path
) -> None:
    _authorization(monkeypatch)
    start = runner.invoke(app, ["--json", "login", "--store", "file", "--wait", "0"])

    assert start.exit_code == 6
    section = FileStore().section(credential_origin(PROD))
    assert auth.PENDING_DEVICE_ENTRY in section
    assert not backend.entries

    _device(monkeypatch)
    resume = runner.invoke(app, ["--json", "login", "--continue"])
    assert resume.exit_code == 0, resume.stderr
    assert json.loads(resume.stdout)["store"] == "file"
    assert auth.load_pending_device(api_url=PROD) is None
    assert not backend.entries


def test_whoami_and_doctor_point_at_credentials_in_the_store_not_in_use(
    backend: Backend, credentials_file: Path
) -> None:
    from solwyn_cli.commands import doctor as doctor_module

    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: KEY})
    backend.entries[(SERVICE, f"{PROD}/api_key")] = NEW_KEY

    result = runner.invoke(app, ["--json", "whoami"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["auth_source"] == "keyring"
    assert (
        f"This API also has stored credentials in {credentials_file}, which is not "
        "the credential store in use. Run solwyn logout to clear both stores."
    ) in result.stderr
    diagnostic = doctor_module._credential_store_diagnostic()
    assert diagnostic.status == "ok"
    assert f"{credentials_file} also holds credentials for this API" in (
        diagnostic.detail
    )
    assert "solwyn logout" in diagnostic.detail
    assert KEY not in result.stdout + result.stderr + diagnostic.detail


def test_the_unused_store_check_never_probes_the_keyring_when_the_file_is_in_use(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _keyring_login(backend, LOGIN_TOKEN)
    calls: list[str] = []
    real_get = backend.get_password

    def counted(service: str, username: str) -> str | None:
        calls.append(username)
        return real_get(service, username)

    backend.get_password = counted  # type: ignore[method-assign]

    assert auth.unused_store_holdings(api_url=PROD) is None
    assert calls == []


def test_a_clean_store_not_in_use_adds_no_note(
    backend: Backend, credentials_file: Path
) -> None:
    _keyring_login(backend, LOGIN_TOKEN)
    assert auth.unused_store_holdings(api_url=PROD) is None
    FileStore().set_many(credential_origin("https://other.example"), {"api_key": KEY})
    assert auth.unused_store_holdings(api_url=PROD) is None  # Another API's entry.


# --- C. One transaction at a time -------------------------------------------


def test_a_keyring_login_is_one_entry(backend: Backend) -> None:
    auth.store_login(NEW, login_meta(label="one"), mode="keyring", api_url=PROD)

    assert set(backend.entries) == {(SERVICE, f"{PROD}/login")}
    document = json.loads(backend.entries[(SERVICE, f"{PROD}/login")])
    assert document["token"] == NEW and document["meta"]["label"] == "one"


def test_the_lock_times_out_with_a_clear_storage_error(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    import fcntl

    monkeypatch.setattr(credential_store, "LOCK_TIMEOUT_SECONDS", 0.2)
    lock = credential_store.lock_path()
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    # A different open file description, as another process would hold it.
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = runner.invoke(
            app,
            ["--json", "login", "--with-key"],
            input=KEY,
            env={"SOLWYN_CREDENTIAL_STORE": "file"},
        )
    finally:
        os.close(holder)

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert "Another Solwyn process is changing stored credentials" in error["message"]
    assert str(lock) in error["message"]
    assert KEY not in result.stdout + result.stderr


def _worker_setup(entries: Any, family: str = "macos") -> Backend:
    shared = Backend(entries)
    credential_store.backend_factory = lambda: shared
    credential_store.platform_name = lambda platform=None: family
    login_module.sleep = lambda seconds: None
    return shared


def _paired_login(
    entries: Any, ready: Any, release: Any, token: str, label: str
) -> None:
    shared = _worker_setup(entries)
    real_set = shared.set_password

    def paused(service: str, username: str, value: str) -> None:
        real_set(service, username, value)
        if ready is not None:
            ready.set()
            release.wait(10)

    shared.set_password = paused  # type: ignore[method-assign]
    login_module._device_token = lambda *a, **kw: (token, login_meta(label=label))
    result = runner.invoke(app, ["--json", "login"])
    os._exit(result.exit_code)


@forking
def test_concurrent_logins_in_two_processes_keep_token_and_metadata_together(
    config_path: Path, credentials_file: Path
) -> None:
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict()
        ready, release = context.Event(), context.Event()
        first = context.Process(
            target=_paired_login, args=(entries, ready, release, LOGIN_TOKEN, "A")
        )
        first.start()
        assert ready.wait(10)
        second = context.Process(
            target=_paired_login, args=(entries, None, None, NEW, "B")
        )
        second.start()
        second.join(1)
        assert second.is_alive()  # Waiting for the lock first holds.
        release.set()
        first.join(10)
        second.join(10)
        assert first.exitcode == second.exitcode == 0
        document = json.loads(entries[(SERVICE, f"{PROD}/login")])
        expected = {LOGIN_TOKEN: "A", NEW: "B"}[document["token"]]
        assert document["meta"]["label"] == expected
        assert document["token"] == NEW  # The second login, written after.


def _mode_switch_then_pause(entries: Any, ready: Any, release: Any) -> None:
    _worker_setup(entries)
    real_update = config.update_config_values

    def paused(*args: Any, **kwargs: Any) -> Any:
        result = real_update(*args, **kwargs)
        ready.set()  # Config says file; this process still holds the lock.
        release.wait(10)
        return result

    config.update_config_values = paused  # type: ignore[assignment]
    result = runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "file"], input=KEY
    )
    os._exit(result.exit_code)


def _pending_login_process(entries: Any) -> None:
    _worker_setup(entries)
    result = runner.invoke(app, ["--json", "login", "--wait", "0"])
    os._exit(result.exit_code)


@forking
def test_a_pending_login_written_during_a_peer_mode_switch_lands_where_reads_look(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, credentials_file: Path
) -> None:
    # The mode is resolved inside the lock: the peer's switch to the file
    # happened while this write waited, so it must go to the file too.
    _authorization(monkeypatch)
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict()
        ready, release = context.Event(), context.Event()
        mover = context.Process(
            target=_mode_switch_then_pause, args=(entries, ready, release)
        )
        mover.start()
        assert ready.wait(10)
        peer = context.Process(target=_pending_login_process, args=(entries,))
        peer.start()
        peer.join(1)
        assert peer.is_alive()
        release.set()
        mover.join(10)
        peer.join(10)
        assert mover.exitcode == 0
        assert peer.exitcode == 6
        assert not entries  # Nothing went to the keychain.
        shared = Backend(entries)
        monkeypatch.setattr(credential_store, "backend_factory", lambda: shared)
        _platform(monkeypatch, "macos")
        assert auth.load_pending_device(api_url=PROD) is not None
        section = FileStore().section(credential_origin(PROD))
        assert section[auth.API_KEY_ENTRY] == KEY
        assert auth.PENDING_DEVICE_ENTRY in section


def _logout_paused_in_revoke(entries: Any, ready: Any, release: Any) -> None:
    _worker_setup(entries)

    def revoke(api_url: str, token: str) -> bool:
        ready.set()
        release.wait(10)
        return True

    login_module.revoke_token = revoke
    result = runner.invoke(app, ["--json", "logout"])
    os._exit(result.exit_code)


def _keyring_login_process(entries: Any, token: str) -> None:
    _worker_setup(entries)
    login_module._device_token = lambda *a, **kw: (token, login_meta(label="later"))
    result = runner.invoke(app, ["--json", "login"])
    os._exit(result.exit_code)


@forking
def test_a_login_during_logout_waits_and_the_revoked_login_never_returns(
    config_path: Path, credentials_file: Path
) -> None:
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict()
        entries[(SERVICE, f"{PROD}/login")] = _login_document(LOGIN_TOKEN)
        ready, release = context.Event(), context.Event()
        logout = context.Process(
            target=_logout_paused_in_revoke, args=(entries, ready, release)
        )
        logout.start()
        assert ready.wait(10)
        login = context.Process(target=_keyring_login_process, args=(entries, NEW))
        login.start()
        login.join(1)
        assert login.is_alive()
        release.set()
        logout.join(10)
        login.join(10)
        assert logout.exitcode == login.exitcode == 0
        document = json.loads(entries[(SERVICE, f"{PROD}/login")])
        assert document["token"] == NEW
        assert LOGIN_TOKEN not in str(dict(entries))


def _killed_after_mode_switch(entries: Any, ready: Any) -> None:
    _worker_setup(entries)
    real_update = config.update_config_values

    def then_hang(*args: Any, **kwargs: Any) -> Any:
        result = real_update(*args, **kwargs)
        ready.set()
        import time

        time.sleep(30)
        return result

    config.update_config_values = then_hang  # type: ignore[assignment]
    runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "file"], input=NEW_KEY
    )


@forking
def test_a_kill_after_the_mode_switch_leaves_one_consistent_store(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, credentials_file: Path
) -> None:
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict()
        entries[(SERVICE, f"{PROD}/login")] = _login_document(LOGIN_TOKEN)
        ready = context.Event()
        worker = context.Process(
            target=_killed_after_mode_switch, args=(entries, ready)
        )
        worker.start()
        assert ready.wait(10)
        worker.kill()
        worker.join(10)
        assert worker.exitcode == -9
        # The next command reads one store (the file, per config) and the lock
        # died with the process.
        shared = Backend(entries)
        monkeypatch.setattr(credential_store, "backend_factory", lambda: shared)
        _platform(monkeypatch, "macos")
        assert 'credential_store = "file"' in config_path.read_text()
        assert auth.resolve_credential({}, api_url=PROD) is None
        assert auth.where_stored().kind == "file"
        auth.store_project_key(KEY, api_url=PROD)
        assert (SERVICE, f"{PROD}/login") in entries  # Never deleted.
        assert NEW_KEY not in str(dict(entries))  # Nothing half-written anywhere.
        assert (
            not credentials_file.exists() or NEW_KEY not in credentials_file.read_text()
        )


# --- D. absent vs unreachable -----------------------------------------------


def _logout_statuses(result: Any) -> list[str]:
    body = result.stdout if result.exit_code == 0 else result.stderr
    document = json.loads(body)
    stores = (
        document["stores"] if result.exit_code == 0 else document["detail"]["stores"]
    )
    return [store["status"] for store in stores]


def test_a_placeholder_backend_is_absent(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    from keyring.backends.fail import Keyring

    monkeypatch.setattr(credential_store, "backend_factory", Keyring)
    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert _logout_statuses(result) == ["cleared", "absent"]


def test_no_keyring_error_from_a_call_is_absent(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    def no_keyring(*args: Any) -> None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr(backend, "get_password", no_keyring)
    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert _logout_statuses(result) == ["cleared", "absent"]


def test_a_locked_keychain_is_unreachable(backend: Backend) -> None:
    backend.locked = True
    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    assert _logout_statuses(result) == ["cleared", "unreachable"]


@pytest.mark.parametrize(
    ("bus", "failure", "absent"),
    [
        (None, None, True),  # No D-Bus session at all.
        ("unix:path=/run/user/1/bus", "SecretStorage required", True),
        (
            "unix:path=/run/user/1/bus",
            "The Secret Service daemon is neither running nor activatable through D-Bus",
            True,
        ),
        # Discovery failing for any reason is keyring's own fail backend.
        (
            "unix:path=/run/user/1/bus",
            "Unable to initialize SecretService: refused",
            True,
        ),
    ],
)
def test_linux_keyring_discovery_is_classified_by_cause(
    monkeypatch: pytest.MonkeyPatch, bus: str | None, failure: str | None, absent: bool
) -> None:
    from keyring.backends import SecretService, kwallet

    class Unusable:
        @property
        def priority(self) -> float:
            raise RuntimeError(failure)

    class KWalletMissing:
        @property
        def priority(self) -> float:
            raise RuntimeError("python-dbus not installed")

    if bus is None:
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus)
    monkeypatch.setattr(SecretService, "Keyring", Unusable())
    monkeypatch.setattr(kwallet, "DBusKeyring", KWalletMissing())
    _platform(monkeypatch, "linux")

    with pytest.raises(credential_store.StoreUnavailableError) as caught:
        credential_store._platform_backend()

    assert isinstance(caught.value, credential_store.KeyringAbsentError) is absent


def test_a_secret_service_that_is_not_viable_is_absent_like_keyring_resolves(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    # keyring itself resolves its fail backend here (checked on Linux with no
    # bus address, and with an address that has no bus behind it).
    from keyring.backends import SecretService

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    _platform(monkeypatch, "linux")

    def disconnected() -> None:
        raise SecretService.exceptions.SecretStorageException("connection refused")

    monkeypatch.setattr(SecretService.secretstorage, "dbus_init", disconnected)
    monkeypatch.setattr(
        credential_store, "backend_factory", credential_store._platform_backend
    )

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert _logout_statuses(result) == ["cleared", "absent"]


# --- E. Logout never deletes what it could not read -------------------------


def test_logout_keeps_a_keychain_login_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    revoked = _device(monkeypatch)
    _keyring_login(backend, LOGIN_TOKEN)
    backend.entries[(SERVICE, f"{PROD}/api_key")] = KEY
    backend.fail_get.add(f"{PROD}/login")

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "logout_incomplete"
    keyring = error["detail"]["stores"][1]
    assert keyring["status"] == "unread"
    assert keyring["kept"] == ["login"]
    assert revoked == []
    assert (SERVICE, f"{PROD}/login") in backend.entries  # Kept.
    assert (SERVICE, f"{PROD}/api_key") not in backend.entries  # The rest cleared.


def test_logout_keeps_an_unparsable_keychain_login(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    _device(monkeypatch)
    backend.entries[(SERVICE, f"{PROD}/login")] = "{not json"

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["detail"]["stores"][1]["kept"] == ["login"]
    assert (SERVICE, f"{PROD}/login") in backend.entries


def test_logout_keeps_a_malformed_file_whole(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, credentials_file: Path
) -> None:
    _device(monkeypatch)
    credentials_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(credentials_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(descriptor, json.dumps({"version": 1, "origins": {PROD: None}}).encode())
    os.close(descriptor)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    file_report = json.loads(result.stderr)["detail"]["stores"][0]
    assert file_report["status"] == "unread"
    assert credentials_file.exists()


def test_logout_still_clears_a_read_login_whose_revoke_failed(
    monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    _keyring_login(backend, LOGIN_TOKEN)
    monkeypatch.setattr(login_module, "revoke_token", lambda api_url, token: False)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["revoked"] is False
    assert login_module.REVOKE_FAILED_NOTE in result.stderr
    assert (SERVICE, f"{PROD}/login") not in backend.entries


# --- F. Whole-document validation -------------------------------------------


MALFORMED = [
    {"version": 1, "origins": {PROD: None}},
    {"version": 1, "origins": {PROD: []}},
    {"version": 1, "origins": {PROD: "malformed"}},
    {"version": 1, "origins": {PROD: {"login": {"token": 1, "meta": {}}}}},
    {"version": 1, "origins": {PROD: {"api_key": ["x"]}}},
    {"version": 1, "origins": {PROD: {"surprise": "x"}}},
    {"version": 2, "origins": {}},
    [],
]


def _write_raw(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.write(descriptor, content)
    os.close(descriptor)


@pytest.mark.parametrize("document", MALFORMED)
def test_a_malformed_document_is_a_structured_error_naming_the_file(
    credentials_file: Path, document: Any
) -> None:
    _write_raw(credentials_file, json.dumps(document).encode())

    result = runner.invoke(
        app,
        ["--json", "login", "--with-key"],
        input=KEY,
        env={"SOLWYN_CREDENTIAL_STORE": "file"},
    )

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert error["message"] == (
        f"{credentials_file} is not a valid Solwyn credentials file. Remove it, or "
        "move it aside, then run solwyn login."
    )
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("content", [b"{", b"\xff\xfe not utf-8", b"null"])
def test_unparsable_content_is_the_same_structured_error(
    credentials_file: Path, content: bytes
) -> None:
    _write_raw(credentials_file, content)

    with pytest.raises(auth.MalformedCredentialFileError):
        auth.resolve_credential({}, api_url=PROD)


@pytest.mark.parametrize("section", [None, [], "malformed"])
def test_a_malformed_section_never_leaks_a_hidden_stdin_key(
    tmp_path: Path, credentials_file: Path, section: Any
) -> None:
    _write_raw(
        credentials_file,
        json.dumps({"version": 1, "origins": {PROD: section}}).encode(),
    )
    environment = dict(
        os.environ,
        SOLWYN_CREDENTIAL_STORE="file",
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        TERM="dumb",
        COLUMNS="240",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from solwyn_cli.main import app; app()",
            "--json",
            "login",
            "--with-key",
        ],
        input=NEW_KEY,
        text=True,
        capture_output=True,
        env=environment,
        timeout=30,
    )

    assert NEW_KEY not in result.stdout + result.stderr
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"


def test_a_new_browser_login_that_cannot_be_stored_is_revoked(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, credentials_file: Path
) -> None:
    revoked = _device(monkeypatch)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _write_raw(
        credentials_file, json.dumps({"version": 1, "origins": {PROD: None}}).encode()
    )

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert revoked == [NEW]
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"


@posix_only
def test_an_unwritable_lock_file_is_a_structured_error(
    tmp_path: Path, credentials_file: Path
) -> None:
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: KEY})
    credential_store.lock_path().chmod(0o400)
    credentials_file.parent.chmod(0o500)
    environment = dict(
        os.environ,
        SOLWYN_CREDENTIAL_STORE="file",
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        TERM="dumb",
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from solwyn_cli.main import app; app()",
                "--json",
                "login",
                "--with-key",
            ],
            input=NEW_KEY,
            text=True,
            capture_output=True,
            env=environment,
            timeout=30,
        )
    finally:
        credentials_file.parent.chmod(0o700)

    assert NEW_KEY not in result.stdout + result.stderr
    assert result.returncode == 3
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"


# --- G. Intentional secret output is listed ---------------------------------


def test_the_readme_lists_every_command_that_prints_a_credential() -> None:
    posture = (
        Path("README.md").read_text().split("## Trust posture", 1)[1].split("## ", 1)[0]
    )
    for command in (
        "keys env",
        "login --no-store",
        "keys rotate",
        "projects create",
        "costs export",
        "alerts export",
        "attest",
    ):
        assert command in posture, command
