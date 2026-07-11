"""FastMCP stdio server foundation and per-tool-call dependencies."""

from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP

from solwyn_cli import auth, runtime
from solwyn_cli.auth import AuthError
from solwyn_cli.client import SolwynClient


@contextmanager
def tool_call_client() -> Iterator[tuple[runtime.CommandContext, SolwynClient]]:
    """Resolve fresh runtime, credential, and client state for one MCP tool call."""
    command_runtime = runtime.resolve_command_context(
        api_url=None,
        project=None,
        output_format=None,
        json_flag=False,
        no_color=True,
    )
    credential = auth.resolve_credential()
    if credential is None:
        raise AuthError("Authentication required. Configure credentials and retry.")
    with SolwynClient(
        api_url=command_runtime.api_url,
        credential=credential,
    ) as client:
        yield command_runtime, client


def create_server() -> FastMCP:
    """Build the stateless local MCP server without resolving credentials."""
    return FastMCP(
        name="solwyn",
        instructions=(
            "Use Solwyn tools for explicit project and budget operations. "
            "Budget status is a pure read and the only polling surface. "
            "Budget check creates a reservation only immediately before caller-made "
            "un-instrumented provider spend. "
            "Budget confirm settles a non-null reservation after that spend. "
            "Never wrap Solwyn SDK-instrumented calls with budget check or confirm. "
            "When check returns a null reservation, skip confirm."
        ),
        log_level="ERROR",
    )
