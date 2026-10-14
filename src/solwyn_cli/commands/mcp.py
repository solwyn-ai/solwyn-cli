"""Run the local Solwyn MCP server over stdio."""

import anyio
from enum import Enum
from typing import Annotated
import typer
from mcp.server import Server
from mcp.server.stdio import stdio_server

from solwyn_cli.help import help_contract
from solwyn_cli.mcp.server import McpLaunchOptions, TOOL_NAMES, create_server


MCP_HELP = help_contract(
    endpoint="local stdio MCP server",
    auth="resolved per tool call",
    exits="0 clean shutdown; 1 startup or protocol failure; 2 usage",
    schema="MCP protocol (stdio JSON-RPC)",
    guidance=(
        "Protocol: stdio only; stdout is reserved for MCP JSON-RPC framing.",
        "Runtime configuration and credentials are resolved anew for every tool call.",
        "Profiles: guardrails (default, all 35 tools) or read (31 reads). Fixed for the server lifetime; profiles never grant permissions.",
        "Put global --project and --api-url before mcp; put --profile after mcp.",
        f"Tools: {', '.join(TOOL_NAMES)}.",
        "No telemetry, update checks, provider keys, prompts, or responses are collected.",
    ),
)


class McpProfile(str, Enum):
    READ = "read"
    GUARDRAILS = "guardrails"


def mcp(
    context: typer.Context,
    profile: Annotated[
        McpProfile, typer.Option(help="Fixed tool surface for this launch.")
    ] = McpProfile.GUARDRAILS,
) -> None:
    """Serve Solwyn tools over local MCP stdio."""
    launch_options = (
        context.obj if isinstance(context.obj, McpLaunchOptions) else McpLaunchOptions()
    )
    launch_options = McpLaunchOptions(
        **(launch_options.model_dump() | {"profile": profile.value})
    )
    anyio.run(_serve_stdio, create_server(launch_options=launch_options))


async def _serve_stdio(server: Server) -> None:
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())
