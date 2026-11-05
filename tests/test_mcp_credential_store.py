"""MCP servers find stored credentials, and say where they looked.

WS-9a finding 13: Codex starts stdio MCP servers with a minimal environment,
which drops the desktop session bus the Linux keyring needs. The credentials
file needs only HOME, so on Linux the server now reads the login with no host
configuration. Auth errors name the store and path, never a secret.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest
from login_fixtures import LOGIN_TOKEN, login_meta
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_mcp_conformance import _install_http

from solwyn_cli import auth, credential_store
from solwyn_cli.mcp import server


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


def _call(name: str, arguments: dict | None = None):
    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(exercise)


def _platform(monkeypatch: pytest.MonkeyPatch, family: str) -> None:
    monkeypatch.setattr(credential_store, "platform_name", lambda platform=None: family)


@pytest.mark.parametrize(
    ("tool", "arguments", "prefix"),
    [
        (
            "get_whoami",
            {},
            "Run solwyn login, or set SOLWYN_TOKEN (a login) or SOLWYN_API_KEY",
        ),
        ("list_projects", {}, auth.LOGIN_REQUIRED_HINT),
        (
            "budget_check",
            {"model": "gpt-4o", "estimated_input_tokens": 10, "provider": "openai"},
            auth.METERING_KEY_HINT,
        ),
    ],
)
def test_missing_credential_hint_names_the_store_and_path(
    monkeypatch: pytest.MonkeyPatch,
    real_resolution,
    credentials_file: Path,
    tool: str,
    arguments: dict,
    prefix: str,
) -> None:
    _platform(monkeypatch, "linux")
    requests = real_resolution()

    result = _call(tool, arguments)

    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "authentication_required"
    assert error["hint"].startswith(prefix)
    assert error["hint"].endswith(f"Looked in: file, {credentials_file}.")
    assert json.loads(result.content[0].text) == result.structured_content
    assert requests == []


def test_keyring_mode_without_a_session_bus_names_the_fix(
    monkeypatch: pytest.MonkeyPatch, real_resolution, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "keyring")
    requests = real_resolution()

    result = _call("get_whoami")

    error = result.structured_content["error"]
    assert error["code"] == "credential_storage_failed"
    assert error["message"] == (
        "Could not read the system keyring (Secret Service): The system keyring "
        "is not available."
    )
    assert "SOLWYN_CREDENTIAL_STORE=file" in error["hint"]
    assert "solwyn login --store file" in error["hint"]
    assert "DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR" in error["hint"]
    assert requests == []


def test_auto_on_macos_with_no_usable_keychain_is_a_storage_error(
    monkeypatch: pytest.MonkeyPatch, real_resolution, keychain_unreachable: None
) -> None:
    _platform(monkeypatch, "macos")
    real_resolution()

    result = _call("get_whoami")

    error = result.structured_content["error"]
    assert error["code"] == "credential_storage_failed"
    assert auth.FILE_STORE_HINT in error["hint"]


def test_auto_on_macos_with_a_locked_keychain_is_a_storage_error(
    monkeypatch: pytest.MonkeyPatch, real_resolution
) -> None:
    from keyring.errors import KeyringLocked

    class Locked:
        def get_password(self, service: str, username: str) -> None:
            raise KeyringLocked("Keyring is locked")

    monkeypatch.setattr(credential_store, "backend_factory", Locked)
    _platform(monkeypatch, "macos")
    real_resolution()

    result = _call("get_whoami")

    error = result.structured_content["error"]
    assert error["code"] == "credential_storage_failed"
    assert error["message"] == (
        "Could not read the macOS Keychain: The system keyring is not available "
        "(KeyringLocked: Keyring is locked)."
    )
    assert error["hint"] == f"{auth.FILE_STORE_HINT} {auth.ENV_KEY_HINT}"


def test_get_whoami_reports_the_store_and_path_it_used(
    monkeypatch: pytest.MonkeyPatch, real_resolution, credentials_file: Path
) -> None:
    import httpx

    _platform(monkeypatch, "linux")
    auth.store_login(
        LOGIN_TOKEN, login_meta(), mode="file", api_url="https://api.example.test"
    )
    identity = {
        "email_verified": True,
        "first_name": "Ada",
        "marketing_emails_subscribed": False,
        "account_id": "acct_synthetic",
        "alert_email_override": None,
        "user_id": "user_1",
        "email": "ada@example.com",
        "display_name": "Ada",
        "subscription_tier": "free",
    }
    account = {
        "id": "acct_synthetic",
        "display_name": "Acme",
        "subscription_tier": "free",
        "alert_email_override": None,
        "seat_limit": 1,
        "seats_used": 1,
        "your_role": "owner",
    }
    real_resolution(
        [
            httpx.Response(200, json=identity),
            httpx.Response(200, json=account),
            httpx.Response(200, json=[]),
        ]
    )

    result = _call("get_whoami")

    assert not result.is_error, result.structured_content
    data = result.structured_content["data"]
    assert data["auth_source"] == "file"
    assert data["credential"]["location"] == str(credentials_file)
    assert LOGIN_TOKEN not in json.dumps(result.structured_content)


class _ProjectsApi(BaseHTTPRequestHandler):
    authorizations: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming.
        type(self).authorizations.append(self.headers.get("Authorization"))
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def _minimal_env_list_projects(home: Path, api_url: str) -> Any:
    # The WS-9a host: only HOME, PATH and USER reach the server (no D-Bus, no
    # XDG_*, no SOLWYN_*), as Codex does by default.
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", "solwyn"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from solwyn_cli.main import app; app()",
            "--project",
            "proj_be76331b95dfc399cd776d2f",
            "--api-url",
            api_url,
            "mcp",
        ],
        env=environment,
        cwd=str(home),
    )

    async def exercise():
        with open(os.devnull, "w") as errlog:
            async with Client(stdio_client(params, errlog=errlog)) as client:
                return await client.call_tool("list_projects", {})

    return anyio.run(exercise)


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the XDG data dir is Linux's"
)


@linux_only
def test_a_minimal_environment_server_reads_the_file_login(tmp_path: Path) -> None:
    api = ThreadingHTTPServer(("127.0.0.1", 0), _ProjectsApi)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    api_url = f"http://127.0.0.1:{api.server_port}"
    _ProjectsApi.authorizations = []
    path = tmp_path / ".local" / "share" / "solwyn" / "credentials.json"
    credential_store.FileStore(path).set_many(
        credential_store.credential_origin(api_url),
        {
            auth.LOGIN_ENTRY: json.dumps(
                {"token": LOGIN_TOKEN, "meta": login_meta().model_dump(mode="json")}
            ),
        },
    )
    try:
        result = _minimal_env_list_projects(tmp_path, api_url)
    finally:
        api.shutdown()
        api.server_close()

    assert not result.is_error, result.structured_content
    assert _ProjectsApi.authorizations == [f"Bearer {LOGIN_TOKEN}"]


@linux_only
def test_a_minimal_environment_server_without_a_login_names_the_file(
    tmp_path: Path,
) -> None:
    result = _minimal_env_list_projects(tmp_path, "http://127.0.0.1:9")

    error = result.structured_content["error"]
    assert error["code"] == "authentication_required"
    expected = tmp_path / ".local" / "share" / "solwyn" / "credentials.json"
    assert error["hint"].endswith(f"Looked in: file, {expected}.")
