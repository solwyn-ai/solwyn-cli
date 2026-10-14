"""Behavioral controls for the portable installed-wheel acceptance harness."""

import ctypes
import errno
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / "scripts/mcp_wheel_smoke.py"
    spec = importlib.util.spec_from_file_location("wheel_runner_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolhelpKernel:
    """Only the documented snapshot boundary; never opens another process."""

    def __init__(self):
        self.handle = 1 << 40
        self.rows = [(100, 1), (200, 100), (300, 200)]
        self.error = 0
        self.create_error = self.first_error = self.next_error = self.close_error = 0
        self.CreateToolhelp32Snapshot = Mock(side_effect=self.snapshot)
        self.Process32FirstW = Mock(side_effect=self.first)
        self.Process32NextW = Mock(side_effect=self.next)
        self.CloseHandle = Mock(side_effect=self.close)

    def snapshot(self, flags, pid):
        assert (flags, pid) == (2, 0)  # TH32CS_SNAPPROCESS, all processes.
        self.error = self.create_error
        return ctypes.c_void_p(-1).value if self.error else self.handle

    def first(self, handle, pointer):
        assert handle == self.handle
        entry = pointer._obj
        assert entry.dwSize == (568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556)
        self.remaining = iter(self.rows)
        self.error = self.first_error
        return False if self.error else self._row(pointer)

    def next(self, handle, pointer):
        assert handle == self.handle
        self.error = self.next_error
        return False if self.error else self._row(pointer)

    def _row(self, pointer):
        row = next(self.remaining, None)
        if row is None:
            self.error = 18  # ERROR_NO_MORE_FILES, normal end of enumeration.
            return False
        pointer._obj.th32ProcessID, pointer._obj.th32ParentProcessID = row
        return True

    def close(self, handle):
        assert handle == self.handle
        self.error = self.close_error
        return not self.error


@pytest.fixture
def windows_kernel(runner, monkeypatch):
    kernel = ToolhelpKernel()
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    kernel.loader = Mock(return_value=kernel)
    monkeypatch.setattr(ctypes, "WinDLL", kernel.loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.error, raising=False)

    def no_shell_inventory(*args, **kwargs):
        raise AssertionError("Windows process inventory must not spawn a shell")

    monkeypatch.setattr(runner, "checked", no_shell_inventory)
    return kernel


def test_windows_inventory_returns_pid_parent_map_and_closes_snapshot(
    runner, windows_kernel
):
    table = runner.process_table()
    assert table == {100: 1, 200: 100, 300: 200}
    assert runner.descendants(table, 100) == {200, 300}
    windows_kernel.CloseHandle.assert_called_once_with(windows_kernel.handle)
    assert windows_kernel.CreateToolhelp32Snapshot.restype is ctypes.c_void_p


def test_each_inventory_is_fresh_without_reloading_the_windows_library(
    runner, windows_kernel
):
    assert runner.process_table() == {100: 1, 200: 100, 300: 200}
    windows_kernel.rows = [(100, 1), (400, 100)]
    assert runner.process_table() == {100: 1, 400: 100}
    windows_kernel.loader.assert_called_once_with("kernel32", use_last_error=True)
    assert windows_kernel.CloseHandle.call_count == 2


@pytest.mark.parametrize(
    ("stage", "error", "closes"),
    [
        ("create_error", 5, False),
        ("first_error", 5, True),
        ("first_error", 18, True),
        ("next_error", 5, True),
        ("close_error", 6, True),
    ],
)
def test_windows_inventory_errors_never_look_like_an_empty_or_partial_table(
    runner, windows_kernel, stage, error, closes
):
    setattr(windows_kernel, stage, error)
    with pytest.raises(OSError) as captured:
        runner.process_table()
    assert captured.value.errno == error
    assert windows_kernel.CloseHandle.call_count == int(closes)


def test_windows_inventory_preserves_enumeration_failure_if_close_also_fails(
    runner, windows_kernel
):
    windows_kernel.next_error = 5
    windows_kernel.close_error = 6
    with pytest.raises(OSError) as captured:
        runner.process_table()
    assert captured.value.errno == 5
    assert "snapshot close" in " ".join(captured.value.__notes__).lower()
    windows_kernel.CloseHandle.assert_called_once()


def test_inventory_failure_stops_owned_client_and_preserves_original_error(
    runner, monkeypatch, tmp_path
):
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    first_error = OSError("first inventory failure")
    later_error = OSError("later cleanup failure")
    inventories = iter([first_error, later_error])

    def unavailable_inventory():
        raise next(inventories)

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "process_table", unavailable_inventory)
    with pytest.raises(OSError) as captured:
        runner.run_client(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=runner.baseline(),
            expected=0,
            observe=True,
        )
    assert captured.value is first_error
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_inventory_refresh_failure_still_cleans_all_observed_children(
    runner, monkeypatch, tmp_path
):
    processes = []
    popen = subprocess.Popen
    environment = runner.baseline()
    first_error = OSError("inventory refresh failed")
    cleanup_error = OSError("cleanup inventory failed")

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    inventories = iter([None, first_error, cleanup_error])

    def inventory():
        error = next(inventories)
        if error is not None:
            raise error
        return {100001: processes[0].pid, 100002: 100001}

    def terminate_observed(pid, sig):
        if pid == 100001:
            raise PermissionError("synthetic cleanup failure")

    kill = Mock(side_effect=terminate_observed)
    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "process_table", inventory)
    monkeypatch.setattr(runner, "os", SimpleNamespace(kill=kill))
    with pytest.raises(OSError) as captured:
        runner.run_client(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=environment,
            expected=0,
            observe=True,
        )
    assert captured.value is first_error
    assert {call.args[0] for call in kill.call_args_list} == {100001, 100002}
    assert "cleanup" in " ".join(captured.value.__notes__).lower()
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_surviving_observed_child_still_fails_acceptance(runner, monkeypatch, tmp_path):
    statuses = iter([None, 1, 1])
    process = SimpleNamespace(
        pid=1000,
        returncode=1,
        poll=lambda: next(statuses),
        communicate=lambda timeout: ('{"status":"client_error"}', ""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    ticks = iter(range(0, 60, 3))
    kill = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(runner, "process_table", lambda: {1001: 1000})
    monkeypatch.setattr(runner, "os", SimpleNamespace(kill=kill))
    monkeypatch.setattr(
        runner,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None),
    )
    with pytest.raises(AssertionError, match="left an observed child alive"):
        runner.run_client(["synthetic"], cwd=tmp_path, env={}, expected=1, observe=True)
    kill.assert_called_once_with(1001, runner.signal.SIGTERM)
    assert process.stdout.closed and process.stderr.closed


@pytest.fixture
def api_handler(runner):
    handler = object.__new__(runner.APIHandler)
    handler.command = "GET"
    handler.server = SimpleNamespace(mode="success", requests=[])
    handler.path = f"/api/v1/projects/{runner.PROJECT}/budget"
    handler.headers = {"Authorization": "Bearer " + runner.KEY}
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = io.BytesIO()
    return handler


def test_synthetic_api_still_writes_the_expected_budget(runner, api_handler):
    api_handler.do_GET()
    assert json.loads(api_handler.wfile.getvalue()) == runner.BUDGET
    assert api_handler.server.requests == [("GET", api_handler.path)]


@pytest.mark.parametrize(
    "error", [BrokenPipeError(), ConnectionResetError(), ConnectionAbortedError()]
)
def test_synthetic_api_accepts_only_expected_client_close_errors(api_handler, error):
    api_handler.wfile = SimpleNamespace(write=Mock(side_effect=error))
    api_handler.do_GET()
    assert api_handler.server.requests == [("GET", api_handler.path)]


def test_synthetic_api_does_not_hide_other_write_failures(api_handler):
    error = OSError(errno.EIO, "synthetic unexpected write failure")
    api_handler.wfile = SimpleNamespace(write=Mock(side_effect=error))
    with pytest.raises(OSError) as captured:
        api_handler.do_GET()
    assert captured.value is error


@pytest.mark.parametrize("observed", [0, 2])
def test_request_count_failure_reports_lane_and_delta_without_request_data(
    runner, observed
):
    api = SimpleNamespace(requests=[("GET", "PRIVATE_ROUTE_CANARY")] * (3 + observed))
    with pytest.raises(AssertionError) as captured:
        runner.assert_request_count(api, 3, 1, lane="solwyn/python/modern cancellation")
    assert str(captured.value) == (
        "solwyn/python/modern cancellation: expected HTTP request delta 1, "
        f"observed {observed}"
    )


def test_request_count_accepts_exact_delta(runner):
    api = SimpleNamespace(requests=[("GET", "synthetic")] * 4)
    runner.assert_request_count(api, 3, 1, lane="solwyn-cli/typescript/legacy read")


@pytest.mark.parametrize("cancellation_requests", [0, 1, 2])
@pytest.mark.parametrize("auth_status", ["execution_error", "client_error"])
@pytest.mark.parametrize("read_tools", [13, 30, 31])
def test_acceptance_requires_one_inflight_read_after_session_startup(
    runner, monkeypatch, tmp_path, cancellation_requests, auth_status, read_tools
):
    api = SimpleNamespace(
        mode="success",
        requests=[],
        release=runner.Event(),
        server_port=12345,
        serve_forever=Mock(),
        shutdown=Mock(),
        server_close=Mock(),
    )
    monkeypatch.setattr(runner, "SyntheticAPI", lambda: api)
    monkeypatch.setattr(runner, "Thread", Mock())
    monkeypatch.setattr(runner, "checked", lambda *args, **kwargs: "{}")
    cancellations = []

    def client(command, *, cwd, env, expected, observe=False):
        if "--validation-error" in command:
            return {"code": "invalid_arguments"}
        if observe:
            assert expected == 1
            # Model a session that starts in 4 seconds: valid for the public
            # default, but it must not be cancelled before reaching HTTP.
            timeout = (
                float(command[command.index("--timeout") + 1])
                if "--timeout" in command
                else 10
            )
            if timeout > 4:
                api.requests.extend(
                    [("GET", f"/api/v1/projects/{runner.PROJECT}/budget")]
                    * cancellation_requests
                )
            cancellations.append(command)
            return {"status": "client_error", "observed_children_exited": 2}
        api.requests.append(("GET", f"/api/v1/projects/{runner.PROJECT}/budget"))
        if api.mode == "auth":
            return {"status": auth_status, "code": "authentication_required"}
        if "--mode" not in command:
            return {"sdk": "1.28.1"}
        mode = command[command.index("--mode") + 1]
        return {
            "status": "ok",
            "tools": read_tools,
            "project_id": runner.PROJECT,
            "remaining_budget": 87.5,
            "protocol": "2026-07-28" if mode == "modern" else "2025-11-25",
        }

    monkeypatch.setattr(runner, "run_client", client)
    arguments = (
        "uv",
        tmp_path / "installed/bin/python",
        tmp_path / "example/bin/python",
        tmp_path / "legacy/bin/python",
        "node",
        tmp_path / "examples",
        tmp_path,
    )
    if read_tools != 30:
        with pytest.raises(AssertionError):
            runner.acceptance(*arguments)
        assert not cancellations  # Reject stale/extra discovery before later lanes.
    elif auth_status != "execution_error":
        with pytest.raises(AssertionError) as captured:
            runner.acceptance(*arguments)
        assert str(captured.value) == (
            "solwyn/python/modern auth: expected execution_error, HTTP request delta 1"
        )
    elif cancellation_requests != 1:
        with pytest.raises(AssertionError) as captured:
            runner.acceptance(*arguments)
        assert str(captured.value) == (
            "solwyn/python/modern cancellation: expected HTTP request delta 1, "
            f"observed {cancellation_requests}"
        )
    else:
        report = runner.acceptance(*arguments)
        assert len(cancellations) == 4
        assert report["http_requests"] == 22
        assert len([row for row in report["clients"] if row.get("cancel")]) == 4
    api.shutdown.assert_called_once()
    api.server_close.assert_called_once()
