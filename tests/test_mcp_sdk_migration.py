"""Released protocol and public SDK migration regressions."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import anyio
import httpx
import pytest
from jsonschema import validate
from mcp import Client, types
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_mcp_conformance import (
    BUDGET_RESPONSE,
    CHECK_RESPONSE,
    VALID_KEY,
    _install_http,
)

from solwyn_cli import __version__, auth
from solwyn_cli.mcp import server as mcp_server


def test_current_stdio_discovery_without_initialization(tmp_path):
    executable = os.environ.get("SOLWYN_TEST_SERVER_EXECUTABLE") or Path(
        sys.executable
    ).with_name("solwyn.exe" if os.name == "nt" else "solwyn")
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    result = subprocess.run(
        [str(executable), "mcp"],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "SOLWYN_API_KEY": "invalid-synthetic-never-authenticate",
        },
        timeout=15,
    )
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert "result" in response, response
    discovery = response["result"]
    assert discovery["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "solwyn",
        "title": "Solwyn",
        "version": __version__,
    }
    assert discovery["supportedVersions"] == ["2026-07-28"]
    assert discovery["capabilities"] == {"tools": {"listChanged": False}}
    assert result.stderr == ""


CANARY = "PRIVATE_SDK2_MIGRATION_71c4"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
    "tracestate": "private=" + CANARY,
}


@asynccontextmanager
async def raw_connection(server, legacy=False, legacy_version="2025-11-25"):
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as group:
            group.start_soon(
                server.run, *server_streams, server.create_initialization_options()
            )
            request_id = 0

            async def request(method, params=None):
                nonlocal request_id
                request_id += 1
                params = (
                    {"_meta": MODERN_META, **(params or {})} if not legacy else params
                )
                await client_streams[1].send(
                    SessionMessage(
                        types.JSONRPCRequest(
                            jsonrpc="2.0",
                            id=request_id,
                            method=method,
                            params=params,
                        )
                    )
                )
                with anyio.fail_after(5):
                    response = await client_streams[0].receive()
                assert response.message.id == request_id
                return response.message.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                )

            if legacy:
                initialized = await request(
                    "initialize",
                    {
                        "protocolVersion": legacy_version,
                        "capabilities": {},
                        "clientInfo": {"name": "raw-legacy", "version": "0.1"},
                    },
                )
                assert initialized["result"]["protocolVersion"] == legacy_version
                assert initialized["result"]["serverInfo"] == {
                    "name": "solwyn",
                    "title": "Solwyn",
                    "version": __version__,
                }
                assert initialized["result"]["capabilities"] == {
                    "experimental": {},
                    "tools": {"listChanged": False},
                }
                await client_streams[1].send(
                    SessionMessage(
                        types.JSONRPCNotification(
                            jsonrpc="2.0",
                            method="notifications/initialized",
                        )
                    )
                )
            try:
                yield request, client_streams
            finally:
                group.cancel_scope.cancel()


def synthetic_status(*args, **kwargs):
    return mcp_server._success(
        mcp_server._BudgetStatusResult,
        mcp_server.BudgetStatusData.model_validate(BUDGET_RESPONSE),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_raw_protocol_success_errors_and_malformed_frames(monkeypatch, caplog, legacy):
    monkeypatch.setattr(mcp_server, "_budget_status_operation", synthetic_status)
    caplog.set_level(logging.DEBUG)

    async def exercise():
        async with raw_connection(mcp_server.create_server(), legacy) as (
            request,
            streams,
        ):
            catalog = (await request("tools/list"))["result"]
            assert [t["name"] for t in catalog["tools"]] == list(mcp_server.TOOL_NAMES)
            schema = catalog["tools"][0]["outputSchema"]
            good = (
                await request("tools/call", {"name": "budget_status", "arguments": {}})
            )["result"]
            bad = (
                await request(
                    "tools/call",
                    {"name": "budget_status", "arguments": {CANARY: CANARY}},
                )
            )["result"]
            for result in [good, bad]:
                validate(result["structuredContent"], schema)
                assert (
                    json.loads(result["content"][0]["text"])
                    == result["structuredContent"]
                )
            assert good["isError"] is False
            assert bad["isError"] is True
            if not legacy:
                assert good["resultType"] == "complete"
            malformed = []
            for params in [
                {"name": "budget_status", "arguments": CANARY},
                {"name": {CANARY: CANARY}, "arguments": {}},
                {"name": CANARY, "arguments": {}},
            ]:
                result = await request("tools/call", params)
                assert result["error"]["code"] == -32602
                malformed.append(result)
            await streams[1].send(
                SessionMessage(
                    types.JSONRPCNotification(
                        jsonrpc="2.0",
                        method="notifications/cancelled",
                        params={"requestId": {CANARY: CANARY}},
                    )
                )
            )
            # A real transport exception reaches the SDK emitting logger. The
            # canary stays private even with a preinstalled DEBUG capture handler.
            await streams[1].send(ValueError(CANARY))
            assert "result" in await request("tools/list")
            assert CANARY not in json.dumps([good, bad, *malformed])
            assert CANARY not in caplog.text
            assert "MCP protocol diagnostic" in caplog.text

    anyio.run(exercise)


def test_unsupported_current_protocol_is_sdk_error():
    async def exercise():
        async with raw_connection(mcp_server.create_server()) as (request, _):
            result = await request(
                "tools/list",
                {
                    "_meta": {
                        **MODERN_META,
                        "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                    }
                },
            )
            assert result["error"]["code"] == -32022
            assert result["error"]["message"] == "Unsupported protocol version"
            assert "result" in await request("tools/list")

    anyio.run(exercise)


@pytest.mark.parametrize("mode", ["auto", "legacy", "2026-07-28"])
def test_public_clients_and_fresh_auth_without_caching(monkeypatch, mode):
    credentials = [
        auth.ApiKeyCredential(api_key=VALID_KEY, source="environment"),
        None,
    ]
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=BUDGET_RESPONSE)],
        credentials=credentials,
    )

    async def exercise():
        async with Client(mcp_server.create_server(), mode=mode, cache=None) as client:
            if mode != "2026-07-28":
                assert client.server_info.name == "solwyn"
                assert client.server_info.title == "Solwyn"
                assert client.server_info.version == __version__
                assert client.server_capabilities.model_dump(
                    by_alias=True, exclude_none=True
                ) == (
                    {"experimental": {}, "tools": {"listChanged": False}}
                    if mode == "legacy"
                    else {"tools": {"listChanged": False}}
                )
            assert client.protocol_version == (
                "2025-11-25" if mode == "legacy" else "2026-07-28"
            )
            first = await client.call_tool("budget_status", {})
            second = await client.call_tool("budget_status", {})
            assert first.structured_content["ok"] is True
            assert (
                second.structured_content["error"]["code"] == "authentication_required"
            )
            assert second.is_error is True

    anyio.run(exercise)
    assert len(requests) == 1


def test_legacy_json_preparsing_and_numeric_coercion(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=CHECK_RESPONSE)],
        credentials=[auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")],
    )

    async def exercise():
        async with Client(mcp_server.create_server(), cache=None) as client:
            result = await client.call_tool(
                "budget_check",
                {
                    "estimated_input_tokens": "12",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "tags": '{"arbitrary":"valid"}',
                    "no_record": "true",
                },
            )
            assert result.is_error is False

    anyio.run(exercise)
    assert requests[0][2]["json"]["estimated_input_tokens"] == 12
    assert requests[0][2]["json"]["tags"] == {"arbitrary": "valid"}


@pytest.mark.parametrize(
    "exception_type", [asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
def test_safe_tool_boundary_preserves_control_exceptions(exception_type):
    # Test the exact callback without letting SystemExit terminate pytest's loop.
    catalog = mcp_server._ToolCatalog("guardrails")

    @catalog.tool(
        name="control", title="Test control flow", description="Test control flow"
    )
    async def operation() -> mcp_server._BudgetStatusResult:
        raise exception_type()

    async def exercise():
        with pytest.raises(exception_type):
            await catalog.call_tool(
                None, types.CallToolRequestParams(name="control", arguments={})
            )

    anyio.run(exercise)


def test_no_telemetry_under_real_ambient_exporter():
    # An isolated process installs a real host tracer once, without replacing a
    # provider/propagator in the production server or contaminating other tests.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--telemetry"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "SOLWYN_API_KEY": "invalid-synthetic-never-authenticate",
        },
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["default_sdk_spans"] > 0
    assert evidence["solwyn_sdk_spans"] == 0
    assert evidence["protocol_eras"] == 2


async def telemetry_probe():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    contexts = []

    def operation(*args, **kwargs):
        contexts.append(trace.get_current_span().get_span_context().trace_id)
        if args and args[0] == "fail":
            raise RuntimeError(CANARY)
        return synthetic_status()

    mcp_server._budget_status_operation = operation
    default_count = 0
    for legacy in [False, True]:
        for default in [True, False]:
            exporter.clear()
            if default:

                async def list_tools(ctx, params):
                    return types.ListToolsResult(tools=[])

                server = Server(
                    "solwyn",
                    title="Solwyn",
                    version=__version__,
                    on_list_tools=list_tools,
                )
            else:
                server = mcp_server.create_server()
            with trace.get_tracer("ambient-test").start_as_current_span(
                "ambient"
            ) as ambient:
                ambient_id = ambient.get_span_context().trace_id
                async with raw_connection(server, legacy) as (request, streams):
                    replies = [await request("tools/list")]
                    if not default:
                        for arguments in [
                            {},
                            {"project_id": "fail"},
                            {"projectid": CANARY},
                            CANARY,
                        ]:
                            replies.append(
                                await request(
                                    "tools/call",
                                    {
                                        "name": "budget_status",
                                        "arguments": arguments,
                                        # Legacy trace context uses _meta without modern envelope.
                                        "_meta": {
                                            k: v
                                            for k, v in MODERN_META.items()
                                            if k in {"traceparent", "tracestate"}
                                        }
                                        if legacy
                                        else MODERN_META,
                                    },
                                )
                            )
                        replies.append(
                            await request(
                                "tools/call", {"name": CANARY, "arguments": {}}
                            )
                        )
                        await streams[1].send(ValueError(CANARY))
                        replies.append(await request("tools/list"))
                        assert contexts[-2:] == [ambient_id, ambient_id]
                        serialized = json.dumps(replies)
                        assert CANARY not in serialized
                        assert (
                            "traceparent" not in serialized
                            and "tracestate" not in serialized
                        )
            spans = [
                span
                for span in exporter.get_finished_spans()
                if span.instrumentation_scope.name == "mcp-python-sdk"
            ]
            if default:
                assert spans, (
                    "Default SDK must prove the exporter detects instrumentation"
                )
                default_count += len(spans)
            else:
                assert spans == []
    print(
        json.dumps(
            {
                "default_sdk_spans": default_count,
                "solwyn_sdk_spans": 0,
                "protocol_eras": 2,
            }
        )
    )


@pytest.mark.skipif(
    not os.environ.get("SOLWYN_TEST_LEGACY_PYTHON"),
    reason="Set SOLWYN_TEST_LEGACY_PYTHON to a separate mcp==1.28.1 environment",
)
@pytest.mark.parametrize("profile", ["guardrails", "read"])
def test_actual_pre_v2_client_against_stdio(tmp_path, profile):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Authorization"] == f"Bearer {VALID_KEY}"
            assert self.path == "/api/v1/projects/proj_9b33046ed39d182e3adafa90/budget"
            payload = json.dumps(
                {**BUDGET_RESPONSE, "project_id": "proj_9b33046ed39d182e3adafa90"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    executable = os.environ.get(
        "SOLWYN_TEST_SERVER_EXECUTABLE",
        str(
            Path(sys.executable).with_name(
                "solwyn.exe" if os.name == "nt" else "solwyn"
            )
        ),
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    os.environ["SOLWYN_TEST_LEGACY_PYTHON"],
                    str(Path(__file__).with_name("mcp_legacy_probe.py")),
                    executable,
                    f"http://127.0.0.1:{server.server_port}",
                    profile,
                ],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=25,
                env={"PATH": os.environ["PATH"]},
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["client_sdk"] == "1.28.1"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("module", mcp_server._SDK_DIAGNOSTIC_MODULES)
def test_actual_sdk_emitters_scrub_diagnostics_before_existing_handlers(module):
    logger = logging.getLogger(module.__name__)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.__dict__.copy())

    handler = Capture()
    logger.addHandler(handler)
    try:
        mcp_server.create_server()
        mcp_server.create_server()
        assert (
            sum(
                isinstance(item, mcp_server._PrivateMcpDiagnostics)
                for item in logger.filters
            )
            == 1
        )
        try:
            raise RuntimeError(CANARY)
        except RuntimeError:
            record = logging.LogRecord(
                logger.name,
                logging.ERROR,
                module.__file__,
                1,
                "private %s",
                (CANARY,),
                sys.exc_info(),
                sinfo=CANARY,
            )
        record.exc_text = CANARY
        record.message = CANARY
        logger.handle(record)
        assert records[0]["msg"] == "MCP protocol diagnostic."
        assert records[0]["levelno"] == logging.ERROR
        assert CANARY not in repr(records)
        logger.handle(
            logging.LogRecord(
                logger.name, logging.WARNING, __file__, 1, "keep %s", ("useful",), None
            )
        )
        assert records[-1]["msg"] == "keep %s"
        assert records[-1]["args"] == ("useful",)
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize(
    "revision", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
)
def test_every_previously_supported_legacy_revision(monkeypatch, revision):
    monkeypatch.setattr(mcp_server, "_budget_status_operation", synthetic_status)

    async def exercise():
        async with raw_connection(
            mcp_server.create_server(), legacy=True, legacy_version=revision
        ) as (request, _):
            catalog = (await request("tools/list"))["result"]
            assert [tool["name"] for tool in catalog["tools"]] == list(
                mcp_server.TOOL_NAMES
            )
            good = (
                await request("tools/call", {"name": "budget_status", "arguments": {}})
            )["result"]
            bad = (
                await request(
                    "tools/call",
                    {"name": "budget_status", "arguments": {CANARY: CANARY}},
                )
            )["result"]
            assert good["isError"] is False
            assert json.loads(good["content"][0]["text"])["ok"] is True
            assert bad["isError"] is True
            assert (
                json.loads(bad["content"][0]["text"])["error"]["code"]
                == "invalid_arguments"
            )
            assert CANARY not in json.dumps(bad)
            assert "result" in await request("ping")

    anyio.run(exercise)


if __name__ == "__main__":
    anyio.run(telemetry_probe)
