"""Run the local Solwyn MCP server over stdio."""

from solwyn_cli.help import help_contract
from solwyn_cli.mcp.server import create_server


MCP_HELP = help_contract(
    endpoint="local stdio MCP server",
    auth="resolved per tool call",
    exits="0 clean shutdown; 1 startup or protocol failure; 2 usage",
    schema="MCP protocol (stdio JSON-RPC)",
    guidance=(
        "Protocol: stdio only; stdout is reserved for MCP JSON-RPC framing.",
        "Runtime configuration and credentials are resolved anew for every tool call.",
        "No telemetry, update checks, provider keys, prompts, or responses are collected.",
    ),
)


def mcp() -> None:
    """Serve Solwyn tools over local MCP stdio."""
    create_server().run(transport="stdio")
