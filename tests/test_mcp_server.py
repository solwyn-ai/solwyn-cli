"""Protocol and command tests for the local MCP stdio server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import tomllib
from pathlib import Path
from threading import Thread

import anyio
import pytest
from mcp import ClientSession, McpError, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from typer.testing import CliRunner

from solwyn_cli import auth, runtime
from solwyn_cli import main as main_module
from solwyn_cli.commands import mcp as mcp_command
from solwyn_cli.main import app
from solwyn_cli.mcp import server as mcp_server
from solwyn_cli.output import OutputFormat


runner = CliRunner()
_GUARDRAIL_INSTRUCTIONS = (
    "Budget status is a pure read and the only polling surface.",
    (
        "Budget check creates a reservation only immediately before "
        "caller-made un-instrumented provider spend."
    ),
    "Budget confirm settles a non-null reservation after that spend.",
    "Never wrap Solwyn SDK-instrumented calls with budget check or confirm.",
    "When check returns a null reservation, skip confirm.",
)
_TOOL_NAMES = [
    "budget_status",
    "budget_check",
    "budget_confirm",
    "get_project_status",
    "get_costs",
    "list_projects",
    "get_cost_total",
    "list_budget_rules",
]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_private_fastmcp_seam_is_exactly_version_pinned_and_tool_only() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    root_package = next(
        package for package in lock["package"] if package["name"] == "solwyn-cli"
    )
    mcp_package = next(
        package for package in lock["package"] if package["name"] == "mcp"
    )

    assert "mcp==1.28.1" in dependencies
    assert (
        next(
            requirement
            for requirement in root_package["metadata"]["requires-dist"]
            if requirement["name"] == "mcp"
        )["specifier"]
        == "==1.28.1"
    )
    assert mcp_package["version"] == "1.28.1"

    server = mcp_server.create_server()
    assert set(server._mcp_server.request_handlers) == {
        types.PingRequest,
        types.ListToolsRequest,
        types.CallToolRequest,
    }


def test_fastmcp_server_initializes_and_lists_tools_in_process() -> None:
    async def exercise_protocol() -> None:
        server = mcp_server.create_server()

        async with create_connected_server_and_client_session(server) as session:
            capabilities = session.get_server_capabilities()
            tools = await session.list_tools()

        assert capabilities.tools is not None
        assert [tool.name for tool in tools.tools] == _TOOL_NAMES

    anyio.run(exercise_protocol)


def test_stdio_entrypoint_has_clean_protocol_and_guardrail_instructions(
    tmp_path: Path,
) -> None:
    async def exercise_stdio_entrypoint() -> None:
        executable = Path(sys.executable).with_name(
            "solwyn.exe" if os.name == "nt" else "solwyn"
        )
        server = StdioServerParameters(
            command=str(executable),
            args=["mcp"],
            cwd=Path(__file__).resolve().parents[1],
            env={"SOLWYN_API_KEY": "must-not-be-resolved-or-echoed"},
        )

        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            # Six complete tool schemas make startup sensitive to loaded CI hosts;
            # 30 seconds remains a bounded protocol-stall and shutdown detector.
            with anyio.fail_after(30):
                async with stdio_client(server, errlog=stderr) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        capabilities = session.get_server_capabilities()
                        tools = await session.list_tools()
                        with pytest.raises(McpError):
                            await session.list_prompts()
                        with pytest.raises(McpError):
                            await session.list_resources()
                        await session.send_ping()
            stderr.seek(0)
            stderr_output = stderr.read()

        assert initialized.serverInfo.name == "solwyn"
        assert capabilities.tools is not None
        assert capabilities.prompts is None
        assert capabilities.resources is None
        assert [tool.name for tool in tools.tools] == _TOOL_NAMES
        assert initialized.instructions is not None
        for instruction in _GUARDRAIL_INSTRUCTIONS:
            assert instruction in initialized.instructions
        assert stderr_output == ""

    anyio.run(exercise_stdio_entrypoint)


def test_real_stdio_entrypoint_applies_global_api_and_project_overrides(
    tmp_path: Path,
) -> None:
    received: list[tuple[str, str | None]] = []
    project_key = "sk_proj_" + "d4" * 32

    class ApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append((self.path, self.headers.get("Authorization")))
            payload = {
                "project_id": "proj_override",
                "budget_limit": 100.0,
                "current_usage": 12.5,
                "remaining_budget": 87.5,
                "utilization_pct": 12.5,
                "budget_mode": "hard_deny",
                "budget_period": "monthly",
                "period_date": "2026-07",
                "outage_overspend_ceiling_usd": 0.0,
                "thresholds": [],
                "sub_period": None,
                "sub_limit": None,
                "constraints": {
                    "allowed_modes": ["alert_only", "hard_deny"],
                    "allowed_periods": ["daily", "weekly", "monthly"],
                    "sub_period_enabled": True,
                    "max_thresholds": 50,
                },
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    api_server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    api_thread = Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    api_url = f"http://127.0.0.1:{api_server.server_port}"

    async def exercise_stdio_entrypoint() -> None:
        executable = Path(sys.executable).with_name(
            "solwyn.exe" if os.name == "nt" else "solwyn"
        )
        server = StdioServerParameters(
            command=str(executable),
            args=[
                "--api-url",
                api_url,
                "--project",
                "proj_override",
                "mcp",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env={
                "HOME": str(tmp_path),
                "SOLWYN_API_KEY": project_key,
                "SOLWYN_API_URL": "https://environment.invalid",
                "SOLWYN_PROJECT_ID": "proj_environment_wrong",
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
            },
        )
        with (tmp_path / "override-stderr.log").open("w+", encoding="utf-8") as stderr:
            with anyio.fail_after(30):
                async with stdio_client(server, errlog=stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool("budget_status", {})
            stderr.seek(0)
            assert stderr.read() == ""
        assert result.isError is False
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["data"]["project_id"] == "proj_override"

    try:
        anyio.run(exercise_stdio_entrypoint)
    finally:
        api_server.shutdown()
        api_server.server_close()
        api_thread.join(timeout=5)

    assert received == [
        (
            "/api/v1/projects/proj_override/budget",
            f"Bearer {project_key}",
        )
    ]


def test_server_factory_does_not_eagerly_resolve_runtime_or_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("server construction must not resolve per-call state")

    monkeypatch.setattr(runtime, "resolve_command_context", unexpected_call)
    monkeypatch.setattr(auth, "resolve_credential", unexpected_call)
    monkeypatch.setattr(mcp_server, "SolwynClient", unexpected_call)

    mcp_server.create_server()


def test_tool_call_client_resolves_fresh_runtime_and_credential_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_runtime = runtime.CommandContext(
        api_url="https://first.example",
        project="first",
        output_format=OutputFormat.TABLE,
        no_color=True,
        environment_no_color=False,
    )
    second_runtime = runtime.CommandContext(
        api_url="https://second.example",
        project="second",
        output_format=OutputFormat.TABLE,
        no_color=True,
        environment_no_color=False,
    )
    first_credential = object()
    second_credential = object()
    runtimes = [first_runtime, second_runtime]
    credentials = [first_credential, second_credential]
    constructed: list[tuple[str, object]] = []
    closed: list[object] = []
    runtime_options: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *, api_url: str, credential: object) -> None:
            self.credential = credential
            constructed.append((api_url, credential))

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            closed.append(self)

    def resolve_runtime(**kwargs: object) -> runtime.CommandContext:
        runtime_options.append(kwargs)
        return runtimes.pop(0)

    monkeypatch.setattr(runtime, "resolve_command_context", resolve_runtime)
    monkeypatch.setattr(auth, "resolve_credential", lambda: credentials.pop(0))
    monkeypatch.setattr(mcp_server, "SolwynClient", FakeClient)

    launch = mcp_server.McpLaunchOptions(
        api_url="https://override.example",
        project_id="proj_override",
    )
    with mcp_server.tool_call_client(launch) as first:
        assert first[0].api_url == "https://first.example"
    with mcp_server.tool_call_client(launch) as second:
        assert second[0].api_url == "https://second.example"

    assert constructed == [
        ("https://first.example", first_credential),
        ("https://second.example", second_credential),
    ]
    assert len(closed) == 2
    assert runtime_options == [
        {
            "api_url": "https://override.example",
            "project": "proj_override",
            "output_format": None,
            "json_flag": False,
            "no_color": True,
        },
        {
            "api_url": "https://override.example",
            "project": "proj_override",
            "output_format": None,
            "json_flag": False,
            "no_color": True,
        },
    ]


def test_tool_call_client_without_overrides_defers_to_runtime_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: list[dict[str, object]] = []
    command_runtime = runtime.CommandContext(
        api_url="https://configured.example",
        project="proj_configured",
        output_format=OutputFormat.TABLE,
        no_color=True,
        environment_no_color=False,
    )
    credential = object()

    class FakeClient:
        def __init__(self, *, api_url: str, credential: object) -> None:
            assert api_url == "https://configured.example"

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def resolve_runtime(**kwargs: object) -> runtime.CommandContext:
        options.append(kwargs)
        return command_runtime

    monkeypatch.setattr(runtime, "resolve_command_context", resolve_runtime)
    monkeypatch.setattr(auth, "resolve_credential", lambda: credential)
    monkeypatch.setattr(mcp_server, "SolwynClient", FakeClient)

    with mcp_server.tool_call_client(mcp_server.McpLaunchOptions()) as resolved:
        assert resolved[0] is command_runtime

    assert options == [
        {
            "api_url": None,
            "project": None,
            "output_format": None,
            "json_flag": False,
            "no_color": True,
        }
    ]


def test_mcp_command_dispatches_stdio_without_eager_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[str] = []
    launches: list[object] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            transports.append(transport)

    def fake_create_server(*, launch_options: object) -> FakeServer:
        launches.append(launch_options)
        return FakeServer()

    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("mcp startup must not resolve per-call state")

    monkeypatch.setattr(mcp_command, "create_server", fake_create_server)
    monkeypatch.setattr(main_module, "resolve_command_context", unexpected_call)
    monkeypatch.setattr(auth, "resolve_credential", unexpected_call)

    result = runner.invoke(
        app,
        [
            "--api-url",
            "https://override.example",
            "--project",
            "proj_override",
            "mcp",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert transports == ["stdio"]
    assert len(launches) == 1
    assert launches[0].api_url == "https://override.example"
    assert launches[0].project_id == "proj_override"


def test_mcp_modules_are_importable_from_the_package() -> None:
    assert callable(mcp_server.create_server)
    assert callable(mcp_server.tool_call_client)
