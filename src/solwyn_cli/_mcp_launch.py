"""Small, offline launch descriptor shared by config and diagnostic helper."""

import os
import sys
import sysconfig
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

Profile = Literal["read", "guardrails"]

# Python keyring reaches the Linux Secret Service over the desktop session bus.
# Hosts that start MCP servers with a minimal environment (Codex does) must pass
# these through, or the server cannot read the stored login or key.
LINUX_KEYCHAIN_ENV_VARS = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


class LaunchDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str
    args: list[str]
    # Parent environment variable names the host must forward (Codex: env_vars).
    env_vars: list[str]


def keychain_env_vars(platform: str | None = None) -> list[str]:
    """Return the session variables keychain access needs on this platform."""
    current = sys.platform if platform is None else platform
    return list(LINUX_KEYCHAIN_ENV_VARS) if current.startswith("linux") else []


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
    command: str, project: str | None, api_url: str, profile: Profile
) -> LaunchDescriptor:
    args = ["--project", project] if project else []
    args.extend(["--api-url", api_url, "mcp", "--profile", profile])
    return LaunchDescriptor(command=command, args=args, env_vars=keychain_env_vars())
