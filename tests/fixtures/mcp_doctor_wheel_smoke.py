"""Run with the wheel's Python from outside the checkout; only synthetic HTTP."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
from threading import Thread

from solwyn_cli import __version__
from solwyn_cli._mcp_doctor_ipc import isolated_environment

KEY = "sk_proj_" + "a1" * 32
PROJECT = "proj_" + "ab" * 12
IDENTITY = PROJECT
SCOPE = "read_only"
BUDGET = {
    "project_id": "proj_abababababababababababab",
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
REQUESTS = []
FAIL = False


class API(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        REQUESTS.append(
            (
                self.command,
                self.path,
                self.headers.get("Authorization") == "Bearer " + KEY,
            )
        )
        if self.headers.get("Authorization") != "Bearer " + KEY:
            status, body = 401, {"detail": "PRIVATE_UNEXPECTED_CREDENTIAL"}
        elif self.path == "/api/v1/auth/key":
            status, body = 200, {"project_id": IDENTITY, "scope": SCOPE}
        elif self.path == "/api/v1/projects/proj_abababababababababababab/budget":
            status, body = (
                (503, {"detail": "PRIVATE_REMOTE_CANARY"}) if FAIL else (200, BUDGET)
            )
        else:
            status, body = 500, {"detail": "PRIVATE_UNEXPECTED_REQUEST"}
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        raise AssertionError("Diagnostics must never mutate")


with tempfile.TemporaryDirectory(prefix="solwyn-wheel-doctor-") as directory:
    root = Path(directory)
    environment = isolated_environment()
    environment.update(
        SOLWYN_API_KEY=KEY,
        HOME=directory,
        USERPROFILE=directory,
        APPDATA=directory,
        LOCALAPPDATA=directory,
        XDG_CONFIG_HOME=directory,
    )
    executable = str(
        Path(sysconfig.get_path("scripts"))
        / ("solwyn.exe" if os.name == "nt" else "solwyn")
    )
    offline = subprocess.run(
        [
            executable,
            "--json",
            "--project",
            "proj_abababababababababababab",
            "mcp-config",
        ],
        capture_output=True,
        env=environment,
        cwd=root,
        timeout=10,
    )
    assert offline.returncode == 0, offline.stderr.decode()
    config = json.loads(offline.stdout)
    assert config["command"] == executable
    assert config["args"][-3:] == ["mcp", "--profile", "read"]
    assert offline.stderr == b"" and KEY.encode() not in offline.stdout
    api = ThreadingHTTPServer(("127.0.0.1", 0), API)
    thread = Thread(target=api.serve_forever)
    thread.start()
    try:
        command = [
            executable,
            "--json",
            "--project",
            "proj_abababababababababababab",
            "--api-url",
            f"http://127.0.0.1:{api.server_port}",
            "doctor",
            "--mcp",
        ]
        good = subprocess.run(
            command, capture_output=True, env=environment, cwd=root, timeout=25
        )
        assert good.returncode == 0, (good.stdout + good.stderr).decode()
        data = json.loads(good.stdout)
        assert data["status"] == "ok"
        assert data["key_context"]["scope"] == "read_only"
        assert data["mcp_read"]["status"] == "ok"
        assert data["mcp"]["protocol"] == "2026-07-28"
        assert data["mcp"]["inventory"] == "match"
        assert REQUESTS == [
            ("GET", "/api/v1/auth/key", True),
            ("GET", "/api/v1/projects/proj_abababababababababababab/budget", True),
            ("GET", "/api/v1/projects/proj_abababababababababababab/budget", True),
        ]
        FAIL = True
        bad = subprocess.run(
            command, capture_output=True, env=environment, cwd=root, timeout=25
        )
        assert bad.returncode == 1
        failure = json.loads(bad.stdout)
        assert failure["key_context"]["scope"] == "read_only"
        assert failure["http_read"]["status"] == "failed"
        assert failure["mcp_read"]["status"] == "failed"
        for output in (good.stdout, good.stderr, bad.stdout, bad.stderr):
            assert KEY.encode() not in output and b"PRIVATE_" not in output
        assert good.stderr == bad.stderr == b""
        assert len(REQUESTS) == 6 and all(row[2] for row in REQUESTS)
        FAIL = False
        for IDENTITY in (
            PROJECT,
            KEY,
            "sk-PRIVATE_PROVIDER_SECRET_CANARY",
            "PRIVATE_REMOTE_IDENTITY_CANARY",
        ):
            SCOPE = "full"
            before = len(REQUESTS)
            response = subprocess.run(
                command, capture_output=True, env=environment, cwd=root, timeout=25
            )
            payload = json.loads(response.stdout)
            assert payload["schema"] == "solwyn.doctor.mcp.v1"
            assert payload["http_read"]["status"] == "ok"
            assert payload["mcp_read"]["status"] == "ok"
            if IDENTITY == PROJECT:
                assert response.returncode == 0
                assert payload["key_context"]["project_id"] == PROJECT
                assert payload["key_context"]["scope"] == "full"
            else:
                assert response.returncode == 1
                assert payload["status"] == "degraded"
                assert payload["key_context"]["status"] == "failed"
                assert payload["key_context"]["project_id"] is None
                assert payload["key_context"]["scope"] is None
                assert IDENTITY.encode() not in response.stdout
            assert response.stderr == b""
            assert KEY.encode() not in response.stdout
            assert b"PRIVATE_" not in response.stdout
            assert REQUESTS[before:] == [
                ("GET", "/api/v1/auth/key", True),
                ("GET", f"/api/v1/projects/{PROJECT}/budget", True),
                ("GET", f"/api/v1/projects/{PROJECT}/budget", True),
            ]
        IDENTITY = PROJECT
        for setting, value in (
            ("HTTP_PROXY", "http://127.0.0.1:PRIVATE_PROXY_SECRET_CANARY"),
            ("SSL_CERT_FILE", str(root / "PRIVATE_MISSING_CERT_CANARY")),
        ):
            before = len(REQUESTS)
            response = subprocess.run(
                command,
                capture_output=True,
                env={**environment, setting: value},
                cwd=root,
                timeout=25,
            )
            payload = json.loads(response.stdout)
            assert response.returncode == 1
            assert payload["schema"] == "solwyn.doctor.mcp.v1"
            assert payload["status"] == "degraded"
            assert payload["credential"]["status"] == "available"
            assert payload["key_context"]["status"] == "failed"
            assert payload["key_context"]["project_id"] is None
            assert payload["key_context"]["scope"] is None
            assert payload["http_read"]["status"] == "failed"
            assert payload["mcp"]["status"] == "ok"
            assert payload["mcp"]["inventory"] == "match"
            assert payload["mcp_read"]["status"] == "ok"
            assert payload["executable"]["status"] == "ok"
            assert response.stderr == b""
            assert KEY.encode() not in response.stdout
            assert b"PRIVATE_" not in response.stdout
            assert REQUESTS[before:] == [
                ("GET", f"/api/v1/projects/{PROJECT}/budget", True)
            ]
        assert list(root.iterdir()) == []  # No credential/config files were created.
    finally:
        api.shutdown()
        thread.join(timeout=3)
        api.server_close()
    print(
        json.dumps(
            {
                "version": __version__,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "outside_checkout": True,
                "offline_config": "ok",
                "captured_key_http_and_mcp_read": "ok",
                "safe_failure": "ok",
                "canonical_identity_privacy": "ok",
                "http_initialization_privacy": "ok",
                "request_count": len(REQUESTS),
            }
        )
    )
