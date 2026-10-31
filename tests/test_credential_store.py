"""Where credentials are stored: per OS, per mode, per API origin.

These run against the real FileStore and an in-memory keyring backend
(tests/conftest.py); the OS is simulated by pinning the platform family.
"""

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from login_fixtures import LOGIN_TOKEN, OTHER_LOGIN_TOKEN, login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.commands import _shared
from solwyn_cli.credential_store import (
    FileStore,
    InsecureFileError,
    KeyringStore,
    StoreUnavailableError,
    credential_origin,
)
from solwyn_cli.main import app

runner = CliRunner()
VALID_KEY = "sk_proj_" + "a1" * 32
OTHER_KEY = "sk_proj_" + "b2" * 32
PROD = "https://api.solwyn.ai"
LOCAL = "http://localhost:8080"
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL"):
        monkeypatch.delenv(name, raising=False)
    return path


def _platform(monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)


def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def touched() -> Any:
        pytest.fail("the keyring was touched")

    monkeypatch.setattr(credential_store, "backend_factory", touched)


def _read_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# --- Origins ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://api.solwyn.ai", "https://api.solwyn.ai"),
        ("https://API.Solwyn.ai/", "https://api.solwyn.ai"),
        ("https://api.solwyn.ai:443", "https://api.solwyn.ai"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://localhost:80/", "http://localhost"),
        ("https://localhost:8080", "https://localhost:8080"),
        ("https://gw.example/solwyn/", "https://gw.example/solwyn"),
        ("http://[::1]:9000", "http://[::1]:9000"),
    ],
)
def test_origin_normalizes_scheme_host_port_and_path(url: str, origin: str) -> None:
    assert credential_origin(url) == origin


def test_a_localhost_login_is_never_used_for_production(credentials_file: Path) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=LOCAL)

    assert auth.resolve_credential({}, api_url=LOCAL) is not None
    assert auth.resolve_credential({}, api_url=PROD) is None
    assert set(_read_file(credentials_file)["origins"]) == {LOCAL}


def test_each_api_keeps_its_own_login_and_key(credentials_file: Path) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=LOCAL)
    auth.store_login(OTHER_LOGIN_TOKEN, login_meta(), mode="file", api_url=PROD)
    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)

    local = auth.resolve_credential({}, api_url=LOCAL)
    prod = auth.resolve_credential({}, api_url=PROD)
    assert local is not None and local.bearer_value() == LOGIN_TOKEN
    assert prod is not None and prod.bearer_value() == OTHER_LOGIN_TOKEN
    assert auth.resolve_credential({}, purpose="metering", api_url=LOCAL) is None

    auth.clear_credentials(api_url=LOCAL)
    assert set(_read_file(credentials_file)["origins"]) == {PROD}


def test_cli_api_url_selects_which_stored_key_is_used(
    credentials_file: Path,
) -> None:
    stored = runner.invoke(
        app,
        ["--json", "--api-url", LOCAL, "login", "--with-key", "--store", "file"],
        input=VALID_KEY,
    )
    assert stored.exit_code == 0, stored.stderr

    local = runner.invoke(app, ["--json", "--api-url", LOCAL, "keys", "env"])
    prod = runner.invoke(app, ["--json", "--api-url", PROD, "keys", "env"])

    assert local.exit_code == 0, local.stderr
    assert VALID_KEY in local.stdout
    assert prod.exit_code == 3
    assert VALID_KEY not in prod.stdout + prod.stderr


# --- The file ---------------------------------------------------------------


def test_default_path_is_the_non_roaming_user_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def user_data_dir(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        return "/data/solwyn"

    monkeypatch.delenv(credential_store.FILE_ENV)
    monkeypatch.setattr(credential_store, "user_data_dir", user_data_dir)

    assert credential_store.credentials_path() == Path("/data/solwyn/credentials.json")
    # roaming=False: %LOCALAPPDATA% on Windows, never the roaming %APPDATA%.
    assert calls == [(("solwyn",), {"appauthor": False, "roaming": False})]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="XDG on Linux")
def test_linux_default_path_follows_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(credential_store.FILE_ENV)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert credential_store.credentials_path() == (
        tmp_path / "data" / "solwyn" / "credentials.json"
    )


def test_a_relative_override_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(credential_store.FILE_ENV, "relative/credentials.json")

    with pytest.raises(StoreUnavailableError, match="absolute path"):
        credential_store.credentials_path()


@posix_only
def test_file_and_directory_are_private_and_versioned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "solwyn" / "credentials.json"
    monkeypatch.setenv(credential_store.FILE_ENV, str(path))

    auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=PROD)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = _read_file(path)
    assert document["version"] == 1
    login = document["origins"][PROD]
    assert login["cli_credential"] == LOGIN_TOKEN
    assert login["cli_credential_meta"]["label"] == "dev-laptop"


@posix_only
def test_writes_are_atomic_with_0600_from_creation(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    opened: list[tuple[int, int]] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, mode: int = 0o777) -> int:
        if str(path).endswith(".tmp"):
            opened.append((flags, mode))
        return real_open(path, flags, mode)

    monkeypatch.setattr(credential_store.os, "open", recording_open)

    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)

    assert len(opened) == 1
    flags, mode = opened[0]
    assert flags & os.O_EXCL and flags & os.O_CREAT
    assert mode == 0o600
    assert [p.name for p in credentials_file.parent.iterdir() if ".tmp" in p.name] == []


def test_a_failed_replace_keeps_the_previous_file_intact(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)
    before = credentials_file.read_bytes()

    def failing_replace(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(credential_store.os, "replace", failing_replace)

    with pytest.raises(auth.CredentialStorageError):
        auth.store_project_key(OTHER_KEY, mode="file", api_url=PROD)

    assert credentials_file.read_bytes() == before
    assert [p for p in credentials_file.parent.iterdir() if ".tmp" in p.name] == []


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_file_other_users_can_read_is_refused(
    credentials_file: Path, mode: int
) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=PROD)
    credentials_file.chmod(mode)

    with pytest.raises(auth.InsecureCredentialFileError) as caught:
        auth.resolve_credential({}, api_url=PROD)

    message = str(caught.value)
    assert message == (
        f"{credentials_file} is readable by other users. Run chmod 600 {credentials_file}."
    )
    assert LOGIN_TOKEN not in message


@posix_only
def test_a_file_owned_by_another_user_is_refused(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)
    other_uid = os.getuid() + 1
    monkeypatch.setattr(credential_store.os, "getuid", lambda: other_uid)

    with pytest.raises(InsecureFileError, match="owned by another user"):
        FileStore(credentials_file).get(credential_origin(PROD), auth.API_KEY_ENTRY)


@posix_only
def test_cli_refuses_a_readable_file_loudly(credentials_file: Path) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=PROD)
    credentials_file.chmod(0o644)

    result = runner.invoke(
        app, ["--json", "whoami"], env={"SOLWYN_CREDENTIAL_STORE": "file"}
    )

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "credential_storage_failed"
    assert f"chmod 600 {credentials_file}" in error["message"]


def test_clearing_the_last_entry_removes_the_file(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    auth.store_project_key(VALID_KEY, api_url=PROD)
    assert credentials_file.exists()

    auth.clear_credentials(api_url=PROD)

    assert not credentials_file.exists()


@pytest.mark.parametrize("content", ["{", "[]", '{"version": 2, "origins": {}}'])
def test_unreadable_content_is_no_credential_and_a_write_replaces_it(
    credentials_file: Path, content: str
) -> None:
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(credentials_file, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(descriptor, content.encode())
    os.close(descriptor)

    store = FileStore(credentials_file)
    assert store.get(credential_origin(PROD), auth.API_KEY_ENTRY) is None

    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)
    assert _read_file(credentials_file)["version"] == 1


# --- Modes ------------------------------------------------------------------


def test_store_mode_precedence_is_env_then_config_then_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert auth.store_mode({}) == "auto"
    config.update_config_values({"credential_store": "keyring"})
    assert auth.store_mode({}) == "keyring"
    assert auth.store_mode({"SOLWYN_CREDENTIAL_STORE": "file"}) == "file"
    with pytest.raises(auth.CredentialStorageError, match="auto, keyring or file"):
        auth.store_mode({"SOLWYN_CREDENTIAL_STORE": "vault"})


@pytest.mark.parametrize(
    ("family", "mode", "kind"),
    [
        ("macos", "auto", "keyring"),
        ("windows", "auto", "keyring"),
        ("linux", "auto", "file"),
        ("linux", "keyring", "keyring"),
        ("macos", "file", "file"),
        ("windows", "file", "file"),
    ],
)
def test_auto_picks_the_keychain_on_macos_and_windows_and_the_file_on_linux(
    monkeypatch: pytest.MonkeyPatch, family: str, mode: str, kind: str
) -> None:
    _platform(monkeypatch, family)

    assert auth.primary_kind(mode) == kind  # type: ignore[arg-type]


@pytest.mark.parametrize("family", ["linux", "macos", "windows"])
def test_file_mode_never_touches_the_keyring(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, family: str
) -> None:
    _platform(monkeypatch, family)
    _no_keyring(monkeypatch)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    auth.store_project_key(VALID_KEY, api_url=PROD)
    assert auth.resolve_credential({}, api_url=PROD) is not None
    assert auth.delete_legacy_entries() is False
    # Logout is the one exception: it looks in both stores (see the logout tests).


def test_linux_auto_never_touches_the_keyring_for_credentials(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    _platform(monkeypatch, "linux")
    _no_keyring(monkeypatch)
    monkeypatch.setattr(auth, "_interactive", lambda: False)

    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    assert auth.resolve_credential({}, api_url=PROD) is not None
    assert auth.delete_legacy_entries() is False
    assert auth.legacy_login_notice() is None


def test_keyring_mode_stores_one_entry_per_secret_named_by_origin(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")

    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)

    assert set(memory_keyring.values) == {
        ("solwyn-cli", f"{PROD}/cli_credential"),
        ("solwyn-cli", f"{PROD}/cli_credential_meta"),
    }
    assert not credentials_file.exists()
    found = auth.resolve_credential({}, api_url=PROD)
    assert found is not None and found.source == "keyring"


def test_keyring_mode_fails_loudly_with_no_fallback(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")

    with pytest.raises(auth.KeyringUnavailableError) as write_error:
        auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    with pytest.raises(auth.KeyringUnavailableError):
        auth.resolve_credential({}, api_url=PROD)

    assert not credentials_file.exists()
    assert write_error.value.hint is not None
    assert "SOLWYN_CREDENTIAL_STORE=file" in write_error.value.hint
    assert "solwyn login --store file" in write_error.value.hint
    assert "DBUS_SESSION_BUS_ADDRESS" in write_error.value.hint


@pytest.mark.parametrize(
    "module",
    [
        "keyrings.alt.file",
        "keyring.backends.fail",
        "keyring.backends.null",
    ],
)
def test_keyring_mode_refuses_plaintext_and_placeholder_backends(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    impostor = type("Keyring", (), {"__module__": module})
    monkeypatch.setattr(credential_store, "backend_factory", impostor)

    with pytest.raises(StoreUnavailableError):
        KeyringStore().get(credential_origin(PROD), auth.API_KEY_ENTRY)


def test_platform_backend_ignores_global_keyring_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring
    from keyring.backends import SecretService, kwallet

    class Unusable:
        @property
        def priority(self) -> float:
            raise RuntimeError("no D-Bus")

    unusable = Unusable()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    monkeypatch.setattr(
        keyring, "get_keyring", lambda: pytest.fail("global keyring config consulted")
    )
    monkeypatch.setattr(SecretService, "Keyring", unusable)
    monkeypatch.setattr(kwallet, "DBusKeyring", unusable)
    _platform(monkeypatch, "linux")

    with pytest.raises(StoreUnavailableError, match="no D-Bus session"):
        credential_store._platform_backend()


def test_windows_backend_stops_domain_roaming(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.backends import Windows

    class FakeVault:
        persist = "enterprise"

    monkeypatch.setattr(Windows, "WinVaultKeyring", FakeVault)
    _platform(monkeypatch, "windows")

    backend = credential_store._platform_backend()

    assert backend.persist == "local machine"


def test_every_keyring_call_times_out_after_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert credential_store.KEYRING_TIMEOUT_SECONDS == 5.0

    class Hanging:
        def get_password(self, service: str, username: str) -> None:
            time.sleep(2)

    monkeypatch.setattr(credential_store, "KEYRING_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(credential_store, "backend_factory", Hanging)

    started = time.monotonic()
    with pytest.raises(StoreUnavailableError, match="did not answer"):
        KeyringStore().get(credential_origin(PROD), auth.API_KEY_ENTRY)
    assert time.monotonic() - started < 1


# --- auto on macOS and Windows ----------------------------------------------


@pytest.mark.parametrize("family", ["macos", "windows"])
def test_auto_writes_the_keychain_and_deletes_the_stale_file_copy(
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: Any,
    credentials_file: Path,
    family: str,
) -> None:
    _platform(monkeypatch, family)
    auth.store_project_key(VALID_KEY, mode="file", api_url=PROD)
    assert credentials_file.exists()

    stored = auth.store_project_key(OTHER_KEY, api_url=PROD)

    assert stored.source == "keyring"
    assert ("solwyn-cli", f"{PROD}/api_key") in memory_keyring.values
    assert not credentials_file.exists()
    found = auth.stored_project_key(api_url=PROD)
    assert found is not None and found.bearer_value() == OTHER_KEY


def test_auto_reads_an_existing_file_before_the_keychain(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any
) -> None:
    _platform(monkeypatch, "macos")
    memory_keyring.values[("solwyn-cli", f"{PROD}/api_key")] = OTHER_KEY
    FileStore().set_many(credential_origin(PROD), {auth.API_KEY_ENTRY: VALID_KEY})

    found = auth.stored_project_key(api_url=PROD)

    assert found is not None
    assert found.bearer_value() == VALID_KEY
    assert found.source == "file"


@pytest.mark.parametrize("family", ["macos", "windows"])
def test_auto_falls_back_to_the_file_when_a_keychain_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    credentials_file: Path,
    keychain_unreachable: None,
    family: str,
) -> None:
    _platform(monkeypatch, family)

    stored = auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)

    assert stored.source == "file"
    written = auth.last_write()
    assert written is not None and written.fell_back
    assert (
        _read_file(credentials_file)["origins"][PROD]["cli_credential"] == LOGIN_TOKEN
    )
    found = auth.resolve_credential({}, api_url=PROD)
    assert found is not None and found.source == "file"
    note = _shared.credential_saved_note(stored)
    assert note.startswith("Could not use ")
    assert f"Saved to {credentials_file}" in note
    assert "Run solwyn login again from a desktop session" in note


def test_fallback_after_a_keychain_write_fails_deletes_keychain_leftovers(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any, credentials_file: Path
) -> None:
    _platform(monkeypatch, "macos")
    memory_keyring.values[("solwyn-cli", f"{PROD}/cli_credential")] = OTHER_LOGIN_TOKEN
    real_set = memory_keyring.set_password

    def failing_set(service: str, username: str, password: str) -> None:
        if username.endswith("/cli_credential"):
            raise RuntimeError("locked over SSH")
        real_set(service, username, password)

    monkeypatch.setattr(memory_keyring, "set_password", failing_set)

    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)

    # The old keychain token can never shadow the new file login.
    assert not any(user.startswith(PROD) for _, user in memory_keyring.values)
    found = auth.resolve_credential({}, api_url=PROD)
    assert found is not None and found.bearer_value() == LOGIN_TOKEN


def test_a_failed_keychain_replacement_never_pairs_old_token_and_new_metadata(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any
) -> None:
    _platform(monkeypatch, "macos")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")
    auth.store_login(OTHER_LOGIN_TOKEN, login_meta(label="old"), api_url=PROD)
    real_set = memory_keyring.set_password

    def failing_set(service: str, username: str, password: str) -> None:
        if username.endswith("/cli_credential"):
            raise RuntimeError("denied")
        real_set(service, username, password)

    monkeypatch.setattr(memory_keyring, "set_password", failing_set)

    with pytest.raises(auth.KeyringUnavailableError):
        auth.store_login(LOGIN_TOKEN, login_meta(label="new"), api_url=PROD)

    # The old token was dropped first: no login, never old token + new meta.
    assert auth.resolve_credential({}, api_url=PROD) is None


def test_auto_reads_an_unusable_keychain_as_empty(
    monkeypatch: pytest.MonkeyPatch, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "macos")

    assert auth.resolve_credential({}, api_url=PROD) is None
    lookup = auth.last_lookup()
    assert lookup is not None and lookup.keyring_unavailable
    assert (
        auth.resolve_credential({"SOLWYN_API_KEY": VALID_KEY}, api_url=PROD) is not None
    )


# --- --store, warnings and notes --------------------------------------------


def test_login_store_flag_is_saved_to_config(
    monkeypatch: pytest.MonkeyPatch, isolated_config: Path, memory_keyring: Any
) -> None:
    _platform(monkeypatch, "linux")

    result = runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "keyring"], input=VALID_KEY
    )

    assert result.exit_code == 0, result.stderr
    assert 'credential_store = "keyring"' in isolated_config.read_text()
    assert ("solwyn-cli", f"{PROD}/api_key") in memory_keyring.values
    data = json.loads(result.stdout)
    assert data["store"] == "keyring"
    # The next plain read finds it, because config now says keyring.
    assert auth.stored_project_key(api_url=PROD) is not None


def test_login_store_flag_conflicting_with_the_env_var_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, isolated_config: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")

    result = runner.invoke(
        app, ["--json", "login", "--with-key", "--store", "keyring"], input=VALID_KEY
    )

    assert result.exit_code == 2
    assert "conflicts with --store keyring" in json.loads(result.stderr)["message"]
    assert not isolated_config.exists()


def test_saving_a_project_key_to_the_file_steers_to_env_vars(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    _platform(monkeypatch, "linux")

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)

    assert result.exit_code == 0, result.stderr
    assert f"Stored the project key in {credentials_file}." in result.stderr
    assert "full project access and does not expire" in result.stderr
    assert "prefer SOLWYN_API_KEY from a secret manager" in result.stderr
    assert VALID_KEY not in result.stdout + result.stderr


def test_keychain_fallback_warns_once_on_login(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "macos")

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=VALID_KEY)

    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("Could not use the macOS Keychain") == 1
    assert credentials_file.exists()


def test_a_login_saved_to_the_file_says_who_can_read_it(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    stored = auth.store_login(LOGIN_TOKEN, login_meta(), mode="file", api_url=PROD)

    assert _shared.credential_saved_note(stored) == (
        f"Saved to {credentials_file} (readable only by your user, and by any "
        "program you run)."
    )


# --- SOLWYN_TOKEN -----------------------------------------------------------

ENV_TOKEN = "solwyn_cli_" + "e7" * 32


@pytest.mark.parametrize(
    ("environ", "stored_login", "expected"),
    [
        ({"SOLWYN_TOKEN": ENV_TOKEN}, True, ("environment", ENV_TOKEN)),
        (
            {"SOLWYN_TOKEN": ENV_TOKEN, "SOLWYN_API_KEY": VALID_KEY},
            False,
            ("environment", ENV_TOKEN),
        ),
        ({"SOLWYN_API_KEY": VALID_KEY}, True, ("file", LOGIN_TOKEN)),
        (
            {
                "SOLWYN_TOKEN": ENV_TOKEN,
                "SOLWYN_AUTH": "project_key",
                "SOLWYN_API_KEY": VALID_KEY,
            },
            True,
            ("environment", VALID_KEY),
        ),
        (
            {"SOLWYN_TOKEN": ENV_TOKEN, "SOLWYN_AUTH": "login"},
            False,
            ("environment", ENV_TOKEN),
        ),
        ({"SOLWYN_AUTH": "login", "SOLWYN_API_KEY": VALID_KEY}, False, None),
    ],
)
def test_user_precedence_with_solwyn_token(
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    stored_login: bool,
    expected: tuple[str, str] | None,
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    if stored_login:
        auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)

    found = auth.resolve_credential(environ, api_url=PROD)

    if expected is None:
        assert found is None
    else:
        assert found is not None
        assert (found.source, found.bearer_value()) == expected


def test_metering_never_uses_solwyn_token() -> None:
    assert (
        auth.resolve_credential({"SOLWYN_TOKEN": ENV_TOKEN}, purpose="metering") is None
    )
    key = auth.resolve_credential(
        {"SOLWYN_TOKEN": ENV_TOKEN, "SOLWYN_API_KEY": VALID_KEY}, purpose="metering"
    )
    assert key is not None and key.bearer_value() == VALID_KEY


@pytest.mark.parametrize("value", ["", "sk_proj_" + "a1" * 32, "solwyn_cli_short"])
def test_an_invalid_solwyn_token_fails_without_echoing_it(value: str) -> None:
    with pytest.raises(auth.AuthError) as caught:
        auth.resolve_credential({"SOLWYN_TOKEN": value})

    assert str(caught.value) == "SOLWYN_TOKEN is not a valid Solwyn CLI token."
    if value:
        assert value not in repr(caught.value)


def test_solwyn_token_has_no_local_metadata() -> None:
    found = auth.resolve_credential({"SOLWYN_TOKEN": ENV_TOKEN})

    assert isinstance(found, auth.LoginCredential)
    assert found.meta is None
    assert found.location == "SOLWYN_TOKEN"
    assert ENV_TOKEN not in repr(found)
