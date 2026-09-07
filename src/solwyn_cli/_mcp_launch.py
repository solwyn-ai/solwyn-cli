"""Small, offline launch descriptor shared by config and diagnostic helper."""

import os
import sysconfig
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

Profile = Literal["read", "guardrails"]


class LaunchDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str
    args: list[str]


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
    return LaunchDescriptor(command=command, args=args)
