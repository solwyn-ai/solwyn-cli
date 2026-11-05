"""The 0.5 credential store: API keys per (API origin, project id), and the
one-way migration from 0.4.x.

File: version 2, ``api_keys`` is one object keyed by ``proj_`` id. Keyring:
one item per key, ``<origin>/api_key/<project id>``, plus an index item
``<origin>/api_keys``. 0.4.x kept one ``sk_proj_`` key per origin (file
version 1, keyring ``<origin>/api_key``); those keys are dropped, never read.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from key_fixtures import (
    ACCOUNT_ID,
    KEY,
    KEY_ID,
    OTHER_KEY,
    OTHER_KEY_ID,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    key_document,
    key_index,
    key_item,
    store_key,
)
from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.credential_store import FileStore, MalformedFileError
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
LOCAL = "http://localhost:8080"
SERVICE = "solwyn-cli"
LEGACY_KEY = "sk_proj_" + "c3" * 32
PENDING = {
    "device_code": "solwyn_dc_" + "4d" * 32,
    "user_code": "BCDF-GHJK",
    "verification_uri": "https://app.solwyn.ai/device",
    "verification_uri_complete": "https://app.solwyn.ai/device?user_code=BCDF-GHJK",
    "expires_at": "2099-01-01T00:10:00Z",
    "interval": 5,
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "get_config_path", lambda: tmp_path / "config.toml")
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH", "SOLWYN_API_URL", "SOLWYN_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_interactive", lambda: False)


def _platform(monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)


def _write(path: Path, document: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.write(descriptor, json.dumps(document).encode())
    os.close(descriptor)


def _login() -> dict[str, Any]:
    return {"token": LOGIN_TOKEN, "meta": login_meta().model_dump(mode="json")}


def _version_1(path: Path) -> None:
    _write(
        path,
        {
            "version": 1,
            "origins": {
                PROD: {"login": _login(), "api_key": LEGACY_KEY},
                LOCAL: {"pending_device": PENDING, "api_key": LEGACY_KEY},
                "https://keys-only.example": {"api_key": LEGACY_KEY},
            },
        },
    )


# --- Migration: the file ------------------------------------------------------


def test_a_version_1_file_is_rewritten_as_version_2_without_its_keys(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _version_1(credentials_file)

    assert FileStore().migrate() is True

    document = json.loads(credentials_file.read_text())
    assert document["version"] == credential_store.FILE_VERSION == 2
    # Logins and pending device logins are kept; every old key is dropped, and
    # an origin that held only a key is gone.
    assert document["origins"] == {
        PROD: {"login": _login()},
        LOCAL: {"pending_device": PENDING},
    }
    assert LEGACY_KEY not in credentials_file.read_text()
    assert FileStore().migrate() is False  # Once only.
    assert auth.stored_login(api_url=PROD) is not None


def test_a_version_1_file_is_readable_but_its_key_is_never_read(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _version_1(credentials_file)
    before = credentials_file.read_bytes()

    assert auth.stored_login(api_url=PROD) is not None
    assert auth.stored_api_keys(api_url=PROD) == {}
    assert auth.resolve_credential({}, purpose="metering", api_url=PROD) is None
    assert credentials_file.read_bytes() == before  # Reads change nothing.


def test_the_first_write_drops_version_1_keys(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _version_1(credentials_file)

    store_key(api_url=PROD)

    document = json.loads(credentials_file.read_text())
    assert document["version"] == 2
    assert set(document["origins"][PROD]) == {"login", "api_keys"}
    assert LEGACY_KEY not in credentials_file.read_text()


def test_a_cli_command_says_once_that_an_old_key_was_removed(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    _version_1(credentials_file)

    # keys env makes no request for a proj_ id, even with the stored login.
    first = runner.invoke(app, ["--json", "keys", "env", "-p", PROJECT_ID])
    second = runner.invoke(app, ["--json", "keys", "env", "-p", PROJECT_ID])

    assert first.stderr.count(auth.LEGACY_KEYS_REMOVED_NOTE) == 1
    assert auth.LEGACY_KEYS_REMOVED_NOTE not in second.stderr
    assert LEGACY_KEY not in first.stdout + first.stderr
    assert json.loads(credentials_file.read_text())["version"] == 2
    # The old key was never used.
    assert json.loads(first.stderr.splitlines()[-1])["code"] == "api_key_not_stored"


def test_logout_migrates_and_says_so(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    from solwyn_cli.commands import login as login_module

    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(login_module, "revoke_token", lambda api_url, token: True)
    _version_1(credentials_file)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert auth.LEGACY_KEYS_REMOVED_NOTE in result.stderr
    assert LEGACY_KEY not in credentials_file.read_text()


def test_a_version_2_file_needs_no_migration(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    store_key(api_url=PROD)
    before = credentials_file.read_bytes()

    assert auth.migrate_legacy_keys(PROD) is False
    assert credentials_file.read_bytes() == before


def test_migration_refuses_a_malformed_file(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    _write(credentials_file, {"version": 2, "origins": {PROD: {"api_key": KEY}}})

    with pytest.raises(MalformedFileError):
        FileStore().migrate()


def test_the_note_is_the_spec_text() -> None:
    assert auth.LEGACY_KEYS_REMOVED_NOTE == (
        "Removed an old project key (sk_proj_...). Old keys stopped working; run "
        "solwyn keys create --store-key for each repo."
    )


# --- Migration: the keyring ---------------------------------------------------


def test_the_keyring_item_of_an_old_key_is_deleted_with_the_note(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any
) -> None:
    _platform(monkeypatch, "macos")
    memory_keyring.values[(SERVICE, f"{PROD}/api_key")] = LEGACY_KEY
    memory_keyring.values[(SERVICE, f"{PROD}/login")] = json.dumps(_login())

    first = runner.invoke(app, ["--json", "keys", "env", "-p", PROJECT_ID])
    second = runner.invoke(app, ["--json", "keys", "env", "-p", PROJECT_ID])

    assert (SERVICE, f"{PROD}/api_key") not in memory_keyring.values
    assert (SERVICE, f"{PROD}/login") in memory_keyring.values  # Kept.
    assert first.stderr.count(auth.LEGACY_KEYS_REMOVED_NOTE) == 1
    assert auth.LEGACY_KEYS_REMOVED_NOTE not in second.stderr
    assert LEGACY_KEY not in first.stdout + first.stderr
    # The old key was never used: nothing is stored for the project.
    assert json.loads(first.stderr.splitlines()[-1])["code"] == "api_key_not_stored"


def test_file_mode_never_migrates_the_keyring(
    monkeypatch: pytest.MonkeyPatch, memory_keyring: Any
) -> None:
    _platform(monkeypatch, "macos")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    memory_keyring.values[(SERVICE, f"{PROD}/api_key")] = LEGACY_KEY

    assert auth.migrate_legacy_keys(PROD) is False
    assert memory_keyring.values[(SERVICE, f"{PROD}/api_key")] == LEGACY_KEY


# --- The keyring layout -------------------------------------------------------


class Recording:
    """An in-memory keyring that records every call in order."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", username))
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.calls.append(("set", username))
        self.entries[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        self.calls.append(("delete", username))
        if (service, username) not in self.entries:
            raise PasswordDeleteError(username)
        del self.entries[(service, username)]


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> Recording:
    backend = Recording()
    monkeypatch.setattr(credential_store, "backend_factory", lambda: backend)
    _platform(monkeypatch, "macos")
    return backend


def test_a_key_is_written_item_first_then_the_index_each_read_back(
    keychain: Recording,
) -> None:
    stored = store_key(api_url=PROD)

    item, index = key_item(PROD), key_index(PROD)
    writes = [user for op, user in keychain.calls if op == "set"]
    assert writes == [item, index]
    # Each write is followed by a read of the same entry.
    for name in (item, index):
        position = keychain.calls.index(("set", name))
        assert ("get", name) in keychain.calls[position + 1 :]
    assert json.loads(keychain.entries[(SERVICE, index)]) == [PROJECT_ID]
    document = json.loads(keychain.entries[(SERVICE, item)])
    assert set(document) == set(credential_store.API_KEY_FIELDS)
    assert (document["key"], document["key_id"], document["account_id"]) == (
        KEY,
        KEY_ID,
        ACCOUNT_ID,
    )
    assert stored.project == PROJECT_ID and stored.source == "keyring"


def test_keys_for_two_projects_and_deletion_keep_the_index_exact(
    keychain: Recording,
) -> None:
    store_key(api_url=PROD)
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="support-bot",
        api_url=PROD,
    )
    assert json.loads(keychain.entries[(SERVICE, key_index(PROD))]) == [
        PROJECT_ID,
        OTHER_PROJECT_ID,
    ]
    assert set(auth.stored_api_keys(api_url=PROD)) == {PROJECT_ID, OTHER_PROJECT_ID}

    auth.delete_stored_api_keys((PROJECT_ID,), api_url=PROD)

    assert (SERVICE, key_item(PROD)) not in keychain.entries
    assert json.loads(keychain.entries[(SERVICE, key_index(PROD))]) == [
        OTHER_PROJECT_ID
    ]

    auth.delete_stored_api_keys((OTHER_PROJECT_ID,), api_url=PROD)

    assert not any(user.startswith(f"{PROD}/api_key") for _, user in keychain.entries)
    assert auth.stored_api_keys(api_url=PROD) == {}


def test_storing_the_same_project_again_replaces_its_key(
    keychain: Recording,
) -> None:
    store_key(api_url=PROD)
    store_key(key=OTHER_KEY, key_id=OTHER_KEY_ID, api_url=PROD)

    assert json.loads(keychain.entries[(SERVICE, key_index(PROD))]) == [PROJECT_ID]
    found = auth.stored_key_for_project(PROJECT_ID, api_url=PROD)
    assert found is not None and found.bearer_value() == OTHER_KEY


def test_an_index_entry_whose_item_is_gone_is_not_a_stored_key(
    keychain: Recording,
) -> None:
    keychain.entries[(SERVICE, key_index(PROD))] = json.dumps([PROJECT_ID])

    assert auth.stored_api_keys(api_url=PROD) == {}


def test_a_key_item_is_found_by_its_exact_id_even_without_the_index(
    keychain: Recording,
) -> None:
    keychain.entries[(SERVICE, key_item(PROD))] = json.dumps(key_document())

    assert auth.stored_key_for_project(PROJECT_ID, api_url=PROD) is not None


@pytest.mark.parametrize("index", ["not json", '["checkout-bot"]', '{"a": 1}'])
def test_a_malformed_index_is_an_error_never_an_empty_store(
    keychain: Recording, index: str
) -> None:
    keychain.entries[(SERVICE, key_index(PROD))] = index

    with pytest.raises(auth.KeyringUnavailableError):
        auth.stored_api_keys(api_url=PROD)


def test_a_malformed_key_item_is_an_error_naming_its_entry(
    keychain: Recording,
) -> None:
    keychain.entries[(SERVICE, key_item(PROD))] = json.dumps({"key": KEY})

    with pytest.raises(auth.MalformedCredentialFileError) as caught:
        auth.stored_key_for_project(PROJECT_ID, api_url=PROD)

    assert PROJECT_ID in str(caught.value)
    assert KEY not in str(caught.value)


def test_stored_keys_survive_a_login_change(keychain: Recording) -> None:
    store_key(api_url=PROD)
    auth.store_login(LOGIN_TOKEN, login_meta(account_id="acct_other"), api_url=PROD)

    assert set(auth.stored_api_keys(api_url=PROD)) == {PROJECT_ID}
    auth.clear_login(api_url=PROD)
    assert set(auth.stored_api_keys(api_url=PROD)) == {PROJECT_ID}


def test_logout_removes_every_key_item_the_index_and_the_old_key(
    keychain: Recording,
) -> None:
    store_key(api_url=PROD)
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        api_url=PROD,
    )
    store_key(api_url=LOCAL)
    keychain.entries[(SERVICE, f"{PROD}/api_key")] = LEGACY_KEY

    report = auth.logout_everywhere(lambda token: True, api_url=PROD)

    assert all(store.cleared for store in report.stores if store.checked)
    assert not any(user.startswith(PROD) for _, user in keychain.entries)
    # Another API's keys are not this logout's.
    assert (SERVICE, key_item(LOCAL)) in keychain.entries
    assert (SERVICE, key_index(LOCAL)) in keychain.entries


# --- The file layout ----------------------------------------------------------


def test_a_version_2_file_round_trips(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    store_key(api_url=PROD)
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id="acct_other",
        project_name="support-bot",
        api_url=PROD,
    )

    document = json.loads(credentials_file.read_text())
    assert document["version"] == 2
    keys = document["origins"][PROD]["api_keys"]
    assert set(keys) == {PROJECT_ID, OTHER_PROJECT_ID}
    assert set(keys[PROJECT_ID]) == set(credential_store.API_KEY_FIELDS)
    assert FileStore().load() == document  # A fresh read accepts it unchanged.

    stored = auth.stored_api_keys(api_url=PROD)
    assert {pid: entry.account_id for pid, entry in stored.items()} == {
        PROJECT_ID: ACCOUNT_ID,
        OTHER_PROJECT_ID: "acct_other",
    }
    assert stored[OTHER_PROJECT_ID].project_name == "support-bot"


def test_deleting_the_last_key_drops_the_entry(
    monkeypatch: pytest.MonkeyPatch, credentials_file: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    store_key(api_url=PROD)

    auth.delete_stored_api_keys((PROJECT_ID,), api_url=PROD)

    document = json.loads(credentials_file.read_text())
    assert set(document["origins"][PROD]) == {"login"}


@pytest.mark.parametrize(
    "keys",
    [
        {PROJECT_ID: {**key_document(), "extra": "x"}},
        {PROJECT_ID: {k: v for k, v in key_document().items() if k != "account_id"}},
        {PROJECT_ID: key_document(key=LEGACY_KEY)},
        {PROJECT_ID: key_document(key_id="")},
        {PROJECT_ID: key_document(stored_at="2026-11-20T10:00:00")},  # No zone.
        {"PROJ_" + "8A" * 12: key_document()},
        [key_document()],
    ],
)
def test_a_file_with_a_malformed_stored_key_is_refused_whole(
    credentials_file: Path, keys: Any
) -> None:
    _write(credentials_file, {"version": 2, "origins": {PROD: {"api_keys": keys}}})

    with pytest.raises(MalformedFileError):
        FileStore().load()
