"""The only team MCP additions are bounded, strict, authenticated reads."""

import hashlib
import json
from pathlib import Path

import anyio
import httpx
from mcp import Client
import pytest

from solwyn_cli import auth
from solwyn_cli.mcp import server
from test_mcp_conformance import _install_http
from test_team_account import MEMBER, INVITATION, JWT

READS = [
    ("list_team_members", "members", MEMBER),
    ("list_team_invites", "invitations", INVITATION),
]


def test_prior_35_declarations_remain_identical():
    pinned = json.loads(
        (Path(__file__).parent / "fixtures/team-prior-declarations.json").read_text()
    )
    current = {
        t.definition.name: t.definition.model_dump(mode="json", by_alias=True)
        for t in server.get_tool_catalog()
    }
    assert len(pinned) == 35
    assert set(current) - set(pinned) == {"list_team_members", "list_team_invites"}
    for name, digest in pinned.items():
        assert (
            hashlib.sha256(
                json.dumps(
                    current[name], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            == digest
        )


@pytest.mark.parametrize("profile", ["read", "guardrails"])
@pytest.mark.parametrize("name,path,payload", READS)
def test_team_mcp_complete_strict_read_no_selected_project(
    monkeypatch, profile, name, path, payload
):
    calls, _, _ = _install_http(
        monkeypatch, responses=[httpx.Response(200, json=[payload])], credentials=[JWT]
    )

    async def exercise():
        async with Client(
            server.create_server(
                launch_options=server.McpLaunchOptions(profile=profile)
            )
        ) as client:
            listed = await client.list_tools()
            declaration = next(t for t in listed.tools if t.name == name)
            assert declaration.annotations.read_only_hint is True
            assert declaration.annotations.destructive_hint is False
            assert declaration.input_schema["additionalProperties"] is False
            assert listed.next_cursor is None
            result = await client.call_tool(name, {})
            assert not result.is_error, result
            assert result.structured_content["schema"] == (
                "solwyn.team.members.v1"
                if path == "members"
                else "solwyn.team.invites.v1"
            )
            assert len(result.structured_content["data"][path]) == 1

    anyio.run(exercise)
    assert len(calls) == 1 and calls[0][1].endswith("/accounts/me/" + path)


@pytest.mark.parametrize("name,path,payload", READS)
@pytest.mark.parametrize(
    "failure", ["unknown", "missing", "null", "remote", "project_key", "extra_argument"]
)
def test_team_mcp_errors_fail_closed_private_and_respect_key_mask(
    monkeypatch, name, path, payload, failure
):
    item = payload.copy()
    if failure == "unknown":
        item["private"] = "PRIVATE_TEAM_CANARY"
    if failure == "missing":
        item.pop("name" if path == "members" else "delivery_status")
    if failure == "null":
        item["user_id" if path == "members" else "id"] = None
    response = (
        httpx.Response(
            403,
            json={
                "detail": {
                    "code": "PRIVATE_TEAM_CANARY",
                    "message": "Only admins can read the team.",
                    "private": "PRIVATE_TEAM_CANARY",
                }
            },
        )
        if failure == "remote"
        else httpx.Response(200, json=[item])
    )
    credential = (
        auth.ProjectKeyCredential(api_key="sk_proj_" + "a1" * 32, source="environment")
        if failure == "project_key"
        else JWT
    )
    calls, _, _ = _install_http(
        monkeypatch, responses=[response], credentials=[credential]
    )

    async def exercise():
        async with Client(server.create_server()) as client:
            result = await client.call_tool(
                name,
                {"private": "PRIVATE_TEAM_CANARY"}
                if failure == "extra_argument"
                else {},
            )
            assert result.is_error
            assert "PRIVATE_TEAM_CANARY" not in result.model_dump_json()
            assert result.structured_content["error"]["code"] == (
                "authentication_required"
                if failure == "project_key"
                else "invalid_arguments"
                if failure == "extra_argument"
                else "forbidden"
                if failure == "remote"
                else "invalid_read_response"
            )
            if failure == "remote":
                assert result.structured_content["error"]["message"] == (
                    "Only admins can read the team."
                )

    anyio.run(exercise)
    assert len(calls) == (0 if failure in ("extra_argument", "project_key") else 1)


@pytest.mark.parametrize("name,path,payload", READS)
def test_team_read_native_deadline_begins_after_http_start(
    monkeypatch, name, path, payload
):
    from contextlib import asynccontextmanager
    from solwyn_cli import _mcp_operation as operation

    entered = anyio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await anyio.sleep(10)
        return httpx.Response(200, json=[payload])

    original = operation.operation_scope

    @asynccontextmanager
    async def scope():
        async with original(transport=httpx.MockTransport(handler)) as control:
            yield control

    monkeypatch.setattr(operation, "operation_scope", scope)
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.25)
    monkeypatch.setattr(auth, "resolve_credential", lambda **_: JWT)

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            result = await client.call_tool(name, {})
            assert entered.is_set()
            assert result.is_error
            assert result.structured_content["error"]["code"] == "operation_deadline"

    anyio.run(exercise)
    assert len(calls) == 1
