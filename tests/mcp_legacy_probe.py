"""Run with a separately installed mcp==1.28.1 interpreter (never imported)."""

import importlib.metadata
import json
import sys
import tempfile

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    assert importlib.metadata.version("mcp") == "1.28.1"
    server = StdioServerParameters(
        command=sys.argv[1],
        args=["--api-url", sys.argv[2], "--project", "proj_legacy", "mcp"],
        env={"SOLWYN_API_KEY": "sk_proj_" + "a1" * 32},
    )
    with tempfile.TemporaryFile(mode="w+") as stderr:
        with anyio.fail_after(15):
            async with stdio_client(server, errlog=stderr) as streams:
                async with ClientSession(*streams) as client:
                    initialized = await client.initialize()
                    catalog = await client.list_tools()
                    good = await client.call_tool("budget_status", {})
                    bad = await client.call_tool(
                        "budget_status", {"projectid": "PRIVATE_LEGACY_42"}
                    )
                    await client.send_ping()
                    assert initialized.protocolVersion == "2025-11-25"
                    assert initialized.serverInfo.name == "solwyn"
                    assert initialized.serverInfo.title == "Solwyn"
                    assert initialized.serverInfo.version == "0.1.0"
                    assert initialized.capabilities.resources is None
                    assert initialized.capabilities.prompts is None
                    assert len(catalog.tools) == 17
                    assert good.structuredContent["ok"] and not good.isError, (
                        good.model_dump()
                    )
                    assert (
                        bad.isError
                        and bad.structuredContent["error"]["code"]
                        == "invalid_arguments"
                    )
                    assert "PRIVATE_LEGACY_42" not in bad.model_dump_json()
        stderr.seek(0)
        assert stderr.read() == ""
    print(
        json.dumps(
            {
                "client_sdk": "1.28.1",
                "tools": 17,
                "success": True,
                "safe_error": True,
                "ping": True,
                "shutdown": True,
            }
        )
    )


if __name__ == "__main__":
    anyio.run(main)
