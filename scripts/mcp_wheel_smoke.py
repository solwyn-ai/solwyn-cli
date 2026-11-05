"""Install one wheel outside checkout and run the delivered MCP clients.

Run with uv run python scripts/mcp_wheel_smoke.py --python 3.11.
Builds from checkout unless --wheel supplies an existing release artifact.
Only synthetic loopback HTTP, credentials and processes are used. Node24 is required.
"""

import argparse
import ctypes
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "proj_0123456789abcdef01234567"
KEY = "solwyn_sk_" + "a1" * 32
BASELINE = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "SystemRoot",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "UV_CACHE_DIR",
)
BUDGET = {
    "project_id": PROJECT,
    "budget_limit": 100.0,
    "current_usage": 12.5,
    "remaining_budget": 87.5,
    "utilization_pct": 12.5,
    "budget_mode": "hard_deny",
    "budget_period": "monthly",
    "period_date": "2026-09",
    "outage_overspend_ceiling_usd": 0.0,
    "thresholds": [],
    "sub_period": None,
    "sub_limit": None,
    "constraints": {
        "allowed_modes": ["alert_only", "hard_deny"],
        "allowed_periods": ["daily", "weekly", "monthly"],
        "sub_period_enabled": True,
        "max_thresholds": 50,
    },
}


def baseline() -> dict[str, str]:
    return {name: os.environ[name] for name in BASELINE if name in os.environ}


def checked(
    command: list[str], *, cwd: Path, env: dict | None = None, timeout: float = 180
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env or baseline(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Commands contain paths/options only; arbitrary package/subprocess text
        # is not reflected into CI logs. Rerun a failed install locally to inspect.
        raise RuntimeError(f"Command failed ({result.returncode}): {command}")
    return result.stdout.strip()


def interpreter(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def executable(environment: Path, name: str) -> Path:
    return environment / (f"Scripts/{name}.exe" if os.name == "nt" else f"bin/{name}")


def python_command(uv: str, python: Path, *args: str) -> list[str]:
    return [
        uv,
        "run",
        "--no-project",
        "--python",
        str(python),
        "--",
        "python",
        "-I",
        *args,
    ]


class _ProcessEntry32W(ctypes.Structure):
    # Fixed Win32 widths keep this ABI checkable on non-Windows test hosts too.
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_uint16 * 260),  # Required buffer; never read.
    ]


@cache
def windows_process_api():
    """Load and type the shared system-library bindings once per runner."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for function in (kernel.Process32FirstW, kernel.Process32NextW):
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
        function.restype = ctypes.c_int32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int32
    return kernel


def windows_process_table() -> dict[int, int]:
    """Read PID/PPID from a Toolhelp32 snapshot without launching PowerShell/WMI."""
    kernel = windows_process_api()
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS.
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Process snapshot creation failed")
    failure: BaseException | None = None
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process inventory unavailable")
        rows = {}
        while True:
            rows[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES is the only successful end.
                    raise OSError(error, "Process inventory enumeration failed")
                return rows
    except BaseException as error:
        failure = error
        raise
    finally:
        if not kernel.CloseHandle(snapshot):
            error = ctypes.get_last_error()
            if failure is None:
                raise OSError(error, "Process snapshot close failed")
            failure.add_note(
                f"Process snapshot close also failed (Windows error {error})."
            )


def process_table() -> dict[int, int]:
    """Only PID/PPID inventory; never read other processes' argv or environment."""
    if os.name == "nt":
        return windows_process_table()
    text = checked(["ps", "-axo", "pid=,ppid="], cwd=Path.cwd(), timeout=5)
    return {
        int(pid): int(parent)
        for pid, parent in (line.split() for line in text.splitlines())
    }


def descendants(table: dict[int, int], parent: int) -> set[int]:
    found: set[int] = set()
    pending = {parent}
    while pending:
        pending = {pid for pid, ppid in table.items() if ppid in pending} - found
        found.update(pending)
    return found


def run_client(
    command: list[str], *, cwd: Path, env: dict, expected: int, observe: bool = False
) -> dict:
    """Bound the test itself and observe actual client/server exit after cancellation."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    children: set[int] = set()
    started = time.monotonic()
    failure: BaseException | None = None
    try:
        while process.poll() is None:
            if observe:
                children.update(descendants(process_table(), process.pid))
            if time.monotonic() - started > 25:
                raise AssertionError("Client exceeded outer watchdog")
            time.sleep(0.1)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == expected, (
            "Unexpected client exit",
            process.returncode,
            stdout,
        )
        assert not stderr, "Client wrote unexpected stderr"
        assert "PRIVATE_" not in stdout and KEY not in stdout
        result = json.loads(stdout)
        if observe:
            assert children, "No actual child process was observed"
            until = time.monotonic() + 5
            while children.intersection(process_table()) and time.monotonic() < until:
                time.sleep(0.1)
            assert not children.intersection(process_table()), (
                "Client left an observed child alive"
            )
            result["observed_children_exited"] = len(children)
        return result
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_errors = []
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except Exception as error:
            cleanup_errors.append(error)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except Exception as error:
                cleanup_errors.append(error)
        # Failure-path cleanup touches only the synthetic descendants observed
        # during this owned invocation. It must still run if inventory failed.
        for pid in children if failure is not None else ():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            if failure is None:
                raise cleanup_errors[0]
            failure.add_note(
                f"Client cleanup also failed for {len(cleanup_errors)} resource(s)."
            )


class SyntheticAPI(ThreadingHTTPServer):
    mode = "success"

    def __init__(self):
        super().__init__(("127.0.0.1", 0), APIHandler)
        self.requests: list[tuple[str, str]] = []
        self.release = Event()


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.command, self.path))
        if self.server.mode == "stall":
            self.server.release.wait(15)
        if (
            self.server.mode == "auth"
            or self.headers.get("Authorization") != "Bearer " + KEY
        ):
            status, data = 401, {"detail": "PRIVATE_API_CANARY"}
        elif self.path == f"/api/v1/projects/{PROJECT}/budget":
            status, data = 200, BUDGET
        else:
            status, data = 404, {"detail": "PRIVATE_UNEXPECTED_ROUTE"}
        body = json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        self.server.requests.append((self.command, self.path))
        self.send_error(500)  # Recorded below as a forbidden mutation.


def assert_request_count(api, before: int, expected: int, *, lane: str) -> None:
    observed = len(api.requests) - before
    assert observed == expected, (
        f"{lane}: expected HTTP request delta {expected}, observed {observed}"
    )


def acceptance(
    uv: str,
    python: Path,
    example_python: Path,
    legacy_python: Path,
    node: str,
    kit: Path,
    root: Path,
) -> dict:
    environment = baseline()
    environment.update(
        SOLWYN_API_KEY=KEY,
        HOME=str(root / "home"),
        USERPROFILE=str(root / "home"),
        APPDATA=str(root / "home"),
        LOCALAPPDATA=str(root / "home"),
    )
    (root / "home").mkdir()
    # Synthetic parent canaries must not become child settings or output.
    environment.update(
        OPENAI_API_KEY="PRIVATE_PROVIDER_CANARY",
        SOLWYN_TAGS="PRIVATE_TAGS_CANARY",
        OTEL_EXPORTER_OTLP_HEADERS="PRIVATE_OTEL_CANARY",
        PYTHONPATH="PRIVATE_PYTHON_CANARY",
    )
    api = SyntheticAPI()
    thread = Thread(target=api.serve_forever, daemon=True)
    thread.start()
    rows = []
    try:
        for alias in ("solwyn", "solwyn-cli"):
            server = executable(python.parent.parent, alias)
            common = [
                "--executable",
                str(server),
                "--project",
                PROJECT,
                "--api-url",
                f"http://127.0.0.1:{api.server_port}",
            ]
            clients = {
                "python": python_command(
                    uv, example_python, str(kit / "python/client.py")
                ),
                "typescript": [node, str(kit / "typescript/dist/client.js")],
            }
            for language, command in clients.items():
                for mode in ("modern", "legacy"):
                    launch = [*command, *common, "--mode", mode]
                    lane = f"{alias}/{language}/{mode}"
                    before = len(api.requests)
                    result = run_client(launch, cwd=root, env=environment, expected=0)
                    assert (
                        result["status"] == "ok"
                        and result["tools"] == 33
                        and result["project_id"] == PROJECT
                        and result["remaining_budget"] == 87.5
                    )
                    assert result["protocol"] == (
                        "2026-07-28" if mode == "modern" else "2025-11-25"
                    )
                    assert_request_count(api, before, 1, lane=f"{lane} read")
                    bad = run_client(
                        [*launch, "--validation-error"],
                        cwd=root,
                        env=environment,
                        expected=1,
                    )
                    assert bad["code"] == "invalid_arguments", (
                        f"{lane} validation: expected invalid_arguments",
                        bad,
                    )
                    assert_request_count(
                        api, before, 1, lane=f"{lane} after validation"
                    )
                    api.mode = "auth"
                    auth_before = len(api.requests)
                    auth = run_client(launch, cwd=root, env=environment, expected=1)
                    assert auth["status"] == "execution_error", (
                        f"{lane} auth: expected execution_error, "
                        f"HTTP request delta {len(api.requests) - auth_before}"
                    )
                    # No auth replay.
                    assert_request_count(api, before, 2, lane=f"{lane} after auth")
                    api.mode = "success"
                    rows.append(
                        {
                            "client": language,
                            "entrypoint": alias,
                            "mode": mode,
                            "tools": result["tools"],
                            "read": "ok",
                            "validation": "ok",
                            "auth": auth["code"],
                        }
                    )
                # A real in-flight HTTP read is cancelled by the client deadline.
                # Use the same default useful-work budget as the successful read:
                # a shorter startup deadline can expire before any HTTP request.
                api.mode = "stall"
                before = len(api.requests)
                cancelled = run_client(
                    [*command, *common],
                    cwd=root,
                    env=environment,
                    expected=1,
                    observe=True,
                )
                assert cancelled["status"] == "client_error"
                assert_request_count(
                    api, before, 1, lane=f"{alias}/{language}/modern cancellation"
                )
                api.release.set()
                api.mode = "success"
                api.release = Event()
                rows.append(
                    {
                        "client": language,
                        "entrypoint": alias,
                        "cancel": "ok",
                        "children_exited": cancelled["observed_children_exited"],
                    }
                )
            legacy = run_client(
                python_command(
                    uv,
                    legacy_python,
                    str(kit / "mcp_installed_legacy.py"),
                    str(server),
                    f"http://127.0.0.1:{api.server_port}",
                    PROJECT,
                ),
                cwd=root,
                env=environment,
                expected=0,
            )
            assert legacy["sdk"] == "1.28.1"
            rows.append({"entrypoint": alias, **legacy})
        assert all(
            method == "GET" and path == f"/api/v1/projects/{PROJECT}/budget"
            for method, path in api.requests
        )
    finally:
        api.release.set()
        api.shutdown()
        api.server_close()
        thread.join(timeout=3)
    doctor = checked(
        python_command(uv, python, str(kit / "mcp_doctor_wheel_smoke.py")),
        cwd=root,
        env=environment,
        timeout=120,
    )
    return {
        "clients": rows,
        "doctor": json.loads(doctor),
        "http_requests": len(api.requests),
    }


def prepare_wheel(uv: str, root: Path, wheel: Path | None) -> Path:
    if wheel is not None:
        return wheel.resolve(strict=True)
    checked(
        [uv, "build", "--wheel", "--out-dir", str(root / "dist"), str(ROOT)],
        cwd=root,
    )
    wheels = list((root / "dist").glob("*.whl"))
    assert len(wheels) == 1, "Expected exactly one built wheel"
    return wheels[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.11", help="Python floor to install/test")
    parser.add_argument("--report", type=Path, help="Optional JSON evidence output")
    parser.add_argument("--npm-cache", type=Path, help="Optional npm cache directory")
    parser.add_argument("--wheel", type=Path, help="Existing release wheel to test")
    args = parser.parse_args()
    uv, node, npm = shutil.which("uv"), shutil.which("node"), shutil.which("npm")
    assert uv and node and npm, "uv, Node24 and npm must be installed"
    node_version = checked([node, "--version"], cwd=ROOT)
    assert node_version.startswith("v24."), "This example is maintained on Node24"
    npm_cli = (
        Path(npm).resolve()
        if os.name != "nt"
        else Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
    )
    assert npm_cli.is_file(), "Cannot resolve npm's JavaScript entrypoint"
    with tempfile.TemporaryDirectory(prefix="solwyn wheel acceptance ") as directory:
        root = Path(directory).resolve()
        assert not root.is_relative_to(ROOT), "Wheel work must be outside checkout"
        wheel = prepare_wheel(uv, root, args.wheel)
        checked(
            [
                uv,
                "run",
                "--project",
                str(ROOT),
                "--no-sync",
                "python",
                str(ROOT / "scripts/inspect_wheel.py"),
                str(wheel),
            ],
            cwd=root,
        )
        requirements = root / "runtime.txt"
        checked(
            [
                uv,
                "export",
                "--project",
                str(ROOT),
                "--frozen",
                "--no-default-groups",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
        )
        assert "opentelemetry-sdk==" not in requirements.read_text()
        environment = root / "installed environment"
        checked([uv, "venv", "--python", args.python, str(environment)], cwd=root)
        python = interpreter(environment)
        checked(
            [
                uv,
                "pip",
                "sync",
                "--python",
                str(python),
                "--require-hashes",
                str(requirements),
            ],
            cwd=root,
        )
        checked(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(wheel),
            ],
            cwd=root,
        )
        checked([uv, "pip", "check", "--python", str(python)], cwd=root)
        metadata_code = "import importlib.metadata as m, importlib.util, json, pathlib, sys; p=pathlib.Path(importlib.util.find_spec('solwyn_cli').origin); assert p.is_relative_to(pathlib.Path(sys.prefix)); names={d.metadata['Name'].lower() for d in m.distributions()}; assert not any(n == 'opentelemetry-sdk' or n.startswith('opentelemetry-exporter') for n in names); print(json.dumps({'python':sys.version.split()[0], 'solwyn':m.version('solwyn-cli'), 'mcp':m.version('mcp'), 'module':str(p), 'environment':sys.prefix}))"
        installed = json.loads(
            checked(python_command(uv, python, "-c", metadata_code), cwd=root)
        )
        for alias in ("solwyn", "solwyn-cli"):
            assert "0.4.3" in checked(
                [str(executable(environment, alias)), "--version"], cwd=root
            )
        kit = root / "copied examples"
        shutil.copytree(
            ROOT / "examples/mcp",
            kit,
            ignore=shutil.ignore_patterns(
                ".venv", "node_modules", "dist", "__pycache__"
            ),
        )
        for fixture in ("mcp_installed_legacy.py", "mcp_doctor_wheel_smoke.py"):
            shutil.copy2(ROOT / "tests/fixtures" / fixture, kit / fixture)
        checked(
            [
                uv,
                "sync",
                "--locked",
                "--project",
                str(kit / "python"),
                "--python",
                str(python),
            ],
            cwd=root,
        )
        example_python = interpreter(kit / "python/.venv")
        checked(
            [uv, "venv", "--python", str(python), str(root / "legacy client")], cwd=root
        )
        legacy_python = interpreter(root / "legacy client")
        checked(
            [
                uv,
                "pip",
                "sync",
                "--python",
                str(legacy_python),
                "--require-hashes",
                str(ROOT / "scripts/locks/mcp-legacy.txt"),
            ],
            cwd=root,
        )
        npm_config = root / "empty.npmrc"
        npm_config.write_text("")
        global_config = root / "empty-global.npmrc"
        global_config.write_text("")
        npm_flags = [
            "--userconfig",
            str(npm_config),
            "--globalconfig",
            str(global_config),
        ]
        if args.npm_cache:
            npm_flags += ["--cache", str(args.npm_cache.resolve())]
        checked(
            [
                node,
                str(npm_cli),
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                *npm_flags,
            ],
            cwd=kit / "typescript",
        )
        checked(
            [node, str(npm_cli), "run", "build", *npm_flags], cwd=kit / "typescript"
        )
        checked([node, str(npm_cli), "test", *npm_flags], cwd=kit / "typescript")
        report = {
            "platform": sys.platform,
            "node": node_version,
            "installed": installed,
            **acceptance(uv, python, example_python, legacy_python, node, kit, root),
        }
        if args.report:
            args.report.resolve().write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
