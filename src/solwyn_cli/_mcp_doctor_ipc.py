"""Capped, private one-shot diagnostics protocol; never print its input."""

import json
import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from solwyn_cli._mcp_launch import Profile

INPUT_LIMIT = 8192
OUTPUT_LIMIT = 16384
OPERATION_SECONDS = 10
CLEANUP_SECONDS = 10
TOOL_INVENTORY_LIMIT = (
    37  # Exact supported guardrails catalog; byte limits remain fixed.
)
Name = Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]


class PrivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    command: str = Field(min_length=1, max_length=2048)
    project: str | None = Field(default=None, max_length=128)
    api_url: str = Field(min_length=1, max_length=2048)
    profile: Profile
    expected_tools: list[Name] = Field(max_length=TOOL_INVENTORY_LIMIT)
    expected_version: str = Field(max_length=32)
    # The launch project's id, as /auth/key resolved it in the parent.
    expected_project_id: str | None = Field(
        default=None, pattern=r"^proj_[a-f0-9]{24}$"
    )
    api_key: SecretStr | None = None

    def to_private_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        payload["api_key"] = self.api_key.get_secret_value() if self.api_key else None
        encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > INPUT_LIMIT:
            raise ValueError("Diagnostic input exceeds its limit.")
        return encoded


class HelperResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    status: Literal[
        "ok", "failed", "timeout", "parent_closed", "invalid_input", "cleanup_failed"
    ] = "failed"
    identity: Literal["match", "mismatch", "unknown"] = "unknown"
    version: Literal["match", "mismatch", "unknown"] = "unknown"
    protocol: (
        Literal["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
        | None
    ) = None
    tools: list[Name] = Field(default_factory=list, max_length=TOOL_INVENTORY_LIMIT)
    unexpected_tools: int = Field(default=0, ge=0, le=10000)
    inventory: Literal["match", "mismatch", "unknown"] = "unknown"
    read: Literal["ok", "failed", "skipped"] = "skipped"


def isolated_environment() -> dict[str, str]:
    """Explicit OS baseline; no provider keys, Python bootstrap or ambient OTel."""
    baseline = (
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    )
    environment = {key: os.environ[key] for key in baseline if key in os.environ}
    environment.update(OTEL_SDK_DISABLED="true", OTEL_PROPAGATORS="none")
    return environment
