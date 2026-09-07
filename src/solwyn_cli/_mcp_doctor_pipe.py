"""Cancellable single-reader private pipe, including Windows Python 3.11.

Windows anonymous pipes support Get/SetNamedPipeHandleState (PIPE_NOWAIT).
ReadFile distinguishes ERROR_NO_DATA (try later) from ERROR_BROKEN_PIPE (EOF).
This controlled single-reader polling adapter avoids a blocked executor thread;
it is not a general async pipe transport. Existing pipe mode bits are preserved.
"""

import ctypes
from ctypes import wintypes
import os

import anyio

if os.name == "nt":
    import msvcrt


class WindowsPipe:
    """Use documented Win32 anonymous-pipe APIs at the Python 3.11 floor."""

    def __init__(self, fd: int) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        kernel.GetNamedPipeHandleStateW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetNamedPipeHandleState.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        for fn in (
            kernel.GetNamedPipeHandleStateW,
            kernel.SetNamedPipeHandleState,
            kernel.ReadFile,
        ):
            fn.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not kernel.GetNamedPipeHandleStateW(
            self.handle, ctypes.byref(mode), None, None, None, None, 0
        ):
            raise OSError("Private pipe unavailable.")
        mode.value |= 1  # PIPE_NOWAIT; retain all other bits.
        if not kernel.SetNamedPipeHandleState(
            self.handle, ctypes.byref(mode), None, None
        ):
            raise OSError("Private pipe unavailable.")
        self.kernel = kernel

    def read(self, size: int) -> bytes | None:
        buffer = ctypes.create_string_buffer(size)
        received = wintypes.DWORD()
        if self.kernel.ReadFile(
            self.handle, buffer, size, ctypes.byref(received), None
        ):
            return buffer.raw[: received.value]
        error = ctypes.get_last_error()
        if error == 232:  # ERROR_NO_DATA: writer still open, no bytes available.
            return None
        if error == 109:  # ERROR_BROKEN_PIPE: all writers closed.
            return b""
        raise OSError("Private pipe read failed.")


class ParentPipe:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.windows = WindowsPipe(fd) if os.name == "nt" else None

    async def read(self, size: int) -> bytes:
        if self.windows is None:
            await anyio.wait_readable(self.fd)
            return os.read(self.fd, size)
        while True:
            result = self.windows.read(size)
            if result is not None:
                return result
            await anyio.sleep(0.02)
