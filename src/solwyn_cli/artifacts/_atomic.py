"""Descriptor-relative atomic rename operations for Tier-1 POSIX platforms."""

import ctypes
import errno
import os
import sys


# CDLL(None) is a POSIX process-library lookup, unavailable on Windows.
_LIBC = (
    ctypes.CDLL(None, use_errno=True) if sys.platform in {"linux", "darwin"} else None
)
_LINUX_NOREPLACE = 1
_LINUX_EXCHANGE = 2
_MACOS_SWAP = 0x00000002
_MACOS_EXCL = 0x00000004


def _call(
    function_name: str, source: str, target: str, parent_fd: int, flag: int
) -> None:
    try:
        function = getattr(_LIBC, function_name)
    except AttributeError:
        raise OSError(
            errno.ENOTSUP,
            f"Atomic rename operation {function_name} is unavailable.",
        ) from None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number), target)


def _dispatch(source: str, target: str, parent_fd: int, *, exchange: bool) -> None:
    if sys.platform == "linux":
        _call(
            "renameat2",
            source,
            target,
            parent_fd,
            _LINUX_EXCHANGE if exchange else _LINUX_NOREPLACE,
        )
        return
    if sys.platform == "darwin":
        _call(
            "renameatx_np",
            source,
            target,
            parent_fd,
            _MACOS_SWAP if exchange else _MACOS_EXCL,
        )
        return
    raise OSError(
        errno.ENOTSUP,
        f"Atomic artifact renames are unsupported on {sys.platform}.",
    )


def atomic_exchange(source: str, target: str, parent_fd: int) -> None:
    """Atomically swap two existing names without losing either inode."""
    _dispatch(source, target, parent_fd, exchange=True)


def atomic_noreplace(source: str, target: str, parent_fd: int) -> None:
    """Atomically rename source only when target does not exist."""
    _dispatch(source, target, parent_fd, exchange=False)
