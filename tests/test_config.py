"""Tests for allowlisted local CLI configuration."""

import multiprocessing
import os
import stat
import threading
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


def _hold_lock_then_update(config_path: str, ready: Any, release: Any) -> None:
    """Hold a real process lock after loading, then persist one setting."""
    path = Path(config_path)
    config_module.get_config_path = lambda: path
    with config_module._config_lock(path):
        current = load_config()
        ready.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release configuration lock")
        save_config(
            Config.model_validate(
                {**current.model_dump(), "default_project": "proj_locked"}
            )
        )


def _update_format(config_path: str, started: Any, done: Any) -> None:
    """Update a second setting through the production locked path."""
    path = Path(config_path)
    config_module.get_config_path = lambda: path
    started.set()
    config_module.update_config_value("format", "json")
    done.set()


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


def test_save_intersects_subclass_fields_with_config_allowlist(
    config_path: Path,
) -> None:
    class ExtendedConfig(Config):
        api_key: str

    save_config(
        ExtendedConfig(
            api_url="https://example.test",
            default_project="proj_safe",
            format="json",
            api_key="must-not-persist",
        )
    )

    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted == {
        "api_url": "https://example.test",
        "default_project": "proj_safe",
        "format": "json",
    }


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


@pytest.mark.parametrize(
    "value",
    [
        "",
        "relative/path",
        "ftp://api.solwyn.ai",
        "https:///missing-host",
        "https://bad host",
        "https://user:secret@api.solwyn.ai",
        "https://api.solwyn.ai?token=secret",
        "https://api.solwyn.ai#fragment",
        "https://api.solwyn.ai:abc",
        "https://api.solwyn.ai:99999",
        "https://api.solwyn.ai:",
    ],
)
def test_config_rejects_unsafe_api_urls_without_echoing_input(value: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Config(api_url=value)

    if value:
        assert value not in str(exc_info.value)


def test_config_accepts_absolute_http_api_url_with_host_and_port() -> None:
    config = Config(api_url="http://localhost:8000/v1")

    assert config.api_url == "http://localhost:8000/v1"


def test_invalid_toml_fails_without_exposing_contents(config_path: Path) -> None:
    secret_text = 'api_url = "unterminated-secret'
    config_path.parent.mkdir(parents=True)
    config_path.write_text(secret_text, encoding="utf-8")

    with pytest.raises(config_module.ConfigLoadError) as exc_info:
        load_config()

    assert secret_text not in str(exc_info.value)


def test_invalid_utf8_is_safely_wrapped(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b'api_url = "\xff"')

    with pytest.raises(config_module.ConfigLoadError) as exc_info:
        load_config()

    assert "Could not load configuration" in str(exc_info.value)


def test_replace_failure_preserves_old_file_and_removes_temporary_file(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path.parent.mkdir(parents=True)
    original = 'default_project = "proj_old"\n'
    config_path.write_text(original, encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace detail")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(ConfigWriteError) as exc_info:
        save_config(Config(default_project="proj_new"))

    assert "replace detail" not in str(exc_info.value)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(config_path.parent.iterdir()) == [config_path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability")
def test_save_fsyncs_file_and_parent_directory(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced_modes: list[int] = []

    def record_fsync(file_descriptor: int) -> None:
        fsynced_modes.append(os.fstat(file_descriptor).st_mode)

    monkeypatch.setattr(config_module.os, "fsync", record_fsync)

    save_config(Config(default_project="proj_durable"))

    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_concurrent_different_key_updates_are_serialized(config_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    started = context.Event()
    done = context.Event()
    first = context.Process(
        target=_hold_lock_then_update,
        args=(str(config_path), ready, release),
    )
    second = context.Process(
        target=_update_format,
        args=(str(config_path), started, done),
    )

    first.start()
    try:
        assert ready.wait(timeout=5)
        second.start()
        assert started.wait(timeout=5)
        assert not done.wait(timeout=0.25)
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert first.exitcode == 0
        assert second.exitcode == 0
    finally:
        release.set()
        if first.is_alive():
            first.terminate()
        if second.pid is not None and second.is_alive():
            second.terminate()
        first.join(timeout=1)
        if second.pid is not None:
            second.join(timeout=1)

    assert load_config() == Config(default_project="proj_locked", format="json")


def test_multi_field_update_preserves_unrelated_config(config_path: Path) -> None:
    save_config(Config(format="json"))

    updated = config_module.update_config_values(
        {
            "default_project": "proj_init",
            "api_url": "https://staging.example.test",
        }
    )

    assert updated == Config(
        default_project="proj_init",
        api_url="https://staging.example.test",
        format="json",
    )
    assert load_config() == updated


def test_concurrent_init_update_preserves_other_update_field(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"config": Config()}
    mutex = threading.Lock()
    release_first = threading.Event()
    first_loaded = threading.Event()
    init_attempted_lock = threading.Event()
    errors: list[BaseException] = []

    @contextmanager
    def coordinated_lock(_path: Path) -> Any:
        if threading.current_thread().name == "init-update":
            init_attempted_lock.set()
        with mutex:
            yield

    def coordinated_load() -> Config:
        current = state["config"]
        if threading.current_thread().name == "format-update":
            first_loaded.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("test did not release first update")
        return current

    def coordinated_save(config: Config, *, omit_default_api_url: bool = False) -> None:
        state["config"] = config

    def update(values: dict[str, object]) -> None:
        try:
            config_module.update_config_values(values)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(config_module, "_config_lock", coordinated_lock)
    monkeypatch.setattr(config_module, "load_config", coordinated_load)
    monkeypatch.setattr(config_module, "save_config", coordinated_save)
    format_thread = threading.Thread(
        target=update,
        name="format-update",
        args=({"format": "json"},),
    )
    init_thread = threading.Thread(
        target=update,
        name="init-update",
        args=(
            {
                "default_project": "proj_init",
                "api_url": "https://staging.example.test",
            },
        ),
    )

    format_thread.start()
    assert first_loaded.wait(timeout=5)
    init_thread.start()
    assert init_attempted_lock.wait(timeout=5)
    release_first.set()
    format_thread.join(timeout=5)
    init_thread.join(timeout=5)

    assert not format_thread.is_alive()
    assert not init_thread.is_alive()
    assert errors == []
    assert state["config"] == Config(
        default_project="proj_init",
        api_url="https://staging.example.test",
        format="json",
    )
