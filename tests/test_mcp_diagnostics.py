"""Offline launch and captured-credential diagnostic regressions."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click import unstyle
from golden.harness import assert_matches_golden
from key_fixtures import keys_entry
from login_fixtures import LOGIN_TOKEN, DictStore, install_store, login_entries
from typer.testing import CliRunner

from solwyn_cli import _mcp_launch, auth, config, runtime
from solwyn_cli._mcp_doctor_ipc import HelperResult
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import mcp_config, mcp_doctor
from solwyn_cli.main import app

KEY = "solwyn_sk_" + "a1" * 32
runner = CliRunner()


def _store(values, *, fail_on=()):
    """A credential store whose fail_on entries must never be read."""
    return DictStore(values, fail_on=fail_on)


GOLDEN_DIR = Path(__file__).parent / "golden"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("SOLWYN_API_KEY", raising=False)
    monkeypatch.delenv("SOLWYN_PROJECT_ID", raising=False)
    monkeypatch.delenv("SOLWYN_API_URL", raising=False)
    install_store(monkeypatch, DictStore(read_only=True))


FORWARDED = [
    "SOLWYN_TOKEN",
    "SOLWYN_API_KEY",
    "SOLWYN_AUTH",
    "SOLWYN_API_URL",
    "SOLWYN_CREDENTIAL_STORE",
    "SOLWYN_CREDENTIALS_FILE",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
]
DBUS = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]


def _pin_platform(monkeypatch, platform):
    from solwyn_cli import credential_store

    real = credential_store.platform_name
    monkeypatch.setattr(
        credential_store, "platform_name", lambda current=None: real(platform)
    )


def test_config_is_offline_and_keeps_global_options_before_mcp(monkeypatch, tmp_path):
    _pin_platform(monkeypatch, "linux")
    target = tmp_path / "path with spaces" / "solwyn"
    target.parent.mkdir()
    target.write_text("must never execute")
    target.chmod(0o755)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    install_store(monkeypatch, DictStore(fail_on="*"))
    monkeypatch.setattr(
        httpx.Client, "request", lambda *a, **kw: pytest.fail("network")
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_abababababababababababab",
            "--api-url",
            "https://api.example",
            "mcp-config",
            "--executable",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data == {
        "schema": "solwyn.mcp-config.v1",
        "command": str(target),
        "args": [
            "--project",
            "proj_abababababababababababab",
            "--api-url",
            "https://api.example",
            "mcp",
            "--profile",
            "read",
        ],
        "env_vars": FORWARDED,
    }
    assert result.stderr == ""
    assert KEY not in result.output


@pytest.mark.parametrize(
    ("platform", "store", "expected"),
    [
        # Names only, never values; D-Bus only when the Linux keyring is used.
        ("linux", "auto", FORWARDED),
        ("linux", "file", FORWARDED),
        ("linux", "keyring", FORWARDED + DBUS),
        ("darwin", "auto", FORWARDED),
        ("darwin", "keyring", FORWARDED),
        ("win32", "auto", FORWARDED),
        ("freebsd14", "keyring", FORWARDED + DBUS),
    ],
)
def test_config_forwards_variable_names_and_dbus_only_for_the_linux_keyring(
    monkeypatch, platform, store, expected
):
    _pin_platform(monkeypatch, platform)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", store)
    monkeypatch.setattr(
        mcp_config, "resolve_executable", lambda explicit: "/installed/solwyn"
    )

    result = runner.invoke(app, ["--json", "mcp-config", "--profile", "guardrails"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["env_vars"] == expected


def test_config_table_shows_env_vars(monkeypatch):
    _pin_platform(monkeypatch, "linux")
    monkeypatch.setattr(
        mcp_config, "resolve_executable", lambda explicit: "/installed/solwyn"
    )

    result = runner.invoke(app, ["--no-color", "mcp-config"])

    assert result.exit_code == 0, result.output
    assert '"env_vars":["SOLWYN_TOKEN","SOLWYN_API_KEY"' in "".join(
        result.stdout.split()
    )


def test_missing_explicit_executable_has_safe_error(tmp_path):
    result = runner.invoke(
        app, ["--json", "mcp-config", "--executable", str(tmp_path / "PRIVATE-missing")]
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "mcp_executable_unavailable"
    assert "PRIVATE-missing" not in result.output


def test_doctor_mcp_has_dedicated_schema():
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["schema"] == "solwyn.doctor.mcp.v1"


@pytest.mark.parametrize(
    "project,environ,want",
    [
        ("cli", {"SOLWYN_PROJECT_ID": "env"}, "cli"),
        (None, {"SOLWYN_PROJECT": "env"}, "environment"),
        # Read until 14d-2 removes it, after SOLWYN_PROJECT and .solwyn.env.
        (None, {"SOLWYN_PROJECT_ID": "env"}, "legacy_environment"),
        (None, {}, "none"),
    ],
)
def test_project_provenance(project, environ, want):
    ctx = runtime.resolve_command_context(
        api_url=None,
        project=project,
        output_format=None,
        json_flag=True,
        no_color=True,
        environ=environ,
    )
    assert getattr(ctx, "project_source", None) == want


@pytest.fixture
def probes(monkeypatch):
    requests = []
    launches = []
    responses = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path in {
            "/api/v1/auth/key",
            "/api/v1/projects/proj_abababababababababababab/budget",
            "/api/v1/projects/proj_cdcdcdcdcdcdcdcdcdcdcdcd/budget",
            "/api/v1/account/me",
        }
        return responses.pop(0)

    monkeypatch.setattr(
        mcp_doctor,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def run(request):
        launches.append(request)
        return HelperResult(
            status="ok",
            identity="match",
            version="match",
            inventory="match",
            protocol="2026-07-28",
            tools=request.expected_tools,
            read="ok" if request.api_key and request.project else "skipped",
        )

    monkeypatch.setattr(mcp_doctor, "run_helper", run)
    return requests, responses, launches


@pytest.mark.parametrize("scope", ["full", "read_only"])
def test_actual_key_identity_is_separate_from_wrong_selected_project(
    monkeypatch, probes, scope
):
    requests, responses, launches = probes
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    install_store(monkeypatch, _store({}, fail_on={auth.API_KEYS_ENTRY}))
    responses.extend(
        [
            httpx.Response(
                200,
                json={"project_id": "proj_abababababababababababab", "scope": scope},
            ),
            httpx.Response(403, json={"detail": "PRIVATE_REMOTE_CANARY"}),
        ]
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_cdcdcdcdcdcdcdcdcdcdcdcd",
            "doctor",
            "--mcp",
            "--profile",
            "guardrails",
        ],
    )
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout)
    assert data["key_context"]["project_id"] == "proj_abababababababababababab"
    assert data["key_context"]["scope"] == scope
    assert data["selected_project"] == "proj_cdcdcdcdcdcdcdcdcdcdcdcd"
    assert data["project_source"] == "cli"
    assert data["http_read"]["status"] == "failed"
    assert data["credential"] == {
        "status": "available",
        "kind": "project_key",
        "source": "environment",
    }
    assert len(data["tools"]) == 37
    assert "full" not in data["authority_note"] or "does not" in data["authority_note"]
    assert [r.headers["authorization"] for r in requests] == ["Bearer " + KEY] * 2
    assert len(launches) == 1 and launches[0].api_key.get_secret_value() == KEY
    assert KEY not in result.output and "PRIVATE_REMOTE_CANARY" not in result.output
    tools = {t["name"]: t for t in data["tools"]}
    assert tools["list_projects"]["credential_requirement"] == "login"
    assert tools["list_alert_events"]["credential_requirement"] == "either"
    assert "account" in tools["list_alert_events"]["detail"].lower()
    assert "Solwyn login" in tools["list_alert_events"]["detail"]


@pytest.mark.parametrize(
    "value,status", [("", "empty"), ("PRIVATE_BAD_KEY", "invalid")]
)
def test_invalid_environment_presence_never_falls_back(
    monkeypatch, probes, value, status
):
    requests, _, launches = probes
    monkeypatch.setenv("SOLWYN_API_KEY", value)
    install_store(monkeypatch, _store({}, fail_on={auth.API_KEYS_ENTRY}))
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["credential"] == {
        "status": status,
        "kind": "project_key",
        "source": "environment",
    }
    assert data["http_read"]["status"] == "skipped"
    assert not requests and launches[0].api_key is None
    assert data["mcp_read"]["status"] == "skipped"
    assert "PRIVATE_BAD_KEY" not in result.output


@pytest.mark.parametrize(
    "available,token,meta,want",
    [
        (False, None, None, "unavailable"),
        (True, None, None, "missing"),
        (True, "PRIVATE_TOKEN", None, "incomplete"),
        (True, "", "PRIVATE_META", "incomplete"),
    ],
)
def test_credential_store_availability_is_safe(
    monkeypatch, probes, available, token, meta, want
):
    # A login is one document; these are its broken forms.
    values = {}
    if token is not None:
        values[auth.LOGIN_ENTRY] = json.dumps({"token": token}) if token else meta
    install_store(monkeypatch, DictStore(values, unavailable=not available))
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["credential"]["status"] == want
    assert not probes[0]
    assert "PRIVATE_" not in result.output


@pytest.mark.parametrize("status,want", [(200, "ok"), (401, "failed")])
def test_login_is_captured_once_and_mcp_read_is_skipped(
    monkeypatch, probes, status, want
):
    requests, responses, launches = probes
    entries = []
    stored_login = login_entries(LOGIN_TOKEN)

    install_store(monkeypatch, DictStore(stored_login, read_log=entries))
    responses.append(
        httpx.Response(
            status,
            json={"user_id": "user_alpha"}
            if status == 200
            else {"detail": "PRIVATE_REMOTE"},
        )
    )
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert entries == [auth.LOGIN_ENTRY]
    assert data["credential"] == {
        "status": "available",
        "kind": "login",
        "source": "file",
    }
    assert data["http_read"]["status"] == want
    assert data["key_context"]["scope"] is None
    assert data["mcp_read"]["status"] == "skipped"
    assert "login" in data["mcp_read"]["detail"]
    assert launches[0].api_key is None
    assert (
        len(requests) == 1
        and requests[0].headers["authorization"] == "Bearer " + LOGIN_TOKEN
    )
    assert "PRIVATE_" not in result.output and LOGIN_TOKEN not in result.output


@pytest.mark.parametrize(
    "identity,want",
    [
        (httpx.Response(404, json={"detail": "PRIVATE_REMOTE"}), "unsupported"),
        (
            httpx.Response(
                200,
                json={
                    "project_id": "proj_abababababababababababab",
                    "scope": "PRIVATE_SCOPE",
                },
            ),
            "failed",
        ),
        (httpx.Response(200, json={"project_id": KEY, "scope": "full"}), "failed"),
        *[
            (
                httpx.Response(200, json={"project_id": value, "scope": "full"}),
                "failed",
            )
            for value in (
                "sk-PRIVATE_PROVIDER_CANARY",
                "PRIVATE_REMOTE_CANARY",
                "proj_" + "AB" * 12,
                "proj_" + "ab" * 11,
                "proj_" + "ab" * 13,
                "proj_" + "ab" * 12 + "\n",
            )
        ],
    ],
)
def test_old_or_malformed_identity_never_infers_scope_from_successful_read(
    monkeypatch, probes, identity, want
):
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    probes[1].extend(
        [
            identity,
            httpx.Response(200, json={"project_id": "proj_abababababababababababab"}),
        ]
    )
    result = runner.invoke(
        app, ["--json", "--project", "proj_abababababababababababab", "doctor", "--mcp"]
    )
    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["key_context"]["status"] == want
    assert data["key_context"]["scope"] is None
    assert data["http_read"]["status"] == "ok"
    assert KEY not in result.output and "PRIVATE_" not in result.output


@pytest.mark.parametrize("stage", ["construct", "enter", "exit"])
@pytest.mark.parametrize("kind", ["project_key", "login"])
def test_http_client_lifecycle_failure_is_safe_and_preserves_known_identity(
    monkeypatch, probes, caplog, stage, kind
):
    requests, _, launches = probes
    if kind == "project_key":
        monkeypatch.setenv("SOLWYN_API_KEY", KEY)
        install_store(monkeypatch, _store({}, fail_on={auth.API_KEYS_ENTRY}))
    else:
        install_store(monkeypatch, _store(login_entries(LOGIN_TOKEN)))

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + (
            KEY if kind == "project_key" else LOGIN_TOKEN
        )
        return httpx.Response(
            200,
            json={"project_id": "proj_" + "ab" * 12, "scope": "full"},
        )

    class LifecycleClient(SolwynClient):
        def __enter__(self):
            if stage == "enter":
                self.close()
                raise RuntimeError("PRIVATE_ENTER_CANARY")
            return super().__enter__()

        def __exit__(self, *args):
            super().__exit__(*args)
            if stage == "exit":
                raise RuntimeError("PRIVATE_EXIT_CANARY")

    def client(**kwargs):
        if stage == "construct":
            raise RuntimeError("PRIVATE_CONSTRUCT_CANARY")
        return LifecycleClient(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(mcp_doctor, "SolwynClient", client)
    result = runner.invoke(
        app, ["--json", "--project", "proj_" + "ab" * 12, "doctor", "--mcp"]
    )
    data = json.loads(result.stdout)
    assert result.exit_code == 1
    assert data["schema"] == "solwyn.doctor.mcp.v1"
    assert data["status"] == "degraded"
    assert data["credential"]["status"] == "available"
    assert data["http_read"]["status"] == "failed"
    if kind == "login":
        assert data["key_context"]["status"] == "skipped"
        assert data["key_context"]["project_id"] is None
    elif stage == "exit":
        assert data["key_context"]["status"] == "ok"
        assert data["key_context"]["project_id"] == "proj_" + "ab" * 12
        assert data["key_context"]["scope"] == "full"
    else:
        assert data["key_context"]["status"] == "failed"
        assert data["key_context"]["project_id"] is None
        assert data["key_context"]["scope"] is None
    assert data["mcp"]["status"] == "ok"
    assert data["executable"]["status"] == "ok"
    assert len(launches) == 1
    if stage != "exit":
        assert not requests
    for output in (result.stdout, result.stderr, caplog.text):
        assert "PRIVATE_" not in output and KEY not in output
        assert LOGIN_TOKEN not in output
    assert result.stderr == ""


def test_key_without_selected_project_still_reports_identity(monkeypatch, probes):
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    probes[1].append(
        httpx.Response(
            200,
            json={"project_id": "proj_abababababababababababab", "scope": "read_only"},
        )
    )
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert data["key_context"]["scope"] == "read_only"
    assert data["http_read"]["status"] == "skipped"
    assert data["mcp_read"]["status"] == "skipped"
    assert len(probes[0]) == 1


def test_config_error_never_sends_key_to_fallback_api(monkeypatch, tmp_path, probes):
    (tmp_path / "config.toml").write_text('api_url="PRIVATE-broken')
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert data["config"]["status"] == "failed"
    assert not probes[0] and probes[2][0].api_key is None
    assert "PRIVATE-" not in result.output


def test_login_discovery_only_is_overall_degraded(monkeypatch, probes):
    """A skipped authenticated MCP read must never advertise all diagnostics passed."""
    install_store(monkeypatch, _store(login_entries(LOGIN_TOKEN)))
    probes[1].append(httpx.Response(200, json={"user_id": "user_alpha"}))
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "degraded"


def test_windows_console_layout_and_no_path_fallback(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    executable = scripts / "solwyn.exe"
    executable.write_bytes(b"synthetic")
    executable.chmod(0o755)
    monkeypatch.setattr(_mcp_launch.sysconfig, "get_path", lambda name: str(scripts))
    monkeypatch.setattr(
        _mcp_launch, "os", SimpleNamespace(name="nt", access=os.access, X_OK=os.X_OK)
    )
    assert _mcp_launch.resolve_executable(None) == str(executable)
    with pytest.raises(ValueError, match="missing or not executable"):
        _mcp_launch.resolve_executable(scripts / "absent.exe")


def test_successful_key_read_and_configured_project_source(
    monkeypatch, tmp_path, probes
):
    (tmp_path / "config.toml").write_text(
        'default_project = "proj_abababababababababababab"\n'
    )
    monkeypatch.setenv("SOLWYN_PROJECT_ID", "")
    install_store(
        monkeypatch,
        DictStore(
            {auth.API_KEYS_ENTRY: keys_entry("proj_abababababababababababab", key=KEY)}
        ),
    )
    probes[1].extend(
        [
            httpx.Response(
                200,
                json={
                    "project_id": "proj_abababababababababababab",
                    "scope": "read_only",
                },
            ),
            httpx.Response(200, json={"project_id": "proj_abababababababababababab"}),
        ]
    )
    result = runner.invoke(app, ["--json", "doctor", "--mcp"])
    data = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert data["status"] == "ok"
    assert data["project_source"] == "config"
    assert data["credential"]["source"] == "file"
    assert data["mcp_read"]["status"] == "ok"


def test_mcp_config_json_golden(monkeypatch):
    _pin_platform(monkeypatch, "linux")
    monkeypatch.setattr(
        mcp_config, "resolve_executable", lambda explicit: "/installed/solwyn"
    )
    result = runner.invoke(
        app, ["--json", "--project", "proj_abababababababababababab", "mcp-config"]
    )
    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "mcp-config.json")


def test_mcp_doctor_json_golden(monkeypatch, probes):
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    probes[1].extend(
        [
            httpx.Response(
                200,
                json={
                    "project_id": "proj_abababababababababababab",
                    "scope": "read_only",
                },
            ),
            httpx.Response(200, json={"project_id": "proj_abababababababababababab"}),
        ]
    )
    result = runner.invoke(
        app, ["--json", "--project", "proj_abababababababababababab", "doctor", "--mcp"]
    )
    assert result.exit_code == 0
    assert_matches_golden(result.stdout, GOLDEN_DIR / "doctor-mcp-key.json")


def test_plain_doctor_rejects_explicit_mcp_profile_before_credentials(monkeypatch):
    install_store(monkeypatch, DictStore(fail_on="*"))
    result = runner.invoke(app, ["doctor", "--profile", "read"])
    assert result.exit_code == 2
    assert "require --mcp" in unstyle(result.output)
