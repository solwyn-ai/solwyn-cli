"""Actual Solwyn HTTP/auth helpers through the operation-owned async bridge."""

import json
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import httpx
import pytest

from solwyn_cli import auth, _mcp_operation as operation
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import budget
from solwyn_cli._vendor.batch import RecoverableBudgetCheckRequest, new_operation_id
from test_budget_batch import CHECK
from test_mcp_conformance import VALID_KEY
from test_mcp_operation import Body
from login_fixtures import login_credential


KEY = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
JWT = login_credential("synthetic-old-access")


def client_work(control, credential, path="/api/v1/read"):
    with SolwynClient(
        api_url="https://synthetic.invalid",
        credential=credential,
        transport=control.transport(),
        sleeper=control.sleep,
    ) as client:
        return client.request("GET", path).json()


@pytest.mark.parametrize("status", [200, 401, 429, 500])
def test_oversized_status_body_cannot_trigger_any_retry(monkeypatch, status):
    monkeypatch.setattr(operation, "MAX_RESPONSE_BYTES", 16)
    sent = []
    body = Body([b"x" * 17])

    async def handler(request):
        sent.append(request.url.path)
        return httpx.Response(status, stream=body, headers={"Retry-After": "0"})

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(operation.OperationLimit, match="response_size_limit"):
                await operation.run_sync(partial(client_work, control, JWT))
        assert sent == ["/api/v1/read"]
        assert body.closed

    anyio.run(exercise)


def test_401_through_the_operation_bridge_sends_nothing_else_and_writes_nothing(
    monkeypatch,
):
    sent, writes = [], []
    monkeypatch.setattr(auth, "_set_password", lambda *args: writes.append(args))

    async def handler(request):
        sent.append(request.url.path)
        return httpx.Response(401, content=b"{}")

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(auth.AuthError):
                await operation.run_sync(partial(client_work, control, JWT))
        assert sent == ["/api/v1/read"]
        assert not writes

    anyio.run(exercise)


@pytest.mark.parametrize("style", ["slow_headers", "trickle"])
def test_real_socket_obeys_total_deadline_and_closes_stream(monkeypatch, style):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.08)
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if style == "slow_headers":
                    time.sleep(0.25)
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(100):
                    self.wfile.write(b"a")
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                done.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def work(control):
        with SolwynClient(
            api_url=f"http://127.0.0.1:{server.server_port}",
            credential=KEY,
            transport=control.transport(),
            sleeper=control.sleep,
        ) as client:
            client.request("GET", "/api/v1/read")

    async def exercise():
        start = time.monotonic()
        with pytest.raises((TimeoutError, operation.OperationLimit)):
            async with operation.operation_scope() as control:
                await operation.run_sync(partial(work, control))
        assert time.monotonic() - start < 0.6

    try:
        anyio.run(exercise)
        assert done.wait(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_maximum_length_unicode_check_fixture_fits_request_budget():
    # Exercise all bounded request strings at four UTF-8 bytes per character.
    raw = {
        **CHECK,
        "model": "🧪" * 2048,
        "fallback_models": ["🧪" * 2048] * 8,
        "fallback_providers": ["openai"] * 8,
        "agent_run_id": "🧪" * 256,
        "tags": {str(index) + "🧪" * 63: "🧪" * 256 for index in range(10)},
        "estimated_media": {"resolution": "🧪" * 32, "quality": "🧪" * 32},
    }
    checks = budget.prepare_batch_checks([raw] * 32)
    body = RecoverableBudgetCheckRequest(operation_id=new_operation_id(), checks=checks)
    request = httpx.Request(
        "POST",
        "https://synthetic.invalid/batch",
        json=body.model_dump(mode="json", exclude_none=True),
    )
    assert 2_000_000 < len(request.content) < operation.MAX_REQUEST_BYTES
    assert json.loads(request.content)["checks"][31]["model"] == raw["model"]
