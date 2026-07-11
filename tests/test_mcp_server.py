"""Protocol and command tests for the local MCP stdio server."""

import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
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
]


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
            with anyio.fail_after(10):
                async with stdio_client(server, errlog=stderr) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        capabilities = session.get_server_capabilities()
                        tools = await session.list_tools()
            stderr.seek(0)
            stderr_output = stderr.read()

        assert initialized.serverInfo.name == "solwyn"
        assert capabilities.tools is not None
        assert [tool.name for tool in tools.tools] == _TOOL_NAMES
        assert initialized.instructions is not None
        for instruction in _GUARDRAIL_INSTRUCTIONS:
            assert instruction in initialized.instructions
        assert stderr_output == ""

    anyio.run(exercise_stdio_entrypoint)


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

    class FakeClient:
        def __init__(self, *, api_url: str, credential: object) -> None:
            self.credential = credential
            constructed.append((api_url, credential))

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            closed.append(self)

    monkeypatch.setattr(
        runtime, "resolve_command_context", lambda **kwargs: runtimes.pop(0)
    )
    monkeypatch.setattr(auth, "resolve_credential", lambda: credentials.pop(0))
    monkeypatch.setattr(mcp_server, "SolwynClient", FakeClient)

    with mcp_server.tool_call_client() as first:
        assert first[0].api_url == "https://first.example"
    with mcp_server.tool_call_client() as second:
        assert second[0].api_url == "https://second.example"

    assert constructed == [
        ("https://first.example", first_credential),
        ("https://second.example", second_credential),
    ]
    assert len(closed) == 2


def test_mcp_command_dispatches_stdio_without_eager_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[str] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            transports.append(transport)

    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("mcp startup must not resolve per-call state")

    monkeypatch.setattr(mcp_command, "create_server", FakeServer)
    monkeypatch.setattr(main_module, "resolve_command_context", unexpected_call)
    monkeypatch.setattr(auth, "resolve_credential", unexpected_call)

    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert transports == ["stdio"]


def test_mcp_modules_are_importable_from_the_package() -> None:
    assert callable(mcp_server.create_server)
    assert callable(mcp_server.tool_call_client)
