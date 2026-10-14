"""Real isolated helper transport, output and lifecycle checks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import signal
import time

import anyio
import pytest

from solwyn_cli._mcp_doctor_ipc import PrivateRequest, isolated_environment
from solwyn_cli._mcp_doctor_process import run_helper

from solwyn_cli import __version__
from solwyn_cli.mcp.server import get_tool_catalog


def request_data():
    return {
        "command": str(
            Path(sysconfig.get_path("scripts"))
            / ("solwyn.exe" if os.name == "nt" else "solwyn")
        ),
        "project": "proj_abababababababababababab",
        "api_url": "http://127.0.0.1:1",
        "profile": "read",
        "expected_tools": [t.definition.name for t in get_tool_catalog(profile="read")],
        "expected_version": __version__,
        "api_key": None,
    }


def test_leaf_helper_discovers_with_parent_pipe_still_open():
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.defpath,
            "OTEL_SDK_DISABLED": "true",
            "OTEL_PROPAGATORS": "none",
        },
    )
    try:
        proc.stdin.write(json.dumps(request_data()).encode() + b"\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
        assert proc.returncode == 0, proc.stderr.read().decode()
        result = json.loads(proc.stdout.read())
        assert result["status"] == "ok"
        assert result["identity"] == "match"
        assert result["protocol"] == "2026-07-28"
        assert result["read"] == "skipped"
        assert len(result["tools"]) == 30
        assert proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_doctor_server.py"
KEY = "sk_proj_" + "a1" * 32
CANARY = "PRIVATE_DIAGNOSTIC_CANARY"


def synthetic(tmp_path, mode="normal"):
    if os.name == "nt":
        pytest.skip(
            "Synthetic shebang fixture is POSIX; real Windows subprocess coverage belongs to OS CI"
        )
    request = request_data()
    request["api_key"] = KEY
    script = tmp_path / "synthetic solwyn"
    script.write_text(f"#!{sys.executable}\n" + FIXTURE.read_text())
    script.chmod(0o755)
    (tmp_path / "fixture.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "tools": request["expected_tools"],
                "project": request["project"],
                "version": request["expected_version"],
            }
        )
    )
    request["command"] = str(script)
    return request


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_files(root, names, timeout=5):
    deadline = time.monotonic() + timeout
    while not all((root / name).exists() for name in names):
        assert time.monotonic() < deadline, "synthetic processes did not start"
        time.sleep(0.02)
    return {name: int((root / name).read_text()) for name in names}


def assert_gone(pids, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(alive(pid) for pid in pids):
        time.sleep(0.02)
    assert not [pid for pid in pids if alive(pid)]


def cleanup_owned(root):
    for path in root.glob("*.pid"):
        pid = int(path.read_text())
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "mode,status,read",
    [
        ("normal", "ok", "ok"),
        ("bad_read", "ok", "failed"),
        ("wrong_identity", "failed", "skipped"),
        ("wrong_inventory", "failed", "skipped"),
        ("malformed", "failed", "skipped"),
    ],
)
def test_real_helper_safe_read_and_errors(tmp_path, mode, status, read):
    request = synthetic(tmp_path, mode)
    try:
        result = anyio.run(run_helper, PrivateRequest.model_validate(request))
        assert result.status == status
        assert result.read == read
        assert (
            KEY not in result.model_dump_json()
            and CANARY not in result.model_dump_json()
        )
        assert_gone(wait_files(tmp_path, ["server.pid"]).values())
        observations = (tmp_path / "observations.jsonl").read_text()
        if mode in {"wrong_inventory", "wrong_identity", "malformed"}:
            assert '"call"' not in observations
    finally:
        cleanup_owned(tmp_path)


@pytest.mark.parametrize("mode", ["stall_read", "eof_exit", "stubborn"])
def test_internal_deadline_unwinds_owned_server_and_active_descendant(tmp_path, mode):
    request = synthetic(tmp_path, mode)
    try:
        started = time.monotonic()
        result = anyio.run(run_helper, PrivateRequest.model_validate(request))
        assert result.status in {
            "timeout",
            "failed",
        }  # SDK's own read deadline may win.
        assert time.monotonic() - started < 20
        names = ["server.pid"] + (["descendant.pid"] if mode == "stubborn" else [])
        assert_gone(wait_files(tmp_path, names).values())
    finally:
        cleanup_owned(tmp_path)


def test_parent_pipe_eof_cancels_stubborn_server_and_descendant(tmp_path):
    request = synthetic(tmp_path, "stubborn")
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=isolated_environment(),
    )
    try:
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        proc.stdin.flush()
        pids = wait_files(tmp_path, ["server.pid", "descendant.pid"])
        started = time.monotonic()
        proc.stdin.close()
        proc.wait(timeout=10)
        assert time.monotonic() - started < 10
        assert json.loads(proc.stdout.read())["status"] == "parent_closed"
        assert proc.stderr.read() == b""
        assert_gone([proc.pid, *pids.values()])
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
        cleanup_owned(tmp_path)


def test_real_parent_death_closes_sole_writer_and_cleans_children(tmp_path):
    request = synthetic(tmp_path, "stubborn")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text("""import json, os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
from solwyn_cli._mcp_doctor_ipc import isolated_environment
with (root / "out").open("wb") as out, (root / "err").open("wb") as err:
    child = subprocess.Popen([sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"], stdin=subprocess.PIPE, stdout=out, stderr=err, env=isolated_environment())
    (root / "helper.pid").write_text(str(child.pid))
    child.stdin.write((root / "request.json").read_bytes())
    child.stdin.flush()
    while True:
        time.sleep(1)
""")
    (tmp_path / "request.json").write_text(json.dumps(request) + "\n")
    parent = subprocess.Popen(
        [sys.executable, "-I", str(parent_script), str(tmp_path)],
        env=isolated_environment(),
    )
    try:
        pids = wait_files(tmp_path, ["helper.pid", "server.pid", "descendant.pid"])
        parent.kill()
        parent.wait(timeout=3)
        assert_gone(pids.values(), timeout=10)
        assert json.loads((tmp_path / "out").read_text())["status"] == "parent_closed"
        assert (tmp_path / "err").read_bytes() == b""
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
        cleanup_owned(tmp_path)


@pytest.mark.parametrize(
    "payload", [b"{}\n", b"PRIVATE_BAD_INPUT\n", b"x" * 8193, b"{}\nPRIVATE_EXTRA\n"]
)
def test_strict_private_input_is_capped_and_never_reflected(payload):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"],
        input=payload,
        capture_output=True,
        env=isolated_environment(),
        timeout=5,
    )
    assert json.loads(result.stdout)["status"] == "invalid_input"
    assert b"PRIVATE" not in result.stdout and result.stderr == b""


def test_isolated_sdk_does_not_inherit_active_parent_exporter_or_propagation(tmp_path):
    request = synthetic(tmp_path)
    (tmp_path / "request.json").write_text(json.dumps(request))
    environment = isolated_environment()
    environment.update(
        {
            "OTEL_SDK_DISABLED": "false",
            "OTEL_PROPAGATORS": "tracecontext,baggage",
            "OPENAI_API_KEY": CANARY,
            "SOLWYN_TAGS": CANARY,
            "PYTHONPATH": CANARY,
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1/" + CANARY,
            "TRACEPARENT": CANARY,
            "BAGGAGE": CANARY,
        }
    )
    script = FIXTURE.with_name("mcp_doctor_telemetry.py")
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(script), str(tmp_path / "request.json")],
            capture_output=True,
            env=environment,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode()
        evidence = json.loads(result.stdout)
        assert evidence["positive_sdk_spans"] >= 3
        assert evidence["helper_sdk_spans"] == 0
        assert (
            evidence["parent_provider_unchanged"]
            and evidence["parent_propagator_unchanged"]
        )
        assert result.stderr == b"" and CANARY.encode() not in result.stdout
        records = [
            json.loads(line)
            for line in (tmp_path / "observations.jsonl").read_text().splitlines()
        ]
        calls = [row["call"] for row in records if "call" in row]
        assert calls[0]["propagation"] == {
            "traceparent": True,
            "tracestate": False,
            "baggage": True,
        }
        assert calls[1]["propagation"] == {
            "traceparent": False,
            "tracestate": False,
            "baggage": False,
        }
        envs = [row["environment"] for row in records if "environment" in row]
        assert envs[-1] == {"intended_key": True, "empty_key": False, "ambient": []}
        assert_gone(wait_files(tmp_path, ["server.pid"]).values())
    finally:
        cleanup_owned(tmp_path)


def test_parent_cancellation_lets_sdk_close_owned_process(tmp_path):
    request = synthetic(tmp_path, "stubborn")

    async def exercise():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_helper, PrivateRequest.model_validate(request))
            with anyio.fail_after(5):
                while not (tmp_path / "descendant.pid").exists():
                    await anyio.sleep(0.02)
            tasks.cancel_scope.cancel()

    try:
        anyio.run(exercise)
        assert_gone(wait_files(tmp_path, ["server.pid", "descendant.pid"]).values())
    finally:
        cleanup_owned(tmp_path)


def test_helper_output_cap_and_stderr_sink_do_not_reflect_private_text(
    tmp_path, monkeypatch
):
    real_open = anyio.open_process
    script = tmp_path / "bad_helper.py"
    script.write_text(
        'import sys\nsys.stdout.buffer.write(b"PRIVATE" * 200000)\nsys.stdout.flush()\nsys.stderr.buffer.write(b"PRIVATE" * 200000)\n'
    )

    async def launch(command, **kwargs):
        assert command == [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        return await real_open([sys.executable, "-I", str(script)], **kwargs)

    monkeypatch.setattr(anyio, "open_process", launch)
    started = time.monotonic()
    result = anyio.run(run_helper, PrivateRequest.model_validate(request_data()))
    assert result.status == "failed"
    assert time.monotonic() - started < 5
    assert "PRIVATE" not in result.model_dump_json()


def test_actual_cli_config_and_doctor_with_synthetic_http_outside_checkout(tmp_path):
    script = FIXTURE.with_name("mcp_doctor_wheel_smoke.py")
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        capture_output=True,
        env=isolated_environment(),
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["captured_key_http_and_mcp_read"] == "ok"
    assert json.loads(result.stdout)["canonical_identity_privacy"] == "ok"
    assert json.loads(result.stdout)["http_initialization_privacy"] == "ok"
    assert result.stderr == b""


def test_forced_helper_shutdown_reports_cleanup_failure(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("SIGTERM refusal is a POSIX failure fixture")
    real_open = anyio.open_process
    script = tmp_path / "stuck_helper.py"
    script.write_text("""import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stdin.buffer.readline()
sys.stdout.buffer.write(b"x" * 20000)
sys.stdout.flush()
sys.stdin.buffer.read()
open(sys.argv[1], "w").write("EOF observed")
while True:
    time.sleep(1)
""")
    pids = []

    async def launch(command, **kwargs):
        process = await real_open(
            [sys.executable, "-I", str(script), str(tmp_path / "eof")], **kwargs
        )
        pids.append(process.pid)
        return process

    monkeypatch.setattr(anyio, "open_process", launch)
    result = anyio.run(run_helper, PrivateRequest.model_validate(request_data()))
    assert result.status == "cleanup_failed"
    assert (tmp_path / "eof").read_text() == "EOF observed"
    assert_gone(pids)


def test_helper_sdk_parser_logs_never_expose_remote_text(tmp_path):
    request = synthetic(tmp_path, "malformed")
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "solwyn_cli._mcp_doctor_helper"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=isolated_environment(),
    )
    try:
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        proc.stdin.flush()
        proc.wait(timeout=15)
        assert json.loads(proc.stdout.read())["status"] == "failed"
        assert proc.stderr.read() == b""
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
        cleanup_owned(tmp_path)
