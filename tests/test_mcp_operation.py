"""Bounds apply to actual streamed responses and subsequent worker effects."""

import time
from functools import partial

import anyio
import httpx
import pytest
from pydantic import BaseModel

from solwyn_cli import _mcp_operation as operation
from solwyn_cli import mcp
from solwyn_cli.commands import budget


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await anyio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status", [200, 400, 401, 429, 500])
def test_every_status_is_streamed_and_closed_at_aggregate_cap(monkeypatch, status):
    monkeypatch.setattr(operation, "MAX_RESPONSE_BYTES", 8)
    body = Body([b"1234", b"56789"])
    sent = []

    async def handler(request):
        sent.append(request)
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, stream=body)

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(operation.OperationLimit, match="response_size_limit"):
                await operation.run_sync(partial(_read, control))
        assert body.closed
        assert len(sent) == 1

    anyio.run(exercise)


def _read(control):
    with httpx.Client(transport=control.transport()) as client:
        return client.get("https://synthetic.invalid/read").content


def test_deadline_interrupts_trickle_and_prevents_next_effect(monkeypatch):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.04)
    body = Body([b"a"] * 100, delay=0.01)
    effects = []

    async def handler(request):
        effects.append(request.url.path)
        return httpx.Response(200, stream=body)

    async def exercise():
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with operation.operation_scope(
                transport=httpx.MockTransport(handler)
            ) as control:
                await operation.run_sync(partial(_read, control))
        assert time.monotonic() - started < 0.5
        assert body.closed
        assert effects == ["/read"]

    anyio.run(exercise)


def test_cancel_wakes_retry_sleep_and_blocks_second_send():
    sent = []
    sleeping = anyio.Event()

    async def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, content=b"ok")

    def work(control):
        _read(control)
        anyio.from_thread.run_sync(sleeping.set)
        control.sleep(10)
        _read(control)

    async def exercise():
        async with anyio.create_task_group() as group:

            async def run():
                async with operation.operation_scope(
                    transport=httpx.MockTransport(handler)
                ) as control:
                    await operation.run_sync(partial(work, control))

            group.start_soon(run)
            await sleeping.wait()
            group.cancel_scope.cancel()
        await anyio.sleep(0.02)
        assert sent == ["/read"]

    anyio.run(exercise)


def test_existing_package_exports_and_command_imports_remain_usable():
    assert callable(mcp.create_server)
    assert callable(mcp.tool_call_client)
    assert callable(budget.request_budget_check)


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "zstd"])
def test_unexpected_compression_is_rejected_without_decoding(encoding):
    body = Body([b"not actually compressed"])

    async def handler(request):
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=body)

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(
                operation.OperationLimit, match="response_encoding_unsupported"
            ):
                await operation.run_sync(partial(_read, control))
        assert body.closed

    anyio.run(exercise)


def test_request_body_cap_rejects_before_any_send(monkeypatch):
    monkeypatch.setattr(operation, "MAX_REQUEST_BYTES", 8)
    sent = []

    async def handler(request):
        sent.append(request)
        return httpx.Response(200, content=b"ok")

    def work(control):
        with httpx.Client(transport=control.transport()) as client:
            client.post("https://synthetic.invalid/read", content=b"123456789")

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(operation.OperationLimit, match="request_size_limit"):
                await operation.run_sync(partial(work, control))
        assert not sent

    anyio.run(exercise)


def test_page_responses_share_one_aggregate_body_budget(monkeypatch):
    monkeypatch.setattr(operation, "MAX_RESPONSE_BYTES", 8)
    bodies = []

    async def handler(request):
        body = Body([b"12345"])
        bodies.append(body)
        return httpx.Response(200, stream=body)

    def work(control):
        operation.page("one")
        assert _read(control) == b"12345"
        operation.page("two")
        _read(control)
        pytest.fail("aggregate cap must reject page two")

    async def exercise():
        async with operation.operation_scope(
            transport=httpx.MockTransport(handler)
        ) as control:
            with pytest.raises(operation.OperationLimit, match="response_size_limit"):
                await operation.run_sync(partial(work, control))
            assert control.recovery().cursor == "two"
        assert len(bodies) == 2 and all(body.closed for body in bodies)

    anyio.run(exercise)


def test_worker_admission_wait_does_not_renew_deadline(monkeypatch):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.04)
    admitted = []

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            await limiter.acquire_on_behalf_of("test-held-token")
            try:
                with pytest.raises(TimeoutError):
                    async with operation.operation_scope():
                        await operation.run_sync(lambda: admitted.append(True))
            finally:
                limiter.release_on_behalf_of("test-held-token")
            await anyio.sleep(0.02)
            assert not admitted
        finally:
            limiter.total_tokens = original

    anyio.run(exercise)


def test_cleanup_stays_bounded_when_stream_and_pool_close_hang(monkeypatch):
    monkeypatch.setattr(operation, "DEADLINE_SECONDS", 0.04)
    close_calls = []

    class UncooperativeBody(Body):
        async def aclose(self):
            close_calls.append("stream")
            await anyio.sleep_forever()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200, stream=UncooperativeBody([b"a"] * 100, delay=0.01)
            )

        async def aclose(self):
            close_calls.append("pool")
            await anyio.sleep_forever()

    async def exercise():
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            async with operation.operation_scope(transport=Transport()) as control:
                await operation.run_sync(partial(_read, control))
        assert time.monotonic() - start < 0.5
        assert close_calls == ["stream", "pool"]

    anyio.run(exercise)


class KnownOutcome(BaseModel):
    reservation_id: str


def test_failed_publication_preserves_both_views_and_snapshots_are_detached():
    first = KnownOutcome(reservation_id="first")
    later = KnownOutcome(reservation_id="later")
    item = operation.RecoveryItem(index=0, state="completed", reservation_id="first")

    async def exercise():
        async with operation.operation_scope() as control:
            control.retain(
                first,
                reservation_id=first.reservation_id,
                settlement="confirmed",
                items=[item],
            )
            with pytest.raises(ValueError):
                control.retain(later, reservation_id="x" * 257)
            captured = control.snapshot()
            assert (
                captured.data.reservation_id
                == captured.recovery.reservation_id
                == "first"
            )
            assert captured.recovery.settlement == "confirmed"
            first.reservation_id = "caller-mutation"
            item.reservation_id = "caller-compact-mutation"
            captured.data.reservation_id = "snapshot-mutation"
            captured.recovery.reservation_id = "snapshot-compact-mutation"
            next_snapshot = control.snapshot()
            assert (
                next_snapshot.data.reservation_id
                == next_snapshot.recovery.reservation_id
                == "first"
            )
            assert next_snapshot.recovery.items[0].reservation_id == "first"

    anyio.run(exercise)
