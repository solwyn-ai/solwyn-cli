"""One MCP invocation's deadline, cancellation fence, and bounded HTTP owner.

Ordinary CLI helpers see no active control. Cancelling an invocation stops future
requests; it cannot undo an admitted request or terminate an OS keychain call.
"""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from threading import Event, Lock
from typing import Any, Literal, TypeVar

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

DEADLINE_SECONDS = 30.0
CLEANUP_SECONDS = 0.25
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 1 << 20
MAX_REQUEST_BYTES = 4 << 20
MAX_RESULT_BYTES = 2 << 20
# The exact pinned SDK adds only bounded protocol metadata to this result.
RESULT_METADATA_ALLOWANCE = 4096

T = TypeVar("T")
_current: ContextVar["OperationControl | None"] = ContextVar(
    "mcp_operation", default=None
)


class RecoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=31)
    state: Literal["completed", "failed", "unknown", "unattempted"]
    reservation_id: str | None = Field(default=None, max_length=256)
    call_id: str | None = Field(default=None, max_length=36)
    recording_status: str | None = Field(default=None, max_length=32)
    checked_at: str | None = Field(default=None, max_length=40)
    reservation_expires_at: str | None = Field(default=None, max_length=40)


class ReadRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(default=None, max_length=4096)


class OperationRecovery(BaseModel):
    """Compact known identities, never a new grant of spend authority."""

    model_config = ConfigDict(extra="forbid")
    operation_id: str | None = Field(default=None, max_length=36)
    state: (
        Literal["not_submitted", "unknown", "running", "finished", "interrupted"] | None
    ) = None
    items: list[RecoveryItem] = Field(default_factory=list, max_length=32)
    reservation_id: str | None = Field(default=None, max_length=256)
    call_id: str | None = Field(default=None, max_length=36)
    settlement: Literal["not_submitted", "unknown", "confirmed"] | None = None
    recording: (
        Literal[
            "not_submitted",
            "unknown",
            "ingested",
            "deduplicated",
            "failed",
            "rejected",
            "skipped",
            "stopped",
        ]
        | None
    ) = None
    cursor: str | None = Field(default=None, max_length=4096)


class OperationLimit(Exception):
    """Fixed error code; never includes remote data or exception text."""

    def __init__(self, code: str):
        self.code = code
        if control := _current.get():
            control._stop.set()
        super().__init__(code)


class OperationSnapshot(BaseModel):
    """One detached observation of retained data and its compact recovery."""

    model_config = ConfigDict(frozen=True)
    data: BaseModel | None
    recovery: OperationRecovery
    initial_cursor: str | None


class OperationControl(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    deadline: float
    sender: httpx.AsyncClient = Field(exclude=True)
    _stop: Event = PrivateAttr(default_factory=Event)
    _lock: Any = PrivateAttr(default_factory=Lock)
    _recovery: OperationRecovery = PrivateAttr(default_factory=OperationRecovery)
    _retained: BaseModel | None = PrivateAttr(default=None)
    _response_bytes: int = PrivateAttr(default=0)
    _pages: int = PrivateAttr(default=0)
    _initial_cursor: str | None = PrivateAttr(default=None)
    _active: int = PrivateAttr(default=0)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def checkpoint(self) -> None:
        if self._stop.is_set() or not self.remaining():
            raise OperationLimit("operation_deadline")
        # Invoked only inside workers admitted by run_sync().
        anyio.from_thread.check_cancelled()

    def remember(self, **values: Any) -> None:
        with self._lock:
            self._recovery = OperationRecovery.model_validate(
                {**self._recovery.model_dump(), **values}
            ).model_copy(deep=True)

    def recovery(self) -> OperationRecovery:
        with self._lock:
            return self._recovery.model_copy(deep=True)

    def retain(self, data: BaseModel, **values: Any) -> None:
        # Prepare the detached full view before taking the publication lock.
        # Validation failure must leave both previously published views intact.
        retained = data.model_copy(deep=True)
        with self._lock:
            recovery = OperationRecovery.model_validate(
                {**self._recovery.model_dump(), **values}
            ).model_copy(deep=True)
            self._retained = retained
            self._recovery = recovery

    def snapshot(self) -> OperationSnapshot:
        # Published objects are replaced, never mutated. Capture their references
        # together, then copy outside the lock so a reader cannot block a worker
        # while copying a large result (or vice versa).
        with self._lock:
            retained, recovery, initial_cursor = (
                self._retained,
                self._recovery,
                self._initial_cursor,
            )
        return OperationSnapshot(
            data=retained.model_copy(deep=True) if retained is not None else None,
            recovery=recovery.model_copy(deep=True),
            initial_cursor=initial_cursor,
        )

    def page(self, cursor: str | None) -> None:
        self.checkpoint()
        if cursor is not None and len(cursor) > 4096:
            raise OperationLimit("cursor_size_limit")
        with self._lock:
            if self._pages == 0:
                self._initial_cursor = cursor
            self._recovery = self._recovery.model_copy(update={"cursor": cursor})
            if self._pages >= MAX_PAGES:
                raise OperationLimit("page_limit")
            self._pages += 1

    def sleep(self, delay: float) -> None:
        self.checkpoint()
        self._stop.wait(min(delay, self.remaining()))
        self.checkpoint()

    def transport(self) -> httpx.BaseTransport:
        return _OperationTransport(self)

    async def send(self, request: httpx.Request, content: bytes) -> httpx.Response:
        if self._stop.is_set() or not self.remaining():
            raise OperationLimit("operation_deadline")
        self._active += 1
        response = None
        try:
            remaining = self.remaining()
            phases = request.extensions.get("timeout", {})
            timeout = httpx.Timeout(
                **{
                    phase: min(phases.get(phase) or remaining, remaining)
                    for phase in ("connect", "read", "write", "pool")
                }
            )
            headers = request.headers.copy()
            headers["Accept-Encoding"] = "identity"
            outbound = self.sender.build_request(
                request.method,
                request.url,
                headers=headers,
                content=content,
                timeout=timeout,
            )
            with anyio.fail_after(remaining):
                response = await self.sender.send(outbound, stream=True)
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise OperationLimit("response_encoding_unsupported")
                body = bytearray()
                async for chunk in _response_chunks(response):
                    if self._stop.is_set():
                        raise OperationLimit("operation_deadline")
                    self._response_bytes += len(chunk)
                    if self._response_bytes > MAX_RESPONSE_BYTES:
                        raise OperationLimit("response_size_limit")
                    body.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(body),
                    request=request,
                )
        finally:
            # Closing a stream must not be interrupted by its cancelled request.
            if response is not None:
                with anyio.move_on_after(CLEANUP_SECONDS / 2, shield=True):
                    await response.aclose()
            self._active -= 1


async def _response_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    # Mock transports can return a buffered response; real sockets are streamed.
    if response.is_stream_consumed:
        yield response.content
    else:
        async for chunk in response.aiter_raw():
            yield chunk


class _OperationTransport(httpx.BaseTransport):
    def __init__(self, control: OperationControl):
        self.control = control

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.control.checkpoint()
        # Solwyn sends pre-encoded JSON bodies; do not consume arbitrary iterators
        # on the event loop or silently truncate a mutation body.
        try:
            content = request.content
        except httpx.RequestNotRead:
            raise OperationLimit("request_stream_unsupported") from None
        if len(content) > MAX_REQUEST_BYTES:
            raise OperationLimit("request_size_limit")
        return anyio.from_thread.run(self.control.send, request, content)

    def close(self) -> None:
        # The async invocation, not the synchronous helper, owns the pool.
        pass


def current() -> OperationControl | None:
    return _current.get()


def checkpoint() -> None:
    if control := current():
        control.checkpoint()


def page(cursor: str | None) -> None:
    if control := current():
        control.page(cursor)


def remember(**values: Any) -> None:
    if control := current():
        control.remember(**values)


def retain(data: BaseModel, **recovery: Any) -> None:
    if control := current():
        control.retain(data, **recovery)


async def run_sync(fn: Callable[[], T]) -> T:
    return await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)


@asynccontextmanager
async def operation_scope(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncIterator[OperationControl]:
    # Set the deadline before creating a pool or waiting for a worker token.
    deadline = time.monotonic() + DEADLINE_SECONDS
    sender = httpx.AsyncClient(transport=transport, follow_redirects=False)
    control = OperationControl(deadline=deadline, sender=sender)
    token = _current.set(control)
    try:
        with anyio.fail_after(control.remaining()):
            yield control
    finally:
        control._stop.set()
        # Abandoned from_thread.run() finalizers need a bounded turn to unwind.
        with anyio.move_on_after(CLEANUP_SECONDS / 2, shield=True):
            while control._active:
                await anyio.sleep(0.001)
        with anyio.move_on_after(CLEANUP_SECONDS / 2, shield=True):
            await sender.aclose()
        _current.reset(token)
