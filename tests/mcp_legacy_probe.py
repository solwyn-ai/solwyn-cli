"""Run with a separately installed mcp==1.28.1 interpreter (never imported)."""

import importlib.metadata
import json
import sys
import tempfile

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

READ_TOOL_NAMES = frozenset(
    {
        "budget_status",
        "get_project_status",
        "get_forecast",
        "get_costs",
        "list_projects",
        "get_cost_total",
        "list_budget_rules",
        "list_tags",
        "list_tag_values",
        "list_agent_runs",
        "get_agent_run",
        "list_alert_events",
        "get_overview",
        "list_cost_events",
        "get_cost_baseline",
        "get_unit_economics",
        "get_cost_drift",
        "list_priced_models",
        "simulate_costs",
        "list_provider_failovers",
        "list_untracked_surfaces",
        "list_audit_events",
        "plan_status",
        "plan_catalog",
        "get_whoami",
        "get_account_phone",
        "list_alert_activity",
        "list_open_alert_deliveries",
        "budget_uncounted",
        "get_account_alert",
        "plan_preview",
        "list_team_members",
        "list_team_invites",
    }
)


async def main():
    assert importlib.metadata.version("mcp") == "1.28.1"
    profile = sys.argv[3] if len(sys.argv) > 3 else "guardrails"
    writers = {"budget_check", "budget_confirm", "record_call", "budget_check_batch"}
    expected_names = READ_TOOL_NAMES if profile == "read" else READ_TOOL_NAMES | writers
    count = len(expected_names)
    server = StdioServerParameters(
        command=sys.argv[1],
        args=[
            "--api-url",
            sys.argv[2],
            "--project",
            "proj_9b33046ed39d182e3adafa90",
            "mcp",
            "--profile",
            profile,
        ],
        env={"SOLWYN_API_KEY": "solwyn_sk_" + "a1" * 32},
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
                    assert initialized.serverInfo.version == "0.5.2"
                    assert initialized.capabilities.resources is None
                    assert initialized.capabilities.prompts is None
                    assert len(catalog.tools) == count
                    assert {tool.name for tool in catalog.tools} == expected_names
                    assert catalog.nextCursor is None
                    assert all(
                        tool.annotations is not None
                        and tool.annotations.readOnlyHint is (tool.name not in writers)
                        for tool in catalog.tools
                    )
                    assert "ttlMs" not in catalog.model_dump(by_alias=True)
                    assert "cacheScope" not in catalog.model_dump(by_alias=True)
                    assert all(
                        tool.title and tool.annotations is not None
                        for tool in catalog.tools
                    )
                    if profile == "read":
                        assert all(
                            tool.annotations.readOnlyHint is True
                            for tool in catalog.tools
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
