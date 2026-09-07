"""Actual MCP1 client against the installed Solwyn wheel; synthetic API only."""

import importlib.metadata
import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)


async def main() -> None:
    assert importlib.metadata.version("mcp") == "1.28.1"
    environment = get_default_environment()
    environment["SOLWYN_API_KEY"] = os.environ["SOLWYN_API_KEY"]
    params = StdioServerParameters(
        command=sys.argv[1],
        args=[
            "--api-url",
            sys.argv[2],
            "--project",
            sys.argv[3],
            "mcp",
            "--profile",
            "read",
        ],
        env=environment,
    )
    with open(os.devnull, "w") as errlog, anyio.fail_after(10):
        async with stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(*streams) as client:
                hello = await client.initialize()
                assert hello.protocolVersion == "2025-11-25"
                assert hello.serverInfo.name == "solwyn"
                assert hello.serverInfo.version == "0.2.0rc1"
                catalog = await client.list_tools()
                assert len(catalog.tools) == 13
                assert all(tool.annotations.readOnlyHint for tool in catalog.tools)
                good = await client.call_tool("budget_status", {})
                assert not good.isError and good.structuredContent["ok"] is True
                assert good.structuredContent["data"]["project_id"] == sys.argv[3]
                bad = await client.call_tool(
                    "budget_status", {"projectid": "PRIVATE_LEGACY_CANARY"}
                )
                assert (
                    bad.isError
                    and bad.structuredContent["error"]["code"] == "invalid_arguments"
                )
                assert "PRIVATE_" not in bad.model_dump_json()
    print(
        json.dumps(
            {
                "sdk": "1.28.1",
                "protocol": "2025-11-25",
                "read": "ok",
                "safe_error": "ok",
            }
        )
    )


if __name__ == "__main__":
    anyio.run(main)
