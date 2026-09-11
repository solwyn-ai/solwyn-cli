"""Run with a separately installed mcp==1.28.1 interpreter (never imported)."""

import importlib.metadata
import json
import sys
import tempfile

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError


async def main():
    assert importlib.metadata.version("mcp") == "1.28.1"
    profile = sys.argv[3] if len(sys.argv) > 3 else "guardrails"
    count = 13 if profile == "read" else 17
    server = StdioServerParameters(
        command=sys.argv[1],
        args=[
            "--api-url",
            sys.argv[2],
            "--project",
            "proj_legacy",
            "mcp",
            "--profile",
            profile,
        ],
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
                    assert initialized.serverInfo.version == "0.2.0"
                    assert initialized.capabilities.resources is None
                    assert initialized.capabilities.prompts is None
                    assert len(catalog.tools) == count
                    assert "ttlMs" not in catalog.model_dump(by_alias=True)
                    assert "cacheScope" not in catalog.model_dump(by_alias=True)
                    assert all(
                        tool.title and tool.annotations is not None
                        for tool in catalog.tools
                    )
                    if profile == "read":
                        assert all(
                            tool.annotations.readOnlyHint for tool in catalog.tools
                        )
                        try:
                            await client.call_tool("budget_check", {})
                        except McpError as error:
                            assert error.error.code == -32602
                            assert error.error.message == "Unknown tool"
                        else:
                            raise AssertionError("Read profile exposed budget_check")
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
                "tools": count,
                "success": True,
                "safe_error": True,
                "ping": True,
                "shutdown": True,
            }
        )
    )


if __name__ == "__main__":
    anyio.run(main)
