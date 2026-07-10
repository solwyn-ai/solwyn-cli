"""Tests for allowlisted local CLI configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from solwyn_cli import config as config_module
from solwyn_cli.config import (
    CONFIG_KEYS,
    Config,
    ConfigWriteError,
    load_config,
    save_config,
)


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Route configuration reads and writes to a temporary directory."""
    path = tmp_path / "nested" / "config.toml"
    monkeypatch.setattr(config_module, "get_config_path", lambda: path)
    return path


def test_missing_config_returns_defaults(config_path: Path) -> None:
    config = load_config()

    assert config == Config(
        api_url="https://api.solwyn.ai", default_project=None, format="table"
    )
    assert not config_path.exists()


def test_config_round_trip_omits_none_and_unknown_fields(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'api_url = "https://example.test"\n'
        'format = "json"\n'
        'api_key = "must-not-survive"\n',
        encoding="utf-8",
    )

    loaded = load_config()
    save_config(loaded)

    assert loaded == Config(api_url="https://example.test", format="json")
    persisted = config_path.read_text(encoding="utf-8")
    assert "api_key" not in persisted
    assert "default_project" not in persisted
    assert set(loaded.model_dump()) == CONFIG_KEYS


def test_save_atomically_overwrites_without_temp_residue(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('format = "table"\n', encoding="utf-8")

    save_config(Config(default_project="proj_new", format="json"))

    assert load_config().default_project == "proj_new"
    assert load_config().format == "json"
    assert list(config_path.parent.iterdir()) == [config_path]


def test_save_wraps_parent_directory_errors(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_to_create_parent(*args: object, **kwargs: object) -> None:
        raise OSError("filesystem detail")

    monkeypatch.setattr(Path, "mkdir", fail_to_create_parent)

    with pytest.raises(ConfigWriteError) as exc_info:
        save_config(Config())

    assert "filesystem detail" not in str(exc_info.value)


@pytest.mark.parametrize("value", ["yaml", "JSON", ""])
def test_config_rejects_invalid_format(value: str) -> None:
    with pytest.raises(ValidationError):
        Config(format=value)


def test_invalid_toml_fails_without_exposing_contents(config_path: Path) -> None:
    secret_text = 'api_url = "unterminated-secret'
    config_path.parent.mkdir(parents=True)
    config_path.write_text(secret_text, encoding="utf-8")

    with pytest.raises(config_module.ConfigLoadError) as exc_info:
        load_config()

    assert secret_text not in str(exc_info.value)
