"""Allowlisted, non-secret local configuration for the Solwyn CLI."""

import os
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Literal
from urllib.parse import urlsplit

import tomli_w
from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ConfigLoadError(ValueError):
    """Configuration could not be safely loaded."""


class ConfigWriteError(OSError):
    """Configuration could not be safely persisted."""


class Config(BaseModel):
    """Persisted CLI settings; credentials are intentionally not modeled."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    api_url: str = "https://api.solwyn.ai"
    default_project: str | None = None
    format: Literal["table", "json"] = "table"

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        """Require a credential-free absolute HTTP(S) API base URL."""
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            raise ValueError("api_url must be a safe absolute HTTP(S) URL") from None

        authority = parsed.netloc.rsplit("@", maxsplit=1)[-1]
        if (
            value != value.strip()
            or "\\" in value
            or any(character.isspace() for character in value)
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or authority.endswith(":")
        ):
            raise ValueError("api_url must be a safe absolute HTTP(S) URL")
        return value


CONFIG_KEYS = frozenset(Config.model_fields)


def get_config_path() -> Path:
    """Return the platform-native path to the user's local configuration."""
    return Path(user_config_dir("solwyn")) / "config.toml"


def load_config() -> Config:
    """Load allowlisted settings, returning defaults when no file exists."""
    path = get_config_path()
    if not path.exists():
        return Config()

    try:
        with path.open("rb") as config_file:
            values = tomllib.load(config_file)
        return Config.model_validate(values)
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        ValidationError,
        TypeError,
    ) as exc:
        raise ConfigLoadError(f"Could not load configuration at {path}.") from exc


def _acquire_lock(lock_file: BinaryIO) -> None:
    """Acquire an exclusive advisory lock using the host platform API."""
    if os.name == "nt":
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_lock(lock_file: BinaryIO) -> None:
    """Release a lock acquired by :func:`_acquire_lock`."""
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Hold the cross-process lock associated with a configuration path."""
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        try:
            _acquire_lock(lock_file)
        except OSError:
            lock_file.close()
            raise
    except OSError as exc:
        raise ConfigWriteError(f"Could not lock configuration at {path}.") from exc

    try:
        yield
    finally:
        try:
            _release_lock(lock_file)
        except OSError as exc:
            raise ConfigWriteError(
                f"Could not unlock configuration at {path}."
            ) from exc
        finally:
            lock_file.close()


def _fsync_parent_directory(path: Path) -> None:
    """Durably record an atomic replace on POSIX; Windows has no directory fsync."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save_config(config: Config) -> None:
    """Atomically persist only modeled, non-null settings."""
    path = get_config_path()
    content = tomli_w.dumps(config.model_dump(include=CONFIG_KEYS, exclude_none=True))
    temporary_path: Path | None = None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        _fsync_parent_directory(path)
    except OSError as exc:
        raise ConfigWriteError(f"Could not write configuration at {path}.") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_config_value(key: str, value: str) -> Config:
    """Atomically read, validate, and update one allowlisted setting."""
    if key not in CONFIG_KEYS:
        raise ValueError(f"Invalid configuration key: {key}")

    path = get_config_path()
    with _config_lock(path):
        current = load_config()
        updated = Config.model_validate(
            {**current.model_dump(include=CONFIG_KEYS), key: value}
        )
        save_config(updated)
    return updated
