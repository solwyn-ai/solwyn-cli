"""Fixed profiles and descriptive discovery must never cache spend authority."""

import json
import os
from pathlib import Path
import subprocess
import sys

import anyio
import httpx
from mcp import Client
from pydantic import ValidationError
import pytest

from solwyn_cli.mcp import server as mcp_server
from solwyn_cli import auth
from solwyn_cli._vendor import wire
from test_mcp_conformance import _install_http, BUDGET_RESPONSE, VALID_KEY
from test_mcp_sdk_migration import raw_connection, MODERN_META, synthetic_status


WRITERS = {"budget_check", "budget_check_batch", "budget_confirm", "record_call"}


@pytest.mark.parametrize("profile", ["read", "guardrails"])
def test_catalog_metadata_and_runtime_models(profile):
    catalog = mcp_server.get_tool_catalog(profile=profile)
    names = [tool.definition.name for tool in catalog]
    assert names == [
        name
        for name in mcp_server.TOOL_NAMES
        if profile == "guardrails" or name not in WRITERS
    ]
    for tool in catalog:
        definition = tool.definition
        assert definition.title and "_" not in definition.title
        assert definition.annotations.read_only_hint == (definition.name not in WRITERS)
        assert definition.annotations.open_world_hint is True
        if definition.name in WRITERS:
            assert definition.annotations.idempotent_hint is False
            assert tool.credential_requirement == "project_key"
        else:
            assert definition.annotations.idempotent_hint is True
        assert definition.input_schema == tool.input_model.model_json_schema(
            by_alias=True
        )
        assert definition.output_schema == tool.output_model.model_json_schema(
            by_alias=True
        )
        assert all(
            len(field.get("description", "")) >= 20
            for field in definition.input_schema["properties"].values()
        )
        for nested in definition.input_schema.get("$defs", {}).values():
            assert all(
                len(field.get("description", "")) >= 20
                for field in nested.get("properties", {}).values()
            )
    by_name = {tool.definition.name: tool for tool in catalog}
    for name in ("get_overview", "list_projects"):
        assert by_name[name].credential_requirement == "jwt"
    assert by_name["list_alert_events"].credential_requirement == "either"
    assert "account=true" in by_name["list_alert_events"].credential_note


def test_profile_is_immutable_and_invalid_profiles_fail():
    launch = mcp_server.McpLaunchOptions(profile="read")
    with pytest.raises(ValidationError):
        launch.profile = "guardrails"
    with pytest.raises(ValidationError):
        mcp_server.McpLaunchOptions(profile="unknown")


@pytest.mark.parametrize("legacy", [False, True])
def test_profile_exclusions_and_cache_wire(monkeypatch, legacy):
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery or excluded calls reached auth")

    monkeypatch.setattr(mcp_server.auth, "resolve_credential", forbidden)
    monkeypatch.setattr(mcp_server, "_budget_status_operation", synthetic_status)

    async def exercise():
        server = mcp_server.create_server(
            launch_options=mcp_server.McpLaunchOptions(profile="read")
        )
        async with raw_connection(server, legacy=legacy) as (request, _):
            listed = (await request("tools/list"))["result"]
            assert len(listed["tools"]) == 30
            assert listed.get("ttlMs") == (None if legacy else 60_000)
            assert listed.get("cacheScope") == (None if legacy else "private")
            for name in sorted(WRITERS):
                response = await request(
                    "tools/call",
                    {"name": name, "arguments": {"secret_unknown": "PRIVATE"}},
                )
                assert response["error"] == {"code": -32602, "message": "Unknown tool"}
            called = (
                await request("tools/call", {"name": "budget_status", "arguments": {}})
            )["result"]
            assert "ttlMs" not in called and "cacheScope" not in called
            assert called["isError"] is False

    anyio.run(exercise)


@pytest.mark.parametrize("profile,count", [("read", 30), ("guardrails", 34)])
def test_actual_stdio_profile_discovery_and_rejection(tmp_path, profile, count):
    executable = Path(sys.executable).with_name(
        "solwyn.exe" if os.name == "nt" else "solwyn"
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": MODERN_META},
        }
    ]
    result = subprocess.run(
        [
            str(executable),
            "--project",
            "proj_synthetic",
            "--api-url",
            "http://127.0.0.1:1",
            "mcp",
            "--profile",
            profile,
        ],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "SOLWYN_API_KEY": "invalid-synthetic-never-resolve",
        },
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    responses = {
        response["id"]: response
        for response in map(json.loads, result.stdout.splitlines())
    }
    assert len(responses[1]["result"]["tools"]) == count
    assert responses[1]["result"]["ttlMs"] == 60_000
    if profile == "read":
        rejected = subprocess.run(
            [str(executable), "mcp", "--profile", "read"],
            input=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "_meta": MODERN_META,
                        "name": "budget_check",
                        "arguments": {},
                    },
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "SOLWYN_API_KEY": "invalid-synthetic-never-resolve",
            },
            timeout=15,
        )
        assert rejected.returncode == 0
        assert json.loads(rejected.stdout)["error"] == {
            "code": -32602,
            "message": "Unknown tool",
        }
        assert rejected.stderr == ""
    assert result.stderr == ""


@pytest.mark.parametrize("profile,ceiling_kib", [("guardrails", 168), ("read", 72)])
def test_catalog_is_deterministic_and_has_a_payload_budget(profile, ceiling_kib):
    def encoded(selected=None):
        return json.dumps(
            [
                tool.definition.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                for tool in mcp_server.get_tool_catalog(profile=profile)
                if selected is None or (tool.definition.name in updated) == selected
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    updated = {
        "get_costs",
        "list_budget_rules",
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
    }
    first = encoded()
    assert first == encoded()
    # Includes useful output schemas. Budgets allow deliberate feature growth,
    # but catch an accidental duplicated catalog or verbose schema expansion.
    # Keep the existing ceilings for unchanged tools. Allocate a separate
    # 112 KiB for the 17 adopted reads and two extended read schemas; the exact
    # inventory and complete golden independently pin every declaration.
    assert len(encoded(False)) <= ceiling_kib * 1024
    assert len(encoded(True)) <= 112 * 1024


def test_cached_discovery_does_not_cache_calls(monkeypatch):
    calls = []

    def status(*args, **kwargs):
        calls.append(1)
        return synthetic_status()

    monkeypatch.setattr(mcp_server, "_budget_status_operation", status)

    async def exercise():
        async with Client(mcp_server.create_server()) as client:
            await client.list_tools()
            await client.list_tools()
            await client.call_tool("budget_status", {})
            await client.call_tool("budget_status", {})

    anyio.run(exercise)
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_discovery_cache_keeps_credentials_fresh(monkeypatch, mode):
    rotated_key = "sk_proj_" + "c9" * 32
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(200, json=BUDGET_RESPONSE),
            httpx.Response(200, json=BUDGET_RESPONSE),
        ],
        credentials=[
            auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment"),
            auth.ProjectKeyCredential(api_key=rotated_key, source="environment"),
            None,
        ],
    )
    lists = []
    original = mcp_server._ToolCatalog.list_tools

    async def listed(self, ctx, params):
        lists.append(1)
        return await original(self, ctx, params)

    monkeypatch.setattr(mcp_server._ToolCatalog, "list_tools", listed)

    async def exercise():
        async with Client(mcp_server.create_server(), mode=mode) as client:
            await client.list_tools()
            await client.list_tools()
            for _ in range(2):
                assert (await client.call_tool("budget_status", {})).is_error is False
            missing = await client.call_tool("budget_status", {})
            assert missing.is_error is True
            assert (
                missing.structured_content["error"]["code"] == "authentication_required"
            )

    anyio.run(exercise)
    assert [request[2]["authorization"] for request in requests] == [
        f"Bearer {VALID_KEY}",
        f"Bearer {rotated_key}",
    ]
    assert len(lists) == (1 if mode == "auto" else 2)


def test_nested_description_models_preserve_wire_validation():
    def without_descriptions(value):
        if isinstance(value, dict):
            return {
                key: without_descriptions(item)
                for key, item in value.items()
                if key != "description"
            }
        if isinstance(value, list):
            return [without_descriptions(item) for item in value]
        return value

    for name in ("MediaUsage", "TokenDetails", "BudgetCheckRequest"):
        assert without_descriptions(
            getattr(mcp_server, name).model_json_schema()
        ) == without_descriptions(getattr(wire, name).model_json_schema())
