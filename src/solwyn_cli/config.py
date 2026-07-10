"""Allowlisted, non-secret local configuration for the Solwyn CLI."""

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigLoadError(ValueError):
    """Configuration could not be safely loaded."""


class ConfigWriteError(OSError):
    """Configuration could not be safely persisted."""


class Config(BaseModel):
    """Persisted CLI settings; credentials are intentionally not modeled."""

    model_config = ConfigDict(extra="ignore")

    api_url: str = "https://api.solwyn.ai"
    default_project: str | None = None
    format: Literal["table", "json"] = "table"


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
    except (OSError, tomllib.TOMLDecodeError, ValidationError, TypeError) as exc:
        raise ConfigLoadError(f"Could not load configuration at {path}.") from exc


def save_config(config: Config) -> None:
    """Atomically persist only modeled, non-null settings."""
    path = get_config_path()
    content = tomli_w.dumps(config.model_dump(exclude_none=True))
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
    except OSError as exc:
        raise ConfigWriteError(f"Could not write configuration at {path}.") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
