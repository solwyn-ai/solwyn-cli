"""Platform dispatch tests for artifact atomic rename operations."""

import ctypes
import errno
import importlib.util

import pytest

from solwyn_cli.artifacts import _atomic


@pytest.mark.parametrize("platform", ["win32", "freebsd13"])
def test_unsupported_platform_import_does_not_load_posix_library(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    def unavailable_library(*args, **kwargs):
        raise AssertionError("Unsupported platform must not load the POSIX library")

    spec = importlib.util.spec_from_file_location(
        "atomic_import_probe", _atomic.__file__
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(_atomic.sys, "platform", platform)
    monkeypatch.setattr(ctypes, "CDLL", unavailable_library)

    spec.loader.exec_module(module)

    for operation in (module.atomic_exchange, module.atomic_noreplace):
        with pytest.raises(OSError) as captured:
            operation("stage", "target", 17)
        assert captured.value.errno == errno.ENOTSUP


@pytest.mark.parametrize(
    ("platform", "exchange", "function_name", "flag"),
    [
        ("linux", True, "renameat2", 2),
        ("linux", False, "renameat2", 1),
        ("darwin", True, "renameatx_np", 0x00000002),
        ("darwin", False, "renameatx_np", 0x00000004),
    ],
)
def test_atomic_rename_dispatches_platform_flags(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    exchange: bool,
    function_name: str,
    flag: int,
) -> None:
    calls: list[tuple[str, str, str, int, int]] = []
    monkeypatch.setattr(_atomic.sys, "platform", platform)
    monkeypatch.setattr(
        _atomic,
        "_call",
        lambda function, source, target, parent_fd, operation: calls.append(
            (function, source, target, parent_fd, operation)
        ),
    )

    _atomic._dispatch("stage", "target", 17, exchange=exchange)

    assert calls == [(function_name, "stage", "target", 17, flag)]


def test_atomic_rename_fails_closed_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_atomic.sys, "platform", "win32")

    with pytest.raises(OSError) as captured:
        _atomic.atomic_exchange("stage", "target", 17)

    assert captured.value.errno == errno.ENOTSUP
    assert "unsupported" in str(captured.value).lower()


def test_missing_platform_syscall_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_atomic.sys, "platform", "linux")
    monkeypatch.setattr(
        _atomic,
        "_call",
        lambda *args: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "atomic syscall unavailable")
        ),
    )

    with pytest.raises(OSError) as captured:
        _atomic.atomic_noreplace("stage", "target", 17)

    assert captured.value.errno == errno.ENOTSUP
