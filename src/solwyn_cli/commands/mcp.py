"""Run the local Solwyn MCP server over stdio."""

import typer

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
        f"Tools: {', '.join(TOOL_NAMES)}.",
        "No telemetry, update checks, provider keys, prompts, or responses are collected.",
    ),
)


def mcp(context: typer.Context) -> None:
    """Serve Solwyn tools over local MCP stdio."""
    launch_options = (
        context.obj if isinstance(context.obj, McpLaunchOptions) else McpLaunchOptions()
    )
    create_server(launch_options=launch_options).run(transport="stdio")
