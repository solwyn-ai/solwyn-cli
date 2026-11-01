"""0.4.1: keys env carries a non-default API URL, login never deletes 0.3
credentials, and doctor and config list show the effective API URL."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from login_fixtures import login_meta
from solwyn_cli import auth, config, credential_store
from solwyn_cli.commands import doctor as doctor_module
from solwyn_cli.commands import login as login_module
from solwyn_cli.commands.keys import export_line
from solwyn_cli.main import app

runner = CliRunner()
PROD = "https://api.solwyn.ai"
STAGING = "https://api.staging.example.test"
KEY = "sk_proj_" + "a1" * 32
LEGACY_KEY = "sk_proj_" + "c3" * 32
NEW = "solwyn_cli_" + "2b" * 32
SERVICE = "solwyn-cli"


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    for name in ("SOLWYN_API_KEY", "SOLWYN_API_URL", "SOLWYN_AUTH", "SOLWYN_FORMAT"):
        monkeypatch.delenv(name, raising=False)
    return path


def _staging(how: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Point this invocation at staging by flag, environment or config."""
    if how == "flag":
        return ["--api-url", STAGING]
    if how == "environment":
        monkeypatch.setenv("SOLWYN_API_URL", STAGING)
    else:
        config.update_config_values({"api_url": STAGING})
    return []


# --- 1. keys env carries the API URL the key belongs to ---------------------


@pytest.mark.parametrize("how", ["flag", "environment", "config"])
def test_keys_env_exports_a_non_default_api_url(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, how: str
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    auth.store_project_key(KEY, api_url=STAGING)
    flags = _staging(how, monkeypatch)

    text = runner.invoke(app, [*flags, "keys", "env"])
    as_json = runner.invoke(app, [*flags, "--json", "keys", "env"])

    expected = f"export SOLWYN_API_KEY={KEY}\nexport SOLWYN_API_URL={STAGING}"
    assert text.exit_code == 0, text.stderr
    assert text.stdout == expected + "\n"
    assert text.stderr == ""
    assert as_json.exit_code == 0, as_json.stderr
    assert json.loads(as_json.stdout) == {
        "schema": "solwyn.keys-env.v1",
        "name": "SOLWYN_API_KEY",
        "export": expected,
        "api_url": STAGING,
    }


def test_keys_env_on_the_default_api_exports_only_the_key(
    monkeypatch: pytest.MonkeyPatch, config_path: Path
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    auth.store_project_key(KEY, api_url=PROD)

    text = runner.invoke(app, ["keys", "env"])
    as_json = runner.invoke(app, ["--json", "keys", "env"])

    assert text.stdout == f"export SOLWYN_API_KEY={KEY}\n"
    assert json.loads(as_json.stdout)["api_url"] is None
    assert "SOLWYN_API_URL" not in as_json.stdout


def test_the_one_time_export_footer_names_a_non_default_api_url() -> None:
    assert export_line(KEY, PROD) == f"export SOLWYN_API_KEY={KEY}"
    assert export_line(KEY, STAGING) == (
        f"export SOLWYN_API_KEY={KEY}\nexport SOLWYN_API_URL={STAGING}"
    )


def test_a_two_line_export_footer_keeps_its_line_break() -> None:
    import io

    from solwyn_cli.output import (
        OneTimeSecretPanel,
        OutputEngine,
        OutputFormat,
        TableData,
    )

    stdout = io.StringIO()
    engine = OutputEngine(
        output_format=OutputFormat.TABLE,
        stdout=stdout,
        stderr=io.StringIO(),
        stdout_is_tty=False,
        stderr_is_tty=False,
        no_color=True,
        environ={},
    )

    engine.write_success(
        schema="solwyn.test.v1",
        data={"ok": True},
        table=TableData(columns=("Field", "Value"), rows=(("ok", "yes"),)),
        one_time_secret=OneTimeSecretPanel(
            title="One-time project key",
            value=KEY,
            footer=export_line(KEY, STAGING),
        ),
    )

    assert f"export SOLWYN_API_KEY={KEY}\nexport SOLWYN_API_URL={STAGING}\n" in (
        stdout.getvalue()
    )
    assert "\N{REPLACEMENT CHARACTER}" not in stdout.getvalue()


# --- 2. Login never deletes 0.3 credentials ---------------------------------


@pytest.fixture
def macos_keychain(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, memory_keyring: Any
) -> Any:
    monkeypatch.setattr(
        credential_store, "platform_name", lambda platform=None: "macos"
    )
    monkeypatch.setattr(login_module, "sleep", lambda seconds: None)
    memory_keyring.values[(SERVICE, "api_key")] = LEGACY_KEY
    memory_keyring.values[(SERVICE, "access_token")] = "legacy-session"
    return memory_keyring


LEGACY_NOTE = (
    "A credential saved by Solwyn CLI 0.3 is still stored in the macOS Keychain. "
    "Run solwyn logout to clear it (this also logs you out of this API)."
)


@pytest.mark.parametrize("how", ["browser", "with-key"])
def test_login_keeps_0_3_credentials_and_says_so(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any, how: str
) -> None:
    monkeypatch.setattr(
        login_module, "_device_token", lambda *a, **kw: (NEW, login_meta())
    )
    result = (
        runner.invoke(app, ["--json", "login"])
        if how == "browser"
        else runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)
    )

    assert result.exit_code == 0, result.stderr
    assert macos_keychain.values[(SERVICE, "api_key")] == LEGACY_KEY
    assert macos_keychain.values[(SERVICE, "access_token")] == "legacy-session"
    assert LEGACY_NOTE in result.stderr
    assert LEGACY_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("command", [["whoami"], ["doctor"], ["keys", "env"]])
def test_no_other_command_deletes_0_3_credentials(
    monkeypatch: pytest.MonkeyPatch, macos_keychain: Any, command: list[str]
) -> None:
    monkeypatch.setattr(auth, "_interactive", lambda: True)
    monkeypatch.setattr(doctor_module, "SolwynClient", _OfflineClient)
    runner.invoke(app, ["--json", *command])

    assert macos_keychain.values[(SERVICE, "api_key")] == LEGACY_KEY
    assert macos_keychain.values[(SERVICE, "access_token")] == "legacy-session"


def test_logout_clears_0_3_credentials_and_reports_it(
    macos_keychain: Any,
) -> None:
    as_json = runner.invoke(app, ["--json", "logout"])

    assert as_json.exit_code == 0, as_json.stderr
    keychain = json.loads(as_json.stdout)["stores"][1]
    assert keychain["legacy_cleared"] == ["access_token", "api_key"]
    assert (SERVICE, "api_key") not in macos_keychain.values
    assert LEGACY_KEY not in as_json.stdout + as_json.stderr

    macos_keychain.values[(SERVICE, "api_key")] = LEGACY_KEY
    table = runner.invoke(app, ["logout"], env={"COLUMNS": "200"})
    assert table.exit_code == 0, table.stderr
    assert "Solwyn CLI 0.3 credentials" in table.stdout
    assert "cleared (api_key)" in table.stdout


# --- 3. doctor and config list show the effective API URL -------------------


class _OfflineClient:
    """No network: every request fails the way an unreachable API does."""

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url

    def __enter__(self) -> "_OfflineClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(503, json={})

    request_unauthenticated = request


@pytest.mark.parametrize(
    ("how", "source", "label"),
    [
        ("flag", "flag", "from --api-url"),
        ("environment", "environment", "from SOLWYN_API_URL"),
        ("config", "config", "from config"),
    ],
)
def test_doctor_shows_the_effective_api_url_and_its_source(
    monkeypatch: pytest.MonkeyPatch,
    config_path: Path,
    how: str,
    source: str,
    label: str,
) -> None:
    monkeypatch.setattr(doctor_module, "SolwynClient", _OfflineClient)
    flags = _staging(how, monkeypatch)

    as_json = runner.invoke(app, [*flags, "--json", "doctor"])
    table = runner.invoke(app, [*flags, "doctor"], env={"COLUMNS": "200"})

    assert json.loads(as_json.stdout)["api_url"] == {"url": STAGING, "source": source}
    assert f"API URL: {STAGING} ({label})" in table.stdout


def test_doctor_names_the_default_api_url(
    monkeypatch: pytest.MonkeyPatch, config_path: Path
) -> None:
    monkeypatch.setattr(doctor_module, "SolwynClient", _OfflineClient)

    as_json = runner.invoke(app, ["--json", "doctor"])

    assert json.loads(as_json.stdout)["api_url"] == {"url": PROD, "source": "default"}


@pytest.mark.parametrize(
    ("how", "label"),
    [
        ("flag", "from --api-url"),
        ("environment", "from SOLWYN_API_URL"),
        ("config", "from config"),
    ],
)
def test_config_list_shows_the_effective_api_url_and_its_source(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, how: str, label: str
) -> None:
    flags = _staging(how, monkeypatch)

    result = runner.invoke(app, [*flags, "config", "list"])

    assert result.exit_code == 0, result.stderr
    assert f"api_url={STAGING} ({label})\n" in result.stdout


def test_config_list_marks_the_default_api_url(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "list"])

    assert result.stdout.startswith(f"api_url={PROD} (default)\n")
