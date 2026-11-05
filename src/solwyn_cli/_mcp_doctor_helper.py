"""Isolated, one-shot public-SDK client for the controlled Solwyn server.

Launched with -I and an explicit environment before SDK imports. No CLI tree,
credential store, exporter, parent tracing changes or private SDK handles.
"""

from contextlib import redirect_stderr
import os
import sys

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from solwyn_cli._mcp_doctor_ipc import (
    INPUT_LIMIT,
    OPERATION_SECONDS,
    HelperResult,
    PrivateRequest,
    isolated_environment,
)
from solwyn_cli._mcp_doctor_pipe import ParentPipe
from solwyn_cli._mcp_launch import launch_descriptor


class _ReadData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    project_id: str


class _ReadEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    schema_name: Literal["solwyn.mcp.budget-status.v1"] = Field(alias="schema")
    ok: Literal[True]
    data: _ReadData
    error: None


async def read_request(pipe: ParentPipe) -> PrivateRequest:
    pending = bytearray()
    with anyio.fail_after(3):
        while b"\n" not in pending:
            chunk = await pipe.read(INPUT_LIMIT + 1 - len(pending))
            if not chunk:
                raise ValueError("Incomplete private input.")
            pending.extend(chunk)
            if len(pending) > INPUT_LIMIT:
                raise ValueError("Private input too large.")
    line, remainder = bytes(pending).split(b"\n", 1)
    if remainder:
        raise ValueError("Unexpected private input.")
    return PrivateRequest.model_validate_json(line)


async def watch_parent(
    pipe: ParentPipe, scope: anyio.CancelScope, result: HelperResult
) -> None:
    await pipe.read(1)  # EOF or unexpected further input both invalidate the run.
    result.status = "parent_closed"
    scope.cancel()


async def probe(request: PrivateRequest, result: HelperResult) -> None:
    launch = launch_descriptor(
        request.command, request.project, request.api_url, request.profile
    )
    environment = isolated_environment()
    # Presence also prevents any accidental fallback to a stored credential.
    environment["SOLWYN_API_KEY"] = (
        request.api_key.get_secret_value() if request.api_key else ""
    )
    params = StdioServerParameters(
        command=launch.command, args=launch.args, env=environment
    )
    with open(os.devnull, "w") as errlog:
        async with Client(
            stdio_client(params, errlog=errlog),
            mode="auto",
            cache=None,
            read_timeout_seconds=OPERATION_SECONDS,
        ) as client:
            info = client.server_info
            result.identity = "match" if info and info.name == "solwyn" else "mismatch"
            result.version = (
                "match"
                if info and info.version == request.expected_version
                else "mismatch"
            )
            # Strict result validation prevents arbitrary protocol/version text output.
            result.protocol = HelperResult(protocol=client.protocol_version).protocol
            listing = await client.list_tools()
            names = [tool.name for tool in listing.tools]
            result.tools = [name for name in request.expected_tools if name in names]
            result.unexpected_tools = min(
                10000,
                len([name for name in names if name not in request.expected_tools]),
            )
            result.inventory = (
                "match"
                if set(names) == set(request.expected_tools)
                and len(names) == len(request.expected_tools)
                and not listing.next_cursor
                else "mismatch"
            )
            if (
                result.identity != "match"
                or result.version != "match"
                or result.inventory != "match"
            ):
                return
            result.status = "ok"
            if request.api_key is not None and request.project:
                result.read = "failed"
                # No project argument: the read uses the launch project.
                read = await client.call_tool("budget_status", {})
                expected = request.expected_project_id or request.project
                if not read.is_error:
                    envelope = _ReadEnvelope.model_validate(read.structured_content)
                    if envelope.data.project_id == expected:
                        result.read = "ok"


async def main() -> HelperResult:
    pipe = ParentPipe(sys.stdin.fileno())
    try:
        request = await read_request(pipe)
    except Exception:
        return HelperResult(status="invalid_input")
    result = HelperResult()
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(watch_parent, pipe, scope, result)
            try:
                with anyio.fail_after(OPERATION_SECONDS):
                    await probe(request, result)
            except TimeoutError:
                result.status = "timeout"
            except Exception:
                result.status = "failed"
            finally:
                tasks.cancel_scope.cancel()
    return result


if __name__ == "__main__":
    try:
        # SDK parser diagnostics can contain raw remote validation input. Only
        # this isolated helper redirects them; parent log handlers stay intact.
        with open(os.devnull, "w") as private_stderr, redirect_stderr(private_stderr):
            outcome = anyio.run(main)
    except BaseException:
        outcome = HelperResult(status="failed")
    print(outcome.model_dump_json(), flush=True)
