"""An MCP server that cannot reach any OS keychain says so, with a fixed hint.

Codex starts stdio MCP servers with a minimal environment. On a Linux desktop
that drops DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR, so Python keyring falls
back to its fail backend and the stored login is unreadable (WS-9a finding 13).
"""

import json
import os
import sys

import anyio
import httpx
import pytest
from keyring.errors import NoKeyringError
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from login_fixtures import fake_keychain, login_entries
from solwyn_cli import auth
from solwyn_cli.mcp import server
from test_mcp_conformance import _install_http


@pytest.fixture
def real_resolution(monkeypatch: pytest.MonkeyPatch):
    """Use the conformance HTTP fixture, but resolve credentials for real."""
    resolve = auth.resolve_credential
    for name in ("SOLWYN_API_KEY", "SOLWYN_AUTH"):
        monkeypatch.delenv(name, raising=False)

    def install(responses=()):
        requests, _, _ = _install_http(
            monkeypatch, responses=list(responses), credentials=[]
        )
        monkeypatch.setattr(auth, "resolve_credential", resolve)
        return requests

    return install


def _no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(service: str, name: str) -> str | None:
        raise NoKeyringError("No recommended backend was available.")

    monkeypatch.setattr(auth.keyring, "get_password", unreachable)


def _call(name: str, arguments: dict | None = None):
    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(exercise)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_hint_is_fixed_and_platform_specific(platform: str) -> None:
    hint = auth.keychain_unreachable_hint(platform)
    assert (
        hint
        == auth.KEYCHAIN_UNREACHABLE_HINTS["linux" if platform == "linux" else "other"]
    )
    assert ("DBUS_SESSION_BUS_ADDRESS" in hint) == (platform == "linux")
    assert ("env_vars" in hint) == (platform == "linux")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_whoami", {}),
        ("list_projects", {}),
        (
            "budget_check",
            {"model": "gpt-4o", "estimated_input_tokens": 10, "provider": "openai"},
        ),
    ],
)
def test_unreachable_keychain_returns_the_keychain_hint(
    monkeypatch: pytest.MonkeyPatch, real_resolution, tool: str, arguments: dict
) -> None:
    requests = real_resolution()
    _no_backend(monkeypatch)

    result = _call(tool, arguments)

    assert result.is_error
    payload = result.structured_content
    assert payload["error"]["code"] == "authentication_required"
    assert payload["error"]["hint"] == auth.keychain_unreachable_hint()
    assert json.loads(result.content[0].text) == payload
    assert requests == []


def test_login_only_tool_with_an_env_key_names_the_unreachable_keychain(
    monkeypatch: pytest.MonkeyPatch, real_resolution
) -> None:
    real_resolution()
    _no_backend(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", "sk_proj_" + "a1" * 32)

    result = _call("list_projects")

    assert result.structured_content["error"]["code"] == "authentication_required"
    assert result.structured_content["error"]["hint"] == (
        auth.keychain_unreachable_hint()
    )


@pytest.mark.parametrize(
    ("tool", "hint"),
    [
        ("get_whoami", "Configure a project key or run solwyn login, then retry."),
        ("list_projects", auth.LOGIN_REQUIRED_HINT),
    ],
)
def test_reachable_empty_keychain_keeps_the_login_hint(
    monkeypatch: pytest.MonkeyPatch, real_resolution, tool: str, hint: str
) -> None:
    real_resolution()
    fake_keychain(monkeypatch)

    result = _call(tool)

    assert result.structured_content["error"]["code"] == "authentication_required"
    assert result.structured_content["error"]["hint"] == hint


def test_a_reachable_keychain_clears_an_earlier_unreachable_lookup(
    monkeypatch: pytest.MonkeyPatch, real_resolution
) -> None:
    requests = real_resolution(
        [httpx.Response(200, json={"projects": [], "next_cursor": None})]
    )
    _no_backend(monkeypatch)
    assert auth.resolve_credential() is None
    assert auth.keychain_unreachable()

    fake_keychain(monkeypatch).update(login_entries())

    assert auth.resolve_credential() is not None
    assert not auth.keychain_unreachable()
    assert requests == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Secret Service is Linux-only"
)
def test_minimal_host_environment_reproduces_and_reports_the_keychain_hint(
    tmp_path,
) -> None:
    # The WS-9a repro: env -i HOME PATH USER solwyn --project <id> mcp, get_whoami.
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", "solwyn"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from solwyn_cli.main import app; app()",
            "--project",
            "proj_alpha",
            "--api-url",
            "http://127.0.0.1:9",
            "mcp",
        ],
        env=environment,
        cwd=str(tmp_path),
    )

    async def exercise():
        with open(os.devnull, "w") as errlog:
            async with Client(stdio_client(params, errlog=errlog)) as client:
                return await client.call_tool("get_whoami", {})

    result = anyio.run(exercise)

    error = result.structured_content["error"]
    assert error["code"] == "authentication_required"
    assert error["hint"] == auth.KEYCHAIN_UNREACHABLE_HINTS["linux"]
