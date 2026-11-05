"""Review r8 findings 4 to 10: reads under the lock, keyring timeouts that
strand the process, one stable lock path, refused deletes, logout ordering and
complete payload validation.

Adapted from the reviewer's independent probes. The keyring is an in-memory
backend shared with forked processes; the OS is simulated by pinning the
platform family.
"""

import json
import multiprocessing as mp
import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from key_fixtures import (
    PROJECT_ID,
    key_document,
    key_index,
    key_item,
    store_key,
    stored_keychain,
)
from keyring.errors import PasswordDeleteError
from login_fixtures import LOGIN_TOKEN, login_meta
from typer.testing import CliRunner

from solwyn_cli import auth, config, credential_store
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import login as login_module
from solwyn_cli.credential_store import FileStore, credential_origin
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
KEY = "solwyn_sk_" + "a1" * 32
NEW_KEY = "solwyn_sk_" + "b2" * 32
NEW = "solwyn_cli_" + "2b" * 32
DEVICE_CODE = "solwyn_dc_" + "4d" * 32
SERVICE = "solwyn-cli"
# Captured at import, before tests/conftest.py points the lock at a temp dir.
UNPATCHED_LOCK_DIRECTORY = getattr(credential_store, "lock_directory", None)
forking = pytest.mark.skipif(
    "fork" not in mp.get_all_start_methods(), reason="needs fork"
)
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX locks")


class Backend:
    def __init__(self, entries: Any = None) -> None:
        self.entries = {} if entries is None else entries
        self.fail_get: set[str] = set()

    def get_password(self, service: str, username: str) -> str | None:
        if username in self.fail_get:
            raise RuntimeError("read failure")
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.entries[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise PasswordDeleteError("already missing")
        del self.entries[(service, username)]


def _document(token: str = LOGIN_TOKEN) -> str:
    return json.dumps({"token": token, "meta": login_meta().model_dump(mode="json")})


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Backend, list[str]]:
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "get_config_path", lambda: tmp_path / "config.toml")
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
    return backend, revoked


def _shared_backend(entries: Any) -> Backend:
    backend = Backend(entries)
    credential_store.backend_factory = lambda: backend
    return backend


# --- 4. Reads run under the lock -------------------------------------------


def _hold_exclusive(ready: Any, release: Any) -> None:
    with credential_store.credential_lock():
        ready.set()
        release.wait(8)
    os._exit(0)


def _hold_shared(ready: Any, release: Any) -> None:
    with credential_store.credential_lock(shared=True):
        ready.set()
        release.wait(8)
    os._exit(0)


def _resolve(queue: Any) -> None:
    found = auth.resolve_credential({}, api_url=PROD, project_id=PROJECT_ID)
    queue.put(type(found).__name__)  # Return normally: the queue must flush.


@forking
@posix_only
def test_a_read_waits_while_another_process_writes(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(credential_store.STORE_ENV, "file")
    store_key(key=KEY, api_url=PROD)
    context = mp.get_context("fork")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    holder = context.Process(target=_hold_exclusive, args=(ready, release))
    holder.start()
    assert ready.wait(5)
    reader = context.Process(target=_resolve, args=(queue,))
    reader.start()
    reader.join(0.5)
    assert reader.is_alive()  # Waiting for the writer.
    release.set()
    holder.join(8)
    reader.join(8)
    assert queue.get(timeout=3) == "ApiKeyCredential"


@forking
@posix_only
def test_reads_share_the_lock(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(credential_store.STORE_ENV, "file")
    store_key(key=KEY, api_url=PROD)
    context = mp.get_context("fork")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    holder = context.Process(target=_hold_shared, args=(ready, release))
    holder.start()
    assert ready.wait(5)
    reader = context.Process(target=_resolve, args=(queue,))
    reader.start()
    reader.join(5)
    assert not reader.is_alive()  # Another reader does not block it.
    release.set()
    holder.join(8)
    assert queue.get(timeout=3) == "ApiKeyCredential"


def _split_reader(entries: Any, ready: Any, release: Any, queue: Any) -> None:
    _shared_backend(entries)
    real_read = auth._read
    seen: list[Any] = []

    def paused(names: tuple[str, ...], **kwargs: Any) -> Any:
        found = real_read(names, **kwargs)
        seen.append(found.kind)
        if names == (credential_store.LOGIN_ENTRY,):
            ready.set()
            release.wait(8)
        return found

    auth._read = paused  # type: ignore[assignment]
    found = auth.resolve_credential({}, api_url=PROD, project_id=PROJECT_ID)
    queue.put((seen, type(found).__name__))


def _switch_and_login(entries: Any) -> None:
    _shared_backend(entries)
    auth.save_store_mode("file")
    auth.store_login(NEW, login_meta(label="new"), api_url=PROD)
    os._exit(0)


@forking
@posix_only
def test_one_resolution_reads_one_store(env: Any) -> None:
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict(stored_keychain(PROD, key=KEY))
        ready, release, queue = context.Event(), context.Event(), context.Queue()
        reader = context.Process(
            target=_split_reader, args=(entries, ready, release, queue)
        )
        reader.start()
        assert ready.wait(5)
        writer = context.Process(target=_switch_and_login, args=(entries,))
        writer.start()
        writer.join(0.5)
        assert writer.is_alive()  # The switch waits for the read to finish.
        release.set()
        reader.join(8)
        writer.join(8)
        assert reader.exitcode == writer.exitcode == 0
        seen, kind = queue.get(timeout=3)
        assert len(set(seen)) == 1, seen
        assert kind == "ApiKeyCredential"


# --- 5. A timed-out keyring write strands the process -----------------------


def _slow_set(monkeypatch: pytest.MonkeyPatch, backend: Backend) -> threading.Event:
    release = threading.Event()
    real_set = backend.set_password

    def slow(service: str, username: str, value: str) -> None:
        release.wait(5)
        real_set(service, username, value)

    monkeypatch.setattr(backend, "set_password", slow)
    monkeypatch.setattr(credential_store, "KEYRING_TIMEOUT_SECONDS", 0.2)
    return release


def test_a_timed_out_write_fails_and_no_later_write_in_the_process_continues(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = env
    release = _slow_set(monkeypatch, backend)
    try:
        with pytest.raises(auth.CredentialStorageError) as first:
            store_key(key=KEY, mode="keyring", api_url=PROD)
        assert "did not answer" in str(first.value)
        assert credential_store.stranded()
        # No code path continues: the next transaction fails at once.
        with pytest.raises(auth.CredentialStorageError) as second:
            store_key(key=NEW_KEY, mode="keyring", api_url=PROD)
        assert str(second.value) == credential_store.STRANDED_MESSAGE
        with pytest.raises(auth.CredentialStorageError):
            auth.logout_everywhere(lambda token: True, api_url=PROD)
    finally:
        release.set()


def test_a_timed_out_write_never_falls_back_in_auto(
    env: Any, monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    backend, _ = env
    release = _slow_set(monkeypatch, backend)
    try:
        with pytest.raises(auth.CredentialStorageError):
            store_key(key=KEY, api_url=PROD)
    finally:
        release.set()
    assert not credentials_file.exists()
    assert config.load_config().credential_store == "auto"


@posix_only
def test_a_stranded_process_keeps_the_lock_until_it_exits(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    backend, _ = env
    release = _slow_set(monkeypatch, backend)
    try:
        with pytest.raises(auth.CredentialStorageError):
            store_key(key=KEY, mode="keyring", api_url=PROD)
        probe = os.open(credential_store.lock_path(), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        release.set()


def _waiting_login(entries: Any, polling: Any, written: Any) -> None:
    backend = _shared_backend(entries)
    real_set = backend.set_password

    def late(service: str, username: str, value: str) -> None:
        if username.endswith("/pending_device"):
            # Lands long after the timeout, if the process is still alive. (A
            # plain sleep: a dead process cannot wake an mp.Event waiter.)
            import time

            time.sleep(2)
            real_set(service, username, value)
            written.set()
            return
        real_set(service, username, value)

    backend.set_password = late  # type: ignore[method-assign]
    credential_store.KEYRING_TIMEOUT_SECONDS = 0.2

    def browser_wait(*args: Any, **kwargs: Any) -> None:
        polling.set()  # Only reached if the CLI kept going after the timeout.
        threading.Event().wait(12)
        raise KeyboardInterrupt

    login_module._poll = browser_wait  # type: ignore[assignment]
    result = runner.invoke(app, ["--json", "login"])
    os._exit(result.exit_code)


def _fake_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/device_authorization":
            return httpx.Response(
                200,
                json={
                    "device_code": DEVICE_CODE,
                    "user_code": "BCDF-GHJK",
                    "verification_uri": "https://app.solwyn.ai/device",
                    "verification_uri_complete": (
                        "https://app.solwyn.ai/device?user_code=BCDF-GHJK"
                    ),
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        raise AssertionError(str(request.url))

    monkeypatch.setattr(
        login_module,
        "SolwynClient",
        lambda *a, **kw: SolwynClient(*a, transport=httpx.MockTransport(handle), **kw),
    )


@forking
@posix_only
def test_a_waiting_cli_cannot_resurrect_pending_state_after_a_peer_logout(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_oauth(monkeypatch)
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict()
        monkeypatch.setattr(
            credential_store, "backend_factory", lambda: Backend(entries)
        )
        polling, written = context.Event(), context.Event()
        login = context.Process(target=_waiting_login, args=(entries, polling, written))
        login.start()
        try:
            login.join(8)
            # The pending write timed out: the CLI failed (exit 3) instead of
            # waiting for the browser while its worker could still write.
            assert login.exitcode == 3
            assert not polling.is_set()
            logout = runner.invoke(app, ["--json", "logout"])
            assert logout.exit_code == 0, logout.stderr
            assert not written.wait(3)  # The worker died with its process.
            assert auth.load_pending_device(api_url=PROD) is None
        finally:
            if login.is_alive():
                login.kill()
            login.join(5)


def test_an_mcp_server_exits_after_answering_once_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solwyn_cli.mcp import server

    scheduled: list[tuple[float, Any, Any]] = []

    class Timer:
        def __init__(self, delay: float, function: Any, args: Any) -> None:
            scheduled.append((delay, function, args))
            self.daemon = False

        def start(self) -> None:
            pass

    monkeypatch.setattr(server.threading, "Timer", Timer)
    server._exit_if_stranded()
    assert scheduled == []
    monkeypatch.setattr(credential_store, "_STRANDED", True)
    server._exit_if_stranded()
    assert scheduled == [(1.0, os._exit, (1,))]


# --- 6. One stable lock per user --------------------------------------------


def test_the_lock_path_ignores_the_credentials_file_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = credential_store.lock_path()
    monkeypatch.setenv(credential_store.FILE_ENV, str(tmp_path / "x" / "c.json"))
    assert credential_store.lock_path() == first


def test_the_lock_lives_in_the_per_user_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def user_state_dir(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        return "/state/solwyn"

    monkeypatch.setattr(credential_store, "user_state_dir", user_state_dir)

    assert UNPATCHED_LOCK_DIRECTORY() == Path("/state/solwyn")
    assert calls == [(("solwyn",), {"appauthor": False, "roaming": False})]


def _logout_paused(entries: Any, ready: Any, release: Any) -> None:
    _shared_backend(entries)

    def revoke(token: str) -> bool:
        ready.set()
        release.wait(8)
        return True

    auth.logout_everywhere(revoke, api_url=PROD)
    os._exit(0)


def _write_with_other_file(entries: Any, path: str) -> None:
    os.environ[credential_store.FILE_ENV] = path
    _shared_backend(entries)
    store_key(key=NEW_KEY, mode="keyring", api_url=PROD)
    os._exit(0)


@forking
@posix_only
def test_a_different_credentials_file_still_uses_the_same_lock(
    env: Any, tmp_path: Path
) -> None:
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict({(SERVICE, f"{PROD}/login"): _document()})
        ready, release = context.Event(), context.Event()
        logout = context.Process(target=_logout_paused, args=(entries, ready, release))
        logout.start()
        assert ready.wait(5)
        writer = context.Process(
            target=_write_with_other_file,
            args=(entries, str(tmp_path / "other" / "credentials.json")),
        )
        writer.start()
        writer.join(0.5)
        assert writer.is_alive()  # Same keychain, same lock.
        release.set()
        logout.join(8)
        writer.join(8)
        assert logout.exitcode == writer.exitcode == 0


# --- 7. A delete error is not proof of absence ------------------------------


def test_a_refused_delete_keeps_the_entry_and_fails_logout(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = env
    backend.entries.update(stored_keychain(PROD, key=KEY))

    def denied(service: str, username: str) -> None:
        raise PasswordDeleteError("permission denied")  # macOS maps denial here.

    monkeypatch.setattr(backend, "delete_password", denied)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    keyring = json.loads(result.stderr)["detail"]["stores"][1]
    assert keyring["status"] == "unreachable"
    assert "api_keys" in keyring["kept"]
    assert (SERVICE, key_item(PROD)) in backend.entries
    assert (SERVICE, key_index(PROD)) in backend.entries


def test_a_delete_error_for_a_missing_entry_is_fine(env: Any) -> None:
    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["cleared"] is True


# --- 9. Logout revokes before it deletes ------------------------------------


@pytest.mark.parametrize("with_file", [False, True])
def test_logout_keeps_tokens_stored_until_revoke_is_attempted(
    env: Any, with_file: bool
) -> None:
    backend, _ = env
    backend.entries[(SERVICE, f"{PROD}/login")] = _document()
    if with_file:
        FileStore().set_many(
            credential_origin(PROD), {credential_store.LOGIN_ENTRY: _document()}
        )
    seen: list[bool] = []

    def revoke(token: str) -> bool:
        seen.append(
            (SERVICE, f"{PROD}/login") in backend.entries
            and (not with_file or FileStore().get(PROD, "login") is not None)
        )
        return True

    auth.logout_everywhere(revoke, api_url=PROD)

    assert seen == [True]
    assert (SERVICE, f"{PROD}/login") not in backend.entries


def _killed_during_revoke(entries: Any, started: Any) -> None:
    _shared_backend(entries)

    def stuck(token: str) -> bool:
        started.set()
        threading.Event().wait(15)
        return True

    auth.logout_everywhere(stuck, api_url=PROD)


@forking
@posix_only
def test_a_logout_killed_before_revoking_leaves_the_token_stored(env: Any) -> None:
    FileStore().set_many(
        credential_origin(PROD), {credential_store.LOGIN_ENTRY: _document()}
    )
    context = mp.get_context("fork")
    with context.Manager() as manager:
        entries = manager.dict({(SERVICE, f"{PROD}/login"): _document()})
        started = context.Event()
        worker = context.Process(target=_killed_during_revoke, args=(entries, started))
        worker.start()
        try:
            assert started.wait(5)
        finally:
            worker.kill()
            worker.join(5)
        assert worker.exitcode == -9
        assert (SERVICE, f"{PROD}/login") in entries
        assert FileStore().get(PROD, credential_store.LOGIN_ENTRY) is not None


# --- 10. Complete payloads, never a silent fallthrough ----------------------


@pytest.mark.parametrize(
    "section",
    [
        {
            "login": {"token": LOGIN_TOKEN, "meta": {}},
            "api_keys": {PROJECT_ID: key_document(key=KEY)},
        },
        {"login": {"token": LOGIN_TOKEN, "meta": {"id": 1}}},
        {
            "login": {
                "token": "solwyn_cli_short",
                "meta": login_meta().model_dump(mode="json"),
            }
        },
        {"pending_device": {"device_code": DEVICE_CODE}},
        {"login": None, "api_keys": {PROJECT_ID: key_document(key=KEY)}},
        {"api_keys": None},
        {"api_keys": {PROJECT_ID: key_document(key="sk_proj_short")}},
        {"api_keys": {PROJECT_ID: key_document(key="sk_proj_" + "a1" * 32)}},
        {"api_keys": {PROJECT_ID: {**key_document(key=KEY), "scope": "full"}}},
        {"api_keys": {PROJECT_ID: {"key": KEY}}},
        {"api_keys": {"checkout-bot": key_document(key=KEY)}},
        {"api_keys": {PROJECT_ID: key_document(key=KEY, stored_at="yesterday")}},
        # The 0.4.x slot is never valid in a version 2 file.
        {"api_key": KEY},
    ],
)
def test_the_file_validates_every_credential_payload(
    env: Any,
    monkeypatch: pytest.MonkeyPatch,
    credentials_file: Path,
    section: dict[str, Any],
) -> None:
    monkeypatch.setenv(credential_store.STORE_ENV, "file")
    credentials_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(credentials_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(
        descriptor, json.dumps({"version": 2, "origins": {PROD: section}}).encode()
    )
    os.close(descriptor)

    with pytest.raises(auth.MalformedCredentialFileError):
        auth.resolve_credential({}, api_url=PROD, project_id=PROJECT_ID)


def test_a_malformed_keychain_login_never_falls_through_to_the_key(
    env: Any,
) -> None:
    backend, _ = env
    backend.entries[(SERVICE, f"{PROD}/login")] = json.dumps(
        {"token": LOGIN_TOKEN, "meta": {}}
    )
    backend.entries.update(stored_keychain(PROD, key=KEY))

    with pytest.raises(auth.MalformedCredentialFileError) as caught:
        auth.resolve_credential({}, api_url=PROD)

    assert f"{PROD}/login" in str(caught.value)
    assert LOGIN_TOKEN not in str(caught.value)
    result = runner.invoke(app, ["--json", "whoami"])
    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"
    assert KEY not in result.stdout + result.stderr


# --- 8. Absence is decided by the resolved backend --------------------------


def test_a_resolved_secret_service_without_its_bus_is_unreachable_at_logout(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from keyring.backends import SecretService

    backend, _ = env

    class Viable:
        priority = 1

        def __new__(cls) -> Backend:
            return backend

    monkeypatch.setattr(SecretService, "Keyring", Viable)
    monkeypatch.setattr(
        credential_store, "platform_name", lambda platform=None: "linux"
    )
    monkeypatch.setattr(
        credential_store, "backend_factory", credential_store._platform_backend
    )
    monkeypatch.setenv(credential_store.STORE_ENV, "keyring")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS")

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)


def test_keyring_resolves_its_fail_backend_without_a_session_bus() -> None:
    # What a headless Linux CI sees; logged in
    # .cache/agent-first/logs/cli-48-r9/keyring-resolution-linux.log.
    import subprocess
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux discovery")
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import keyring; print(type(keyring.get_keyring()).__module__)",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.stdout.strip() == "keyring.backends.fail"
