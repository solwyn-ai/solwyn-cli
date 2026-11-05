"""0.4.2: a 0.3 keychain item logout cannot delete never fails logout, and
config writes only the keys they set, never defaults."""

import json
from pathlib import Path
from typing import Any

import pytest
from keyring.errors import KeyringError, PasswordDeleteError
from login_fixtures import LOGIN_TOKEN, login_meta
from typer.testing import CliRunner

from solwyn_cli import auth, config, credential_store
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
LEGACY_KEY = "solwyn_sk_" + "c3" * 32
SERVICE = "solwyn-cli"


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    for name in ("SOLWYN_API_KEY", "SOLWYN_API_URL", "SOLWYN_AUTH", "SOLWYN_FORMAT"):
        monkeypatch.delenv(name, raising=False)
    return path


# --- 1. A 0.3 item logout cannot delete is kept, not a failed logout --------


@pytest.fixture
def macos_keychain(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, memory_keyring: Any
) -> Any:
    monkeypatch.setattr(
        credential_store, "platform_name", lambda platform=None: "macos"
    )
    auth.store_login(LOGIN_TOKEN, login_meta(), api_url=PROD)
    memory_keyring.values[(SERVICE, "api_key")] = LEGACY_KEY
    return memory_keyring


def _refuse_legacy(monkeypatch: pytest.MonkeyPatch, keychain: Any, how: str) -> None:
    """The 0.3 item belongs to another app: its delete raises, or does nothing."""
    real_delete = keychain.delete_password

    def delete(service: str, username: str) -> None:
        if "/" in username:  # This API's entries are named <origin>/<entry>.
            real_delete(service, username)
        elif how == "raises":
            if (service, username) in keychain.values:
                raise KeyringError("User interaction is not allowed.")
            raise PasswordDeleteError(username)
        elif (service, username) not in keychain.values:
            raise PasswordDeleteError(username)
        # "no-op": returns, and the item is still there.

    monkeypatch.setattr(keychain, "delete_password", delete)


KEPT_NOTE = (
    "Logout could not delete a credential saved by Solwyn CLI 0.3 (api_key) "
    "from the macOS Keychain. Delete the solwyn-cli item in Keychain Access, or "
    "run solwyn logout again from Terminal on this Mac's desktop, where macOS can "
    "ask for permission."
)


@pytest.mark.parametrize("how", ["raises", "no-op"])
def test_a_0_3_item_logout_cannot_delete_is_kept_and_logout_succeeds(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any, how: str
) -> None:
    _refuse_legacy(monkeypatch, macos_keychain, how)
    revoked: list[str] = []
    monkeypatch.setattr(
        "solwyn_cli.commands.login.revoke_token",
        lambda url, token: revoked.append(token) or True,
    )

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["cleared"] is True
    keychain = report["stores"][1]
    assert keychain["status"] == "cleared"
    assert keychain["legacy_cleared"] == []
    assert keychain["legacy_kept"] == ["api_key"]
    assert revoked == [LOGIN_TOKEN]
    assert not any("/" in user for _, user in macos_keychain.values)  # API: gone.
    assert macos_keychain.values[(SERVICE, "api_key")] == LEGACY_KEY
    assert KEPT_NOTE in " ".join(result.stderr.split())
    assert LEGACY_KEY not in result.stdout + result.stderr


def test_the_table_shows_a_kept_0_3_item(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any
) -> None:
    _refuse_legacy(monkeypatch, macos_keychain, "no-op")
    monkeypatch.setattr(
        "solwyn_cli.commands.login.revoke_token", lambda url, token: True
    )

    result = runner.invoke(app, ["logout"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stderr
    assert "kept (api_key)" in result.stdout


def test_a_current_api_entry_logout_cannot_delete_still_fails_logout(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any
) -> None:
    monkeypatch.setattr(
        "solwyn_cli.commands.login.revoke_token", lambda url, token: True
    )
    monkeypatch.setattr(macos_keychain, "delete_password", lambda service, user: None)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 3
    detail = json.loads(result.stderr)["detail"]
    assert detail["stores"][1]["status"] == "unreachable"
    assert "login" in detail["stores"][1]["kept"]
    assert detail["stores"][1]["legacy_kept"] == []  # The store failed as a whole.
    assert "Solwyn CLI 0.3" not in result.stderr


@pytest.mark.parametrize(
    ("family", "where"),
    [
        ("windows", "Delete the solwyn-cli entries in Credential Manager"),
        ("linux", "Delete the solwyn-cli items in your keyring manager"),
    ],
)
def test_the_kept_note_names_each_platforms_manager(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any, family: str, where: str
) -> None:
    _refuse_legacy(monkeypatch, macos_keychain, "raises")
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")
    monkeypatch.setattr(
        "solwyn_cli.commands.login.revoke_token", lambda url, token: True
    )

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0, result.stderr
    assert where in " ".join(result.stderr.split())


# --- 2. config writes only the keys it sets ---------------------------------


def test_config_set_credential_store_auto_writes_no_defaults(
    config_path: Path,
) -> None:
    result = runner.invoke(app, ["config", "set", "credential_store", "auto"])

    assert result.exit_code == 0, result.stderr
    assert "api_url" not in config_path.read_text()
    assert "format" not in config_path.read_text()


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (
            "default_project",
            "proj_11f6ad8ec52a2984abaafd7c",
            'default_project = "proj_11f6ad8ec52a2984abaafd7c"\n',
        ),
        ("format", "json", 'format = "json"\n'),
        ("credential_store", "file", 'credential_store = "file"\n'),
    ],
)
def test_config_set_writes_only_that_key(
    config_path: Path, key: str, value: str, expected: str
) -> None:
    result = runner.invoke(app, ["config", "set", key, value])

    assert result.exit_code == 0, result.stderr
    assert config_path.read_text() == expected


def test_config_set_keeps_other_saved_keys_and_adds_no_defaults(
    config_path: Path,
) -> None:
    runner.invoke(app, ["config", "set", "format", "json"])
    runner.invoke(
        app, ["config", "set", "default_project", "proj_11f6ad8ec52a2984abaafd7c"]
    )

    assert (
        config_path.read_text()
        == 'default_project = "proj_11f6ad8ec52a2984abaafd7c"\nformat = "json"\n'
    )


def test_login_store_and_clearing_a_project_write_no_defaults(
    config_path: Path,
) -> None:
    auth.save_store_mode("keyring")
    assert config_path.read_text() == 'credential_store = "keyring"\n'

    config.update_config_values({"default_project": "proj_11f6ad8ec52a2984abaafd7c"})
    assert config.clear_default_project("proj_11f6ad8ec52a2984abaafd7c") is True
    assert config_path.read_text() == 'credential_store = "keyring"\n'


def test_an_explicit_production_api_url_is_saved_and_still_reads_as_default(
    config_path: Path,
) -> None:
    result = runner.invoke(app, ["config", "set", "api_url", PROD])
    listed = runner.invoke(app, ["config", "list"])

    assert result.exit_code == 0, result.stderr
    assert config_path.read_text() == f'api_url = "{PROD}"\n'
    assert listed.stdout.startswith(f"api_url={PROD} (default)\n")
