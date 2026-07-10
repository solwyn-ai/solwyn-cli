"""CLI tests for local configuration commands."""

from pathlib import Path

import pytest
from platformdirs import user_config_dir
from typer.testing import CliRunner

from solwyn_cli.main import app


runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep real CLI configuration outside the developer's home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_config_group_is_registered_on_real_app(isolated_home: Path) -> None:
    result = runner.invoke(app, ["config", "--help"])

    assert result.exit_code == 0
    assert "get" in result.stdout
    assert "set" in result.stdout
    assert "list" in result.stdout


def test_config_get_set_and_list_stdout_stderr_discipline(
    isolated_home: Path,
) -> None:
    set_result = runner.invoke(app, ["config", "set", "default_project", "proj_x"])
    get_result = runner.invoke(app, ["config", "get", "default_project"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert set_result.stdout == ""
    assert set_result.stderr == "Set default_project.\n"
    assert get_result.exit_code == 0
    assert get_result.stdout == "proj_x\n"
    assert get_result.stderr == ""
    assert list_result.exit_code == 0
    assert list_result.stdout == (
        "api_url=https://api.solwyn.ai\ndefault_project=proj_x\nformat=table\n"
    )
    assert list_result.stderr == ""


@pytest.mark.parametrize("key", ["unknown", "api_key", "access_token", "refresh_token"])
def test_config_commands_reject_unknown_and_credential_keys(
    isolated_home: Path, key: str
) -> None:
    result = runner.invoke(app, ["config", "set", key, "secret"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid configuration key" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_set_rejects_invalid_allowed_value(isolated_home: Path) -> None:
    result = runner.invoke(app, ["config", "set", "format", "yaml"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid value for format" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_get_reports_invalid_toml_without_leaking_it(
    isolated_home: Path,
) -> None:
    config_dir = Path(user_config_dir("solwyn"))
    config_dir.mkdir(parents=True)
    secret_text = 'api_url = "unterminated-secret'
    (config_dir / "config.toml").write_text(secret_text, encoding="utf-8")

    result = runner.invoke(app, ["config", "get", "api_url"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Could not load configuration" in result.stderr
    assert secret_text not in result.stderr
    assert "Traceback" not in result.stderr
