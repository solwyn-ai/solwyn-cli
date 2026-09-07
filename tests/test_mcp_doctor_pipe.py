"""Public Win32 pipe branch checks; these do not substitute for Windows CI."""

from types import SimpleNamespace

import anyio
import pytest

from solwyn_cli import _mcp_doctor_pipe as pipes


class WinFunction:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


def install_kernel(monkeypatch, *, get_ok=True, set_ok=True, read_ok=True, error=0):
    seen = []

    def get(handle, mode, *args):
        mode._obj.value = 0x102
        return get_ok

    def set_mode(handle, mode, *args):
        seen.append(mode._obj.value)
        return set_ok

    def read(handle, buffer, size, received, overlapped):
        assert overlapped is None
        buffer.value = b"ok"
        received._obj.value = 2
        return read_ok

    kernel = SimpleNamespace(
        GetNamedPipeHandleStateW=WinFunction(get),
        SetNamedPipeHandleState=WinFunction(set_mode),
        ReadFile=WinFunction(read),
    )
    monkeypatch.setattr(
        pipes.ctypes, "WinDLL", lambda name, **kwargs: kernel, raising=False
    )
    monkeypatch.setattr(pipes.ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(
        pipes, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 42), raising=False
    )
    return seen


def test_windows_311_pipe_preserves_mode_and_reads_binary(monkeypatch):
    seen = install_kernel(monkeypatch)
    pipe = pipes.WindowsPipe(0)
    assert seen == [0x103]
    assert pipe.read(8) == b"ok"


@pytest.mark.parametrize("error,want", [(232, None), (109, b"")])
def test_windows_would_block_is_distinct_from_all_writers_closed(
    monkeypatch, error, want
):
    install_kernel(monkeypatch, read_ok=False, error=error)
    assert pipes.WindowsPipe(0).read(8) == want


@pytest.mark.parametrize(
    "settings", [{"get_ok": False}, {"set_ok": False}, {"read_ok": False, "error": 5}]
)
def test_windows_unavailable_pipe_fails_with_fixed_text(monkeypatch, settings):
    install_kernel(monkeypatch, **settings)
    with pytest.raises(OSError, match="Private pipe"):
        pipes.WindowsPipe(0).read(8)


def test_windows_polling_is_cancellable_without_a_read_thread():
    calls = []

    def pending(size):
        calls.append(size)
        return None

    async def exercise():
        pipe = object.__new__(pipes.ParentPipe)
        pipe.fd = 0
        pipe.windows = SimpleNamespace(read=pending)
        with anyio.move_on_after(0.07) as scope:
            await pipe.read(1)
        assert scope.cancel_called

    anyio.run(exercise)
    assert 1 <= len(calls) <= 5
