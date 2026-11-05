"""Synthetic diagnostic process. Copied next to an isolated fixture.json."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "fixture.json").read_text())
KEY = "solwyn_sk_" + "a1" * 32
CANARY = "PRIVATE_DIAGNOSTIC_CANARY"


def record(kind, value):
    with (ROOT / "observations.jsonl").open("a") as output:
        output.write(json.dumps({kind: value}) + "\n")


async def listing(ctx, params):
    record("list", True)
    names = CONFIG["tools"]
    if CONFIG["mode"] == "wrong_inventory":
        names = [*names, CANARY]
    return ListToolsResult(
        tools=[Tool(name=name, input_schema={"type": "object"}) for name in names]
    )


async def call(ctx, params):
    record(
        "call",
        {
            "name": params.name,
            "arguments": params.arguments,
            "propagation": {
                key: key in (params.meta or {})
                for key in ("traceparent", "tracestate", "baggage")
            },
        },
    )
    if CONFIG["mode"] == "stall_read":
        await anyio.sleep_forever()
    if CONFIG["mode"] == "bad_read":
        body = {
            "schema": "solwyn.mcp.budget-status.v1",
            "ok": False,
            "data": None,
            "error": {"message": CANARY},
        }
        return CallToolResult(
            content=[TextContent(type="text", text=CANARY)],
            structured_content=body,
            is_error=True,
        )
    body = {
        "schema": "solwyn.mcp.budget-status.v1",
        "ok": True,
        "data": {"project_id": CONFIG["project"]},
        "error": None,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
    )


async def serve():
    server = Server(
        "wrong" if CONFIG["mode"] == "wrong_identity" else "solwyn",
        version=CONFIG["version"],
        on_list_tools=listing,
        on_call_tool=call,
    )
    server.middleware.clear()  # Positive propagation control belongs to this fixture.
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


def main():
    if "--descendant" in sys.argv:
        (ROOT / "descendant.pid").write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    (ROOT / "server.pid").write_text(str(os.getpid()))
    record(
        "environment",
        {
            "intended_key": os.environ.get("SOLWYN_API_KEY") == KEY,
            "empty_key": os.environ.get("SOLWYN_API_KEY") == "",
            "ambient": [
                name
                for name in (
                    "OPENAI_API_KEY",
                    "SOLWYN_TAGS",
                    "PYTHONPATH",
                    "OTEL_EXPORTER_OTLP_ENDPOINT",
                    "TRACEPARENT",
                    "BAGGAGE",
                )
                if name in os.environ
            ],
        },
    )
    mode = CONFIG["mode"]
    if mode in {"stubborn", "leader_exits"}:
        subprocess.Popen([sys.executable, "-I", __file__, "--descendant"])
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    elif mode in {"eof_exit", "leader_exits"}:
        for line in sys.stdin.buffer:
            pass
    elif mode == "malformed":
        print(CANARY, flush=True)
        print(CANARY * 1000, file=sys.stderr, flush=True)
    else:
        anyio.run(serve)


if __name__ == "__main__":
    main()
