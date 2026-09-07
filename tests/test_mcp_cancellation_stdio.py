"""Actual current/legacy Solwyn stdio cancellation and EOF suppress later HTTP."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_mcp_conformance import VALID_KEY, CHECK_RESPONSE
from test_alerts_command import WINDOW


CHILD = """import sys, time
from pathlib import Path
from solwyn_cli import _mcp_operation as operation
from solwyn_cli.commands import _record
from solwyn_cli.main import app
marker = Path(sys.argv[1])
scenario = sys.argv[2]
sys.argv = ["solwyn", *sys.argv[3:]]
if scenario == "recording":
    original = _record.request_ingest
    def delayed(**kwargs):
        marker.write_text("ready")
        time.sleep(0.2)
        return original(**kwargs)
    _record.request_ingest = delayed
elif scenario == "retry":
    original = operation.OperationControl.sleep
    def delayed(self, seconds):
        marker.write_text("ready")
        return original(self, seconds)
    operation.OperationControl.sleep = delayed
elif scenario == "page":
    original = operation.OperationControl.page
    def delayed(self, cursor):
        if cursor is not None:
            marker.write_text("ready")
            time.sleep(0.2)
        return original(self, cursor)
    operation.OperationControl.page = delayed
app()
"""


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("ending", ["cancel", "eof"])
@pytest.mark.parametrize("scenario", ["stream", "retry", "recording", "page"])
def test_stdio_stop_prevents_later_effects(tmp_path, legacy, ending, scenario):
    requests = []
    first = threading.Event()
    release = threading.Event()
    marker = tmp_path / "entered"

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            requests.append((self.command, self.path))
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            first.set()
            if scenario == "retry":
                status, body = 429, b"{}"
            elif scenario == "page":
                status, body = (
                    200,
                    json.dumps(
                        {"events": [], "next_cursor": "next", "window": WINDOW}
                    ).encode(),
                )
            elif scenario == "recording":
                status, body = 204, b""
            else:
                status, body = 200, json.dumps(CHECK_RESPONSE).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if scenario == "retry":
                self.send_header("Retry-After", "10")
            self.end_headers()
            try:
                if scenario == "stream":
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    release.wait(3)
                    self.wfile.write(body[1:])
                else:
                    self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = respond
        do_POST = respond

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR", "USERPROFILE"}
    }
    env["SOLWYN_API_KEY"] = VALID_KEY
    process = subprocess.Popen(
        [
            sys.executable,
            str(child),
            str(marker),
            scenario,
            "--api-url",
            f"http://127.0.0.1:{http.server_port}",
            "--project",
            "proj_alpha",
            "mcp",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output, errors = [], []
    messages = queue.Queue()

    def drain(stream, lines, enqueue=False):
        for line in stream:
            lines.append(line)
            if enqueue:
                messages.put(json.loads(line))

    readers = [
        threading.Thread(
            target=drain, args=(process.stdout, output, True), daemon=True
        ),
        threading.Thread(target=drain, args=(process.stderr, errors), daemon=True),
    ]
    for reader in readers:
        reader.start()

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    meta = (
        {}
        if legacy
        else {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "synthetic-bounds",
                    "version": "0",
                },
            }
        }
    )
    try:
        if legacy:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-bounds", "version": "0"},
                    },
                }
            )
            assert messages.get(timeout=5)["result"]["protocolVersion"] == "2025-11-25"
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if scenario == "recording":
            tool, args = (
                "budget_confirm",
                {
                    "reservation_id": "known",
                    "call_id": "12345678-1234-4000-8000-123456789abc",
                    "model": "gpt-4o",
                    "provider": "openai",
                },
            )
        elif scenario == "page":
            tool, args = "list_alert_events", {"all_pages": True}
        elif scenario == "retry":
            tool, args = "budget_status", {}
        else:
            tool, args = (
                "budget_check",
                {"model": "gpt-4o", "provider": "openai", "estimated_input_tokens": 1},
            )
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args, **meta},
            }
        )
        assert first.wait(5)
        if scenario != "stream":
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            assert marker.exists(), (output, errors)
        start = time.monotonic()
        if ending == "cancel":
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 1},
                }
            )
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": meta})
            reply = messages.get(timeout=3)
            assert reply["id"] == 2 and len(reply["result"]["tools"]) == 17
        process.stdin.close()
        process.wait(timeout=3)
        release.set()
        for reader in readers:
            reader.join(1)
        assert time.monotonic() - start < 2
        assert process.returncode == 0
        assert len(requests) == 1
        assert errors == []
        cancelled_replies = [
            json.loads(line) for line in output if json.loads(line).get("id") == 1
        ]
        if ending == "cancel":
            assert cancelled_replies == []
        else:
            # SDK2 may send its fixed connection-closed protocol error on EOF;
            # it never returns a manufactured application success.
            assert all(
                reply.get("error") == {"code": -32000, "message": "Connection closed"}
                for reply in cancelled_replies
            )
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        http.shutdown()
        http.server_close()
        http_thread.join(1)
