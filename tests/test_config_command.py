"""CLI tests for local configuration commands."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from solwyn_cli import config as config_module
from solwyn_cli.main import app

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route real CLI commands directly to an isolated config path."""
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setattr(config_module, "get_config_path", lambda: path)
    return path


def test_config_group_is_registered_on_real_app(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "--help"])

    assert result.exit_code == 0
    assert "get" in result.stdout
    assert "set" in result.stdout
    assert "list" in result.stdout


def test_config_get_set_and_list_stdout_stderr_discipline(
    config_path: Path,
) -> None:
    set_result = runner.invoke(app, ["config", "set", "format", "json"])
    get_result = runner.invoke(app, ["config", "get", "format"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert set_result.stdout == ""
    assert set_result.stderr == "Set format.\n"
    assert get_result.exit_code == 0
    assert get_result.stdout == "json\n"
    assert get_result.stderr == ""
    assert list_result.exit_code == 0
    assert list_result.stdout == (
        "api_url=https://api.solwyn.ai (default)\ncredential_store=auto\nformat=json\n"
    )
    assert list_result.stderr == ""


@pytest.mark.parametrize(
    "command", [["set", "default_project", "proj_x"], ["get", "default_project"]]
)
def test_the_computer_wide_default_project_is_gone(
    config_path: Path, command: list[str]
) -> None:
    result = runner.invoke(app, ["config", *command])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid configuration key: default_project" in result.stderr
    assert not config_path.exists()


@pytest.mark.parametrize("key", ["unknown", "api_key", "access_token", "refresh_token"])
def test_config_commands_reject_unknown_and_credential_keys(
    config_path: Path, key: str
) -> None:
    result = runner.invoke(app, ["config", "set", key, "secret"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid configuration key" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_set_rejects_invalid_allowed_value(config_path: Path) -> None:
    result = runner.invoke(app, ["config", "set", "format", "yaml"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid value for format" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_get_reports_invalid_toml_without_leaking_it(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    secret_text = 'api_url = "unterminated-secret'
    config_path.write_text(secret_text, encoding="utf-8")

    result = runner.invoke(app, ["config", "get", "api_url"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Could not load configuration" in result.stderr
    assert secret_text not in result.stderr
    assert "Traceback" not in result.stderr


def test_config_get_reports_invalid_utf8_without_traceback(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b'api_url = "\xff"')

    result = runner.invoke(app, ["config", "get", "api_url"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Could not load configuration" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_set_rejects_unsafe_url_without_echoing_it(config_path: Path) -> None:
    unsafe_url = "https://user:secret@api.solwyn.ai"

    result = runner.invoke(app, ["config", "set", "api_url", unsafe_url])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid value for api_url" in result.stderr
    assert unsafe_url not in result.stderr
    assert "Traceback" not in result.stderr


def test_config_set_reports_lock_failure_without_traceback(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_lock(lock_file: object) -> None:
        raise OSError("lock detail")

    monkeypatch.setattr(config_module, "_acquire_lock", fail_lock)

    result = runner.invoke(app, ["config", "set", "format", "json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Could not write configuration" in result.stderr
    assert "lock detail" not in result.stderr
    assert "Traceback" not in result.stderr
