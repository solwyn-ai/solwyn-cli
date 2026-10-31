"""Review r6 regressions: one store per origin, private file reads, logout
across stores, and 0.3.0 migration detection.

Each test drives the CLI or the auth layer the way a user would, against the
real FileStore and an in-memory keyring (tests/conftest.py), with the OS
simulated by pinning the platform family.
"""

import builtins
import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, OTHER_LOGIN_TOKEN, login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import login as login_module
from solwyn_cli.credential_store import FileStore, credential_origin
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
VALID_KEY = "sk_proj_" + "a1" * 32
ISSUED_TOKEN = "solwyn_cli_" + "9a" * 32
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
LOGIN_NOTICE = (
    "Your login from Solwyn CLI 0.3 is still in the system keyring, but it no "
    "longer works. Run solwyn login again."
)
KEY_NOTICE = (
    "A project key saved by Solwyn CLI 0.3 is in the system keyring and is no "
    "longer read. Run solwyn login --with-key again, or set SOLWYN_API_KEY."
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "get_config_path", lambda: tmp_path / "config.toml")
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(login_module, "sleep", lambda seconds: None)


def _platform(monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)


class _Lockable:
    """Make the in-memory keyring behave like a locked keychain on demand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
        self.locked = False
        for name in ("get_password", "set_password", "delete_password"):
            real = getattr(backend, name)

            def guarded(*args: Any, _real: Any = real) -> Any:
                if self.locked:
                    raise RuntimeError("keychain locked")
                return _real(*args)

            monkeypatch.setattr(backend, name, guarded)


def _oauth_server(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Answer the device flow at once and record /oauth/revoke calls."""
    requests: list[httpx.Request] = []
    device = {
        "device_code": "solwyn_dc_" + "4f" * 32,
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://app.solwyn.ai/device",
        "verification_uri_complete": "https://app.solwyn.ai/device?user_code=BCDF-GHJK",
        "expires_in": 600,
        "interval": 5,
    }
    token = {
        "access_token": ISSUED_TOKEN,
        "token_type": "Bearer",
        "expires_in": 2592000,
        "scope": "read contain",
        "solwyn_credential": {
            "id": "6c1f2a90-0000-4000-8000-000000000001",
            "label": "dev-laptop",
            "expires_at": "2099-01-01T00:00:00Z",
            "account_id": "a1",
            "account_name": "Acme",
            "email": "ada@example.com",
            "role": "owner",
        },
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/device_authorization":
            return httpx.Response(200, json=device)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json=token)
        if request.url.path == "/oauth/revoke":
            return httpx.Response(200)
        raise AssertionError(f"unexpected {request.url}")

    transport = httpx.MockTransport(handle)

    def client(*args: Any, **kwargs: Any) -> SolwynClient:
        return SolwynClient(*args, transport=transport, **kwargs)

    monkeypatch.setattr(login_module, "SolwynClient", client)
    real_revoke = login_module.revoke_token

    def revoke(api_url: str, token_value: str) -> bool:
        with httpx.Client(transport=transport) as http:
            http.post(f"{api_url}/oauth/revoke", data={"token": token_value})
        return True

    monkeypatch.setattr(login_module, "revoke_token", revoke)
    assert real_revoke is not None
    return requests


def _revoked_tokens(requests: list[httpx.Request]) -> list[str]:
    return [
        request.content.decode().removeprefix("token=")
        for request in requests
        if request.url.path == "/oauth/revoke"
    ]


# --- Finding 1: one origin lives in exactly one store -----------------------


@pytest.mark.parametrize("family", ["macos", "windows"])
def test_a_login_after_keychain_recovery_is_not_shadowed_by_a_fallback_key(
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: Any,
    credentials_file: Path,
    family: str,
) -> None:
    _platform(monkeypatch, family)
    keychain = _Lockable(monkeypatch, memory_keyring)
    requests = _oauth_server(monkeypatch)

    # The keychain is locked: the project key falls back to the file.
    keychain.locked = True
    with_key = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)
    assert with_key.exit_code == 0, with_key.stderr
    assert json.loads(with_key.stdout)["store"] == "file"

    # The keychain recovers: a browser login goes to the keychain.
    keychain.locked = False
    login = runner.invoke(app, ["--json", "login", "--label", "dev-laptop"])
    assert login.exit_code == 0, login.stderr
    assert json.loads(login.stdout)["store"] == "keyring"

    # The whole origin moved: nothing left in the file, the key kept.
    assert not credentials_file.exists()
    users = {user for _, user in memory_keyring.values}
    assert f"{PROD}/api_key" in users and f"{PROD}/cli_credential" in users

    # User commands use the login, not the full-scope key.
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)
    assert found.bearer_value() == ISSUED_TOKEN
    assert auth.stored_project_key(api_url=PROD) is not None

    # Logout finds and revokes the login.
    logout = runner.invoke(app, ["--json", "logout"])
    assert logout.exit_code == 0, logout.stderr
    assert _revoked_tokens(requests) == [ISSUED_TOKEN]


def test_a_successful_keychain_write_moves_the_origin_out_of_the_file(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    origin = credential_origin(PROD)
    FileStore().set_many(
        origin,
        {
            auth.API_KEY_ENTRY: VALID_KEY,
            auth.CLI_CREDENTIAL_ENTRY: OTHER_LOGIN_TOKEN,
            auth.CLI_CREDENTIAL_META_ENTRY: login_meta().model_dump_json(),
        },
    )

    auth.store_login(LOGIN_TOKEN, login_meta(label="new"), api_url=PROD)

    assert not credentials_file.exists()
    stored = {user: value for (_, user), value in memory_keyring.values.items()}
    assert stored[f"{origin}/api_key"] == VALID_KEY
    assert stored[f"{origin}/cli_credential"] == LOGIN_TOKEN
    assert json.loads(stored[f"{origin}/cli_credential_meta"])["label"] == "new"


def test_a_fallback_write_carries_what_the_keychain_still_answers(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    real_set = memory_keyring.set_password

    def failing_set(service: str, username: str, password: str) -> None:
        if username.endswith("/api_key"):
            raise RuntimeError("write denied")
        real_set(service, username, password)

    monkeypatch.setattr(memory_keyring, "set_password", failing_set)

    auth.store_project_key(VALID_KEY, api_url=PROD)

    section = FileStore().section(credential_origin(PROD))
    assert section[auth.API_KEY_ENTRY] == VALID_KEY
    assert section[auth.CLI_CREDENTIAL_ENTRY] == LOGIN_TOKEN
    assert not any(user.startswith(PROD) for _, user in memory_keyring.values)
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)


# --- Finding 2: the file is read through one checked descriptor -------------


@posix_only
def test_a_file_swapped_between_check_and_open_is_refused(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    # The path keeps passing the privacy check (0600), but every open lands on
    # the attacker's 0644 file: a check-then-open race made deterministic.
    other_key = "sk_proj_" + "b2" * 32
    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)
    attacker = credentials_file.with_name("attacker.json")
    document = json.loads(credentials_file.read_text())
    document["origins"][PROD]["api_key"] = other_key
    attacker.write_text(json.dumps(document))
    attacker.chmod(0o644)
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600
    real_io_open, real_os_open = io.open, os.open

    def redirected(path: Any) -> Any:
        if isinstance(path, (str, os.PathLike)) and Path(path) == credentials_file:
            return attacker
        return path  # Descriptors (os.fdopen) pass through.

    def hooked_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "r" in mode and "+" not in mode:
            file = redirected(file)
        return real_io_open(file, mode, *args, **kwargs)

    def hooked_os_open(path: Any, flags: int, *args: Any) -> int:
        if not flags & (os.O_WRONLY | os.O_RDWR):
            path = redirected(path)
        return real_os_open(path, flags, *args)

    monkeypatch.setattr(io, "open", hooked_io_open)
    monkeypatch.setattr(builtins, "open", hooked_io_open)
    monkeypatch.setattr(credential_store.os, "open", hooked_os_open)

    result = runner.invoke(
        app, ["--json", "keys", "env"], env={"SOLWYN_CREDENTIAL_STORE": "file"}
    )

    assert other_key not in result.stdout + result.stderr
    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert "readable by other users" in error["message"]


@posix_only
def test_a_symlinked_credentials_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.json"
    FileStore(target).set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: VALID_KEY})
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    credentials_file.symlink_to(target)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    with pytest.raises(auth.InsecureCredentialFileError, match="symbolic link"):
        auth.stored_project_key(api_url=PROD)
    with pytest.raises(auth.InsecureCredentialFileError, match="symbolic link"):
        auth.store_project_key(VALID_KEY, api_url=PROD)


@posix_only
@pytest.mark.parametrize("operation", ["read", "write"])
def test_an_existing_directory_we_own_is_tightened_to_0700(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, operation: str
) -> None:
    directory = credentials_file.parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    if operation == "read":
        auth.stored_project_key(api_url=PROD)
    else:
        auth.store_project_key(VALID_KEY, api_url=PROD)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_a_directory_owned_by_another_user_is_refused(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    other_uid = os.getuid() + 1
    monkeypatch.setattr(credential_store.os, "getuid", lambda: other_uid)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    with pytest.raises(auth.InsecureCredentialFileError) as caught:
        auth.store_project_key(VALID_KEY, api_url=PROD)

    assert str(caught.value) == (
        f"The credentials directory {credentials_file.parent} is owned by another "
        "user. Set SOLWYN_CREDENTIALS_FILE to a path in a directory you own."
    )
    assert not credentials_file.exists()


@posix_only
def test_reads_and_writes_never_follow_symlinks(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    flags_seen: list[int] = []
    real_open = os.open

    def recording(path: Any, flags: int, *args: Any) -> int:
        if Path(path).parent == credentials_file.parent and ".lock" not in str(path):
            flags_seen.append(flags)
        return real_open(path, flags, *args)

    monkeypatch.setattr(credential_store.os, "open", recording)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    auth.store_project_key(VALID_KEY, api_url=PROD)
    auth.stored_project_key(api_url=PROD)

    assert flags_seen
    assert all(flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC for flags in flags_seen)


def test_undecodable_file_content_is_no_credential_and_never_a_traceback(
    credentials_file: Path,
) -> None:
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(credentials_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(descriptor, b"\xff\xfe\x00 not utf-8 " + VALID_KEY.encode())
    os.close(descriptor)

    result = runner.invoke(
        app,
        ["--json", "login", "--with-key"],
        input=VALID_KEY,
        env={"SOLWYN_CREDENTIAL_STORE": "file"},
    )

    assert result.exit_code == 0, result.stderr
    assert VALID_KEY not in result.stdout + result.stderr
    assert json.loads(credentials_file.read_text())["version"] == 1


# --- Finding 4: logout looks in both stores ---------------------------------


@pytest.mark.parametrize("family", ["linux", "macos"])
def test_logout_in_file_mode_revokes_and_clears_a_keychain_login(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, family: str
) -> None:
    _platform(monkeypatch, family)
    requests = _oauth_server(monkeypatch)
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="keyring", api_url=PROD)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert _revoked_tokens(requests) == [LOGIN_TOKEN]
    assert not any(user.startswith(PROD) for _, user in memory_keyring.values)
    data = json.loads(result.stdout)
    assert data["cleared"] is True
    assert data["logins_found"] == 1
    # Restoring auto afterwards never resurrects the login.
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "auto")
    assert auth.resolve_credential({}, api_url=PROD) is None


def test_logout_revokes_a_login_in_each_store(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    requests = _oauth_server(monkeypatch)
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="keyring", api_url=PROD)
    FileStore().set_many(
        credential_origin(PROD),
        {
            auth.CLI_CREDENTIAL_ENTRY: OTHER_LOGIN_TOKEN,
            auth.CLI_CREDENTIAL_META_ENTRY: login_meta().model_dump_json(),
        },
    )

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert sorted(_revoked_tokens(requests)) == sorted([LOGIN_TOKEN, OTHER_LOGIN_TOKEN])
    assert json.loads(result.stdout)["logins_found"] == 2
    assert not credentials_file.exists()


def test_logout_with_an_unreachable_keychain_reports_what_it_kept_and_fails(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    _oauth_server(monkeypatch)
    keychain = _Lockable(monkeypatch, memory_keyring)
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="keyring", api_url=PROD)
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: VALID_KEY})
    keychain.locked = True

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "logout_incomplete"
    assert "the macOS Keychain" in error["message"]
    detail = error["detail"]
    assert detail["cleared"] is False
    assert [store["status"] for store in detail["stores"]] == ["cleared", "unreachable"]
    # The file was still cleared for this API; the keychain login was kept.
    assert FileStore().section(credential_origin(PROD)) == {}
    keychain.locked = False
    assert auth.stored_login(api_url=PROD) is not None


def test_logout_where_no_keyring_exists_succeeds(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "linux")
    auth.store_project_key(VALID_KEY, api_url=PROD)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    stores = json.loads(result.stdout)["stores"]
    assert [store["status"] for store in stores] == ["cleared", "absent"]
    assert not credentials_file.exists()


# --- Finding 5: 0.3.0 keyring entries ---------------------------------------


@pytest.mark.parametrize(
    ("entries", "notice"),
    [
        (("access_token", "refresh_token"), LOGIN_NOTICE),
        (("refresh_token",), LOGIN_NOTICE),
        (("api_key",), KEY_NOTICE),
        (
            ("access_token", "refresh_token", "api_key"),
            f"{LOGIN_NOTICE} {KEY_NOTICE}",
        ),
    ],
)
@pytest.mark.parametrize("family", ["linux", "macos", "windows"])
def test_a_terminal_user_is_told_about_real_0_3_keyring_entries(
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: Any,
    family: str,
    entries: tuple[str, ...],
    notice: str,
) -> None:
    _platform(monkeypatch, family)
    monkeypatch.setattr(auth, "_interactive", lambda: True)
    for entry in entries:
        memory_keyring.values[("solwyn-cli", entry)] = "eyJ.legacy." + entry

    result = runner.invoke(app, ["--json", "projects", "list"])

    assert result.exit_code == 3
    assert notice in result.stderr
    assert "SOLWYN_CREDENTIAL_STORE=keyring" not in result.stderr
    # Detected, never used: no request was authenticated with them.
    assert "eyJ.legacy" not in result.stdout + result.stderr
    assert auth.resolve_credential({}, api_url=PROD) is None


def test_the_legacy_notices_are_the_cli_owned_text() -> None:
    assert auth.LEGACY_LOGIN_NOTICE == LOGIN_NOTICE
    assert auth.LEGACY_KEY_NOTICE == KEY_NOTICE


@pytest.mark.parametrize("mode", ["file", "auto"])
def test_no_terminal_or_file_mode_never_probes_for_legacy_entries(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", mode)
    monkeypatch.setattr(auth, "_interactive", lambda: mode == "file")

    def touched() -> Any:
        pytest.fail("the keyring was probed")

    monkeypatch.setattr(credential_store, "backend_factory", touched)

    assert auth.legacy_login_notice() is None


@pytest.mark.parametrize("mode", ["file", "auto", "keyring"])
def test_logout_deletes_0_3_keyring_entries_in_every_mode(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, mode: str
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", mode)
    for entry in ("access_token", "refresh_token", "api_key"):
        memory_keyring.values[("solwyn-cli", entry)] = "legacy"

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert memory_keyring.values == {}


# --- Items 7 to 9: pending logins and other-store cleanup scope --------------


@pytest.mark.parametrize("family", ["macos", "windows"])
def test_wait_0_then_continue_works_when_the_file_holds_the_origin(
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: Any,
    credentials_file: Path,
    family: str,
) -> None:
    _platform(monkeypatch, family)
    keychain = _Lockable(monkeypatch, memory_keyring)
    _oauth_server(monkeypatch)
    # An earlier locked-keychain fallback left this API in the file.
    keychain.locked = True
    with_key = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)
    assert json.loads(with_key.stdout)["store"] == "file"
    keychain.locked = False  # The keychain has recovered.

    start = runner.invoke(app, ["--json", "login", "--wait", "0"])

    assert start.exit_code == 6, start.stderr
    # The pending login follows the origin into the file reads use.
    section = FileStore().section(credential_origin(PROD))
    assert auth.PENDING_DEVICE_ENTRY in section
    assert not any(
        user.endswith("/pending_device") for _, user in memory_keyring.values
    )
    assert "Could not use" not in start.stderr  # Routing, not a fallback.

    resume = runner.invoke(app, ["--json", "login", "--continue"])

    assert resume.exit_code == 0, resume.stderr
    assert json.loads(resume.stdout)["store"] == "keyring"
    assert not credentials_file.exists()
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)
    assert auth.stored_project_key(api_url=PROD) is not None


@pytest.mark.parametrize(
    ("family", "name"),
    [("macos", "the macOS Keychain"), ("windows", "Windows Credential Manager")],
)
def test_a_pending_login_that_falls_back_to_the_file_warns(
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: Any,
    credentials_file: Path,
    family: str,
    name: str,
) -> None:
    _platform(monkeypatch, family)
    keychain = _Lockable(monkeypatch, memory_keyring)
    _oauth_server(monkeypatch)
    keychain.locked = True

    start = runner.invoke(app, ["--json", "login", "--wait", "0"])

    assert start.exit_code == 6
    assert json.loads(start.stderr.splitlines()[-1])["code"] == "login_pending"
    assert (
        f"Could not use {name} (it may be locked, or unavailable over SSH). "
        f"Saved the pending login to {credentials_file} instead."
    ) in start.stderr
    assert "solwyn_dc_" not in start.stderr
    assert auth.PENDING_DEVICE_ENTRY in FileStore().section(credential_origin(PROD))

    keychain.locked = False
    resume = runner.invoke(app, ["--json", "login", "--continue"])
    assert resume.exit_code == 0, resume.stderr


def test_a_failed_file_cleanup_after_a_keychain_write_warns_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: VALID_KEY})

    def failing_delete(self: FileStore, origin: str, entries: Any) -> None:
        raise credential_store.StoreUnavailableError("disk full")

    monkeypatch.setattr(FileStore, "delete", failing_delete)

    result = runner.invoke(
        app, ["--json", "login", "--with-key"], input="sk_proj_" + "b2" * 32
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["store"] == "keyring"
    assert (
        f"Could not remove the earlier copy in {credentials_file}, which is read "
        "before the keychain. Delete it, or run solwyn logout to clear both stores."
    ) in result.stderr


def test_a_failed_keychain_cleanup_after_a_fallback_warns(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)

    def refuse(*args: Any) -> None:
        raise RuntimeError("write denied")

    monkeypatch.setattr(memory_keyring, "set_password", refuse)
    monkeypatch.setattr(memory_keyring, "delete_password", refuse)

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)

    assert result.exit_code == 0, result.stderr
    assert "Could not remove the earlier copy in the macOS Keychain." in result.stderr
    found = auth.resolve_credential({}, api_url=PROD)
    assert isinstance(found, auth.LoginCredential)  # Carried into the file.


@pytest.mark.parametrize("interactive", [True, False])
def test_linux_auto_login_never_touches_the_keyring(
    monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    _platform(monkeypatch, "linux")
    _oauth_server(monkeypatch)
    monkeypatch.setattr(auth, "_interactive", lambda: interactive)

    def touched() -> Any:
        pytest.fail("a Linux auto write touched the keyring")

    monkeypatch.setattr(credential_store, "backend_factory", touched)

    with_key = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)
    start = runner.invoke(app, ["--json", "login", "--wait", "0"])
    resume = runner.invoke(app, ["--json", "login", "--continue"])

    assert with_key.exit_code == 0, with_key.stderr
    assert start.exit_code == 6, start.stderr
    assert resume.exit_code == 0, resume.stderr
