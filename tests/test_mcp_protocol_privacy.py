"""Outer protocol privacy across real stdio and the public SDK runner."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
from threading import Thread

import anyio
from mcp import MCPError, types
import pytest

from solwyn_cli.mcp import server as mcp_server
from test_mcp_conformance import BUDGET_RESPONSE, VALID_KEY
from test_mcp_sdk_migration import raw_connection, synthetic_status


CANARY = "PRIVATE_UNKNOWN_METHOD_4ad197"
METHODS = [CANARY, json.dumps({"nested": {"private": CANARY}}), "x" * 8192 + CANARY]


@contextmanager
def stdio_peer(tmp_path, *, legacy):
    """Own all subprocess/HTTP resources; usable against an installed wheel too."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            payload = json.dumps(BUDGET_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    executable = os.environ.get("SOLWYN_TEST_SERVER_EXECUTABLE") or str(
        Path(sys.executable).with_name("solwyn.exe" if os.name == "nt" else "solwyn")
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        http_thread = Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        process = None
        reader_threads = []
        output, diagnostics = [], []
        messages = queue.Queue()
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "--api-url",
                    f"http://127.0.0.1:{http.server_port}",
                    "--project",
                    "proj_alpha",
                    "mcp",
                ],
                cwd=tmp_path,
                env={"PATH": os.environ["PATH"], "SOLWYN_API_KEY": VALID_KEY},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def read_stdout():
                for line in process.stdout:
                    output.append(line)
                    # Preserve malformed framing as an observable assertion failure.
                    try:
                        messages.put(json.loads(line))
                    except json.JSONDecodeError:
                        messages.put({"invalid_json": line})

            def read_stderr():
                diagnostics.extend(process.stderr)

            for reader in (read_stdout, read_stderr):
                thread = Thread(target=reader, daemon=True)
                thread.start()
                reader_threads.append(thread)
            request_id = 0

            def send_raw(raw):
                process.stdin.write(raw + "\n")
                process.stdin.flush()

            def request(method, params=None):
                nonlocal request_id
                request_id += 1
                params = params or {}
                if not legacy:
                    params = {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                        **params,
                    }
                send_raw(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params,
                        }
                    )
                )
                reply = messages.get(timeout=5)
                assert reply.get("id") == request_id, reply
                return reply

            if legacy:
                initialized = request(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "privacy-regression", "version": "1"},
                    },
                )
                assert initialized["result"]["serverInfo"]["name"] == "solwyn"
                send_raw(
                    json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    )
                )
            yield request, send_raw, output, diagnostics, requests
        finally:
            if process is not None:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    for thread in reader_threads:
                        thread.join(timeout=2)
                    process.stdout.close()
                    process.stderr.close()
            http.shutdown()
            http_thread.join(timeout=2)
        assert process.returncode == 0
        assert all(not thread.is_alive() for thread in reader_threads)
        assert not http_thread.is_alive()


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_method_stdio_privacy_and_later_calls(tmp_path, legacy):
    with stdio_peer(tmp_path, legacy=legacy) as (
        request,
        send_raw,
        output,
        diagnostics,
        http_requests,
    ):
        for method in METHODS:
            reply = request(method)
            assert reply["error"] == {"code": -32601, "message": "Method not found"}
            assert CANARY not in json.dumps(reply)
        # Malformed transport inputs remain private and do not end the session.
        send_raw('{"private":"' + CANARY + '"')
        send_raw(
            json.dumps(
                {"jsonrpc": "2.0", "id": {CANARY: CANARY}, "method": "tools/list"}
            )
        )
        catalog = request("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in catalog] == list(mcp_server.TOOL_NAMES)
        unknown_tool = request("tools/call", {"name": CANARY, "arguments": {}})
        assert unknown_tool["error"] == {"code": -32602, "message": "Unknown tool"}
        malformed = request(
            "tools/call", {"name": "budget_status", "arguments": CANARY}
        )
        assert malformed["error"]["code"] == -32602
        invalid = request(
            "tools/call", {"name": "budget_status", "arguments": {"projectid": CANARY}}
        )["result"]
        assert invalid["isError"] is True
        assert invalid["structuredContent"]["error"]["code"] == "invalid_arguments"
        good = request("tools/call", {"name": "budget_status", "arguments": {}})[
            "result"
        ]
        assert good["isError"] is False
        assert good["structuredContent"]["ok"] is True
        assert json.loads(good["content"][0]["text"]) == good["structuredContent"]
        assert good["structuredContent"]["data"]["project_id"] == "proj_alpha"
    assert http_requests == [
        ("/api/v1/projects/proj_alpha/budget", f"Bearer {VALID_KEY}")
    ]
    assert CANARY not in "".join(output + diagnostics)
    assert VALID_KEY not in "".join(output + diagnostics)


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_method_private_before_captured_sdk_logs(monkeypatch, caplog, legacy):
    monkeypatch.setattr(mcp_server, "_budget_status_operation", synthetic_status)
    caplog.set_level(logging.DEBUG)

    async def exercise():
        async with raw_connection(mcp_server.create_server(), legacy=legacy) as (
            request,
            _,
        ):
            for method in METHODS:
                reply = await request(method)
                assert reply["error"] == {"code": -32601, "message": "Method not found"}
            good = await request(
                "tools/call", {"name": "budget_status", "arguments": {}}
            )
            assert good["result"]["structuredContent"]["ok"] is True
            logging.warning("Keep unrelated application diagnostics.")

    anyio.run(exercise)
    assert CANARY not in repr([record.__dict__ for record in caplog.records])
    assert "Keep unrelated application diagnostics." in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        MCPError(
            code=-32602, message="Invalid request parameters", data={"safe": True}
        ),
        RuntimeError("operation failure"),
        asyncio.CancelledError(),
        SystemExit(),
        KeyboardInterrupt(),
    ],
)
def test_protocol_privacy_middleware_preserves_other_exceptions(error):
    async def fail(ctx):
        raise error

    async def exercise():
        with pytest.raises(type(error)) as raised:
            await mcp_server._private_protocol_errors(None, fail)
        assert raised.value is error

    anyio.run(exercise)


def test_protocol_privacy_middleware_preserves_success():
    result = types.ListToolsResult(tools=[])

    async def succeed(ctx):
        return result

    async def exercise():
        assert await mcp_server._private_protocol_errors(None, succeed) is result

    anyio.run(exercise)
