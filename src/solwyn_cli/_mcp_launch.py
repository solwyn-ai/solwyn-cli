"""Small, offline launch descriptor shared by config and diagnostic helper."""

import os
import sysconfig
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

Profile = Literal["read", "guardrails"]

# Names a host must pass through to the server; values are never written.
# Codex starts servers with a minimal environment, so without these a custom
# XDG_DATA_HOME or XDG_CONFIG_HOME would point the server at another
# credentials file or config than the shell's.
FORWARDED_ENV_VARS = (
    "SOLWYN_TOKEN",
    "SOLWYN_API_KEY",
    "SOLWYN_AUTH",
    "SOLWYN_API_URL",
    "SOLWYN_CREDENTIAL_STORE",
    "SOLWYN_CREDENTIALS_FILE",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
)
# The Linux keyring (Secret Service) is reached over the desktop session bus.
LINUX_KEYRING_ENV_VARS = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


class LaunchDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str
    args: list[str]
    # Parent environment variable names the host must forward (Codex: env_vars).
    env_vars: list[str]


def forwarded_env_vars(*, keyring_on_linux: bool) -> list[str]:
    """Return the names to forward; D-Bus only when the Linux keyring is used."""
    return [*FORWARDED_ENV_VARS, *(LINUX_KEYRING_ENV_VARS if keyring_on_linux else ())]


def resolve_executable(explicit: Path | None) -> str:
    """Use this interpreter's installed console script, never PATH fallback."""
    target = (
        explicit
        if explicit is not None
        else Path(sysconfig.get_path("scripts"))
        / ("solwyn.exe" if os.name == "nt" else "solwyn")
    )
    try:
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError
        return str(target.absolute())
    except (OSError, ValueError):
        raise ValueError("Solwyn executable is missing or not executable.") from None


def launch_descriptor(
    command: str,
    project: str | None,
    api_url: str,
    profile: Profile,
    env_vars: list[str] | None = None,
) -> LaunchDescriptor:
    args = ["--project", project] if project else []
    args.extend(["--api-url", api_url, "mcp", "--profile", profile])
    return LaunchDescriptor(command=command, args=args, env_vars=env_vars or [])
