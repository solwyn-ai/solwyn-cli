"""MCP reporting tools take their project once, at launch (spec section 7.7).

budget_check, budget_check_batch, budget_confirm and record_call never accept a
project per call: the server resolves the launch project (--project before mcp,
SOLWYN_PROJECT, the repo's .solwyn.env) and its key once, validates the pair
with GET /api/v1/auth/key once, and returns the same refusal on every call when
that fails. Read tools take ``project`` (a name or proj_ id) per call.
"""

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client

from key_fixtures import (
    ACCOUNT_ID,
    KEY,
    KEY_ID,
    OTHER_ACCOUNT_ID,
    OTHER_KEY,
    OTHER_KEY_ID,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    auth_key_body,
    store_key,
)
from login_fixtures import LOGIN_TOKEN, OTHER_LOGIN_TOKEN, login_meta
from solwyn_cli import auth, credential_store, project_resolver, repo_file, runtime
from solwyn_cli.client import SolwynClient as RealSolwynClient
from solwyn_cli.commands import mcp_doctor
from solwyn_cli.mcp import server as mcp_server
from test_mcp_conformance import BUDGET_RESPONSE, CHECK_RESPONSE

NAMES = {
    "checkout-bot": PROJECT_ID,
    PROJECT_ID: PROJECT_ID,
    "research": OTHER_PROJECT_ID,
    OTHER_PROJECT_ID: OTHER_PROJECT_ID,
}
CHECK_ARGS = {"estimated_input_tokens": 10, "model": "gpt-5", "provider": "openai"}
REPORTING_CALLS = [
    ("budget_check", CHECK_ARGS),
    (
        "budget_check_batch",
        {"checks": [CHECK_ARGS]},
    ),
    (
        "budget_confirm",
        {
            "reservation_id": "res_01JZEXAMPLE",
            "call_id": "12345678-1234-1234-1234-123456789abc",
            "model": "gpt-5",
            "provider": "openai",
        },
    ),
    ("record_call", {"model": "local", "provider": "ollama"}),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "SOLWYN_API_KEY",
        "SOLWYN_PROJECT",
        "SOLWYN_PROJECT_ID",
        "SOLWYN_TOKEN",
        "SOLWYN_AUTH",
        "SOLWYN_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)


class Core:
    """A fake Core over the real client: every request with its headers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.requests: list[httpx.Request] = []
        self.auth_key_account = ACCOUNT_ID
        self.auth_key_status: list[int] = []
        self.echo = True

        def factory(*, api_url: str, credential: Any, **_: Any) -> RealSolwynClient:
            return RealSolwynClient(
                api_url=api_url,
                credential=credential,
                transport=httpx.MockTransport(self.handle),
                sleeper=lambda _: None,
            )

        monkeypatch.setattr(mcp_server, "SolwynClient", factory)
        monkeypatch.setattr(mcp_doctor, "SolwynClient", factory)
        monkeypatch.setattr(project_resolver, "_client_factory", lambda: factory)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        selector = request.headers.get("Solwyn-Project")
        path = request.url.path
        if path == "/api/v1/auth/key":
            if self.auth_key_status:
                status = self.auth_key_status.pop(0)
                if status != 200:
                    return httpx.Response(
                        status,
                        json={
                            "detail": {
                                "code": "api_key_expired",
                                "message": "This API key expired on 2026-11-01. Rotate it in the dashboard or with solwyn keys rotate.",
                            }
                        }
                        if status == 401
                        else {"detail": "busy"},
                        headers={"Retry-After": "0"} if status == 503 else {},
                    )
            if selector not in NAMES:
                return httpx.Response(
                    404,
                    json={
                        "detail": {
                            "code": "project_not_found",
                            "message": "No project on this API key matches Solwyn-Project.",
                        }
                    },
                )
            project_id = NAMES[selector]
            return httpx.Response(
                200,
                json=auth_key_body(
                    project_id=project_id,
                    project_name="checkout-bot"
                    if project_id == PROJECT_ID
                    else "research",
                    account_id=self.auth_key_account,
                ),
            )
        headers = (
            {"Solwyn-Project-Id": NAMES.get(selector or "", "")}
            if self.echo and selector in NAMES
            else {}
        )
        if path == "/api/v1/budgets/check":
            body = json.loads(request.content)
            del body
            return httpx.Response(
                200,
                json={**CHECK_RESPONSE, "project_id": NAMES.get(selector or "", "")},
                headers=headers,
            )
        if path.endswith("/budget") and request.method == "GET":
            project_id = path.split("/")[4]
            return httpx.Response(
                200, json={**BUDGET_RESPONSE, "project_id": project_id}, headers=headers
            )
        return httpx.Response(599, json={"detail": "unexpected"})


async def _session_calls(
    launch: mcp_server.McpLaunchOptions, calls: list[tuple[str, dict[str, Any]]]
) -> list[Any]:
    async with Client(mcp_server.create_server(launch_options=launch), cache=None) as s:
        return [await s.call_tool(name, arguments) for name, arguments in calls]


def call(launch: mcp_server.McpLaunchOptions, *calls: tuple[str, dict]) -> list[dict]:
    results = anyio.run(_session_calls, launch, list(calls))
    return [result.structured_content for result in results]


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".solwyn.env").write_text(
        "# Solwyn project for this repo. Not a secret: commit this file.\n"
        f'SOLWYN_PROJECT="{value}"\n'
    )
    monkeypatch.chdir(root)


# --- The launch pair, validated once ---------------------------------------------


def test_a_proj_id_launch_validates_the_key_once_and_sends_the_id_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert first["ok"] and second["ok"]
    assert core.paths() == [
        "GET /api/v1/auth/key",
        "POST /api/v1/budgets/check",
        "POST /api/v1/budgets/check",
    ]
    for request in core.requests:
        assert request.headers["Solwyn-Project"] == PROJECT_ID
        assert request.headers["Authorization"] == f"Bearer {KEY}"
    # The project Core says the call was for, from Solwyn-Project-Id.
    assert first["project_id"] == second["project_id"] == PROJECT_ID


def test_a_name_launch_resolves_through_auth_key_once_then_sends_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")

    results = call(
        mcp_server.McpLaunchOptions(),
        ("budget_check", CHECK_ARGS),
        ("record_call", {"model": "local", "provider": "ollama"}),
    )

    assert results[0]["ok"]
    assert core.paths()[:2] == ["GET /api/v1/auth/key", "POST /api/v1/budgets/check"]
    assert core.paths().count("GET /api/v1/auth/key") == 1
    assert core.requests[0].headers["Solwyn-Project"] == "checkout-bot"
    assert all(r.headers["Solwyn-Project"] == PROJECT_ID for r in core.requests[1:])


def test_without_an_echo_the_output_has_no_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.echo = False
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    (result,) = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID), ("budget_check", CHECK_ARGS)
    )

    assert result["ok"] and "project_id" not in result


def test_a_busy_validation_is_retried_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.auth_key_status = [503, 503, 200]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert first["error"]["code"] == "service_busy"
    assert second["ok"]


# --- Refusals are kept and returned on every call ---------------------------------


@pytest.mark.parametrize("name,arguments", REPORTING_CALLS)
def test_a_repo_file_mismatch_refuses_every_reporting_call_but_reads_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, arguments: dict
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second, read = call(
        mcp_server.McpLaunchOptions(project_id="research"),
        (name, arguments),
        (name, arguments),
        ("budget_status", {}),
    )

    for refused in (first, second):
        assert refused["ok"] is False
        assert refused["error"]["code"] == "project_mismatch"
        assert "Never switch projects" in refused["error"]["hint"]
    # Only the read tool reached Core; nothing was reserved or recorded.
    assert read["ok"] is True
    assert read["data"]["project_id"] == OTHER_PROJECT_ID
    assert not any(r.method == "POST" for r in core.requests)


def test_the_stored_key_of_another_project_is_never_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )

    first, second = call(
        mcp_server.McpLaunchOptions(project_id="checkout-bot"),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert first["error"]["code"] == second["error"]["code"] == "api_key_not_stored"
    assert core.requests == []


def test_a_launch_without_a_project_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    (result,) = call(mcp_server.McpLaunchOptions(), ("budget_check", CHECK_ARGS))

    assert result["error"]["code"] == "project_required"
    assert core.requests == []


def test_a_leftover_solwyn_project_id_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT_ID", PROJECT_ID)

    (result,) = call(mcp_server.McpLaunchOptions(), ("budget_check", CHECK_ARGS))

    assert result["error"]["code"] == "usage_error"
    assert "SOLWYN_PROJECT" in result["error"]["message"]


def test_an_env_key_of_another_account_than_the_login_is_refused_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.auth_key_account = OTHER_ACCOUNT_ID
    auth.store_login(LOGIN_TOKEN, login_meta(account_id=ACCOUNT_ID))
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert (
        first["error"]["code"]
        == second["error"]["code"]
        == ("api_key_account_mismatch")
    )
    assert core.paths() == ["GET /api/v1/auth/key"]


def test_an_expired_key_refusal_is_kept_with_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.auth_key_status = [401]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    for refused in (first, second):
        assert refused["error"]["code"] == "api_key_expired"
        assert refused["error"]["message"] == "The API key expired."
        assert "keys rotate" in refused["error"]["hint"]
    assert core.paths() == ["GET /api/v1/auth/key"]


# --- No per-call project for reporting tools ---------------------------------------


@pytest.mark.parametrize("field", ["project", "project_id"])
@pytest.mark.parametrize("name,arguments", REPORTING_CALLS)
def test_a_reporting_tool_refuses_a_per_call_project(
    monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict, field: str
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    (result,) = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        (name, {**arguments, field: OTHER_PROJECT_ID}),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert core.requests == []


def test_reporting_tools_declare_no_project_and_read_tools_declare_project() -> None:
    tools = {
        tool.definition.name: tool.definition.input_schema["properties"]
        for tool in mcp_server.get_tool_catalog(profile="guardrails")
    }
    for name in ("budget_check", "budget_check_batch", "budget_confirm", "record_call"):
        assert "project" not in tools[name] and "project_id" not in tools[name]
    assert "project" in tools["budget_status"]
    assert all("project_id" not in properties for properties in tools.values())


# --- Read tools take a project per call --------------------------------------------


def test_a_read_tool_takes_a_project_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    (result,) = call(
        mcp_server.McpLaunchOptions(project_id=OTHER_PROJECT_ID),
        ("budget_status", {"project": "checkout-bot"}),
    )

    assert result["ok"] and result["data"]["project_id"] == PROJECT_ID
    assert core.paths() == [
        "GET /api/v1/auth/key",
        f"GET /api/v1/projects/{PROJECT_ID}/budget",
    ]


def test_a_read_tool_takes_a_project_by_id_without_a_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    (result,) = call(
        mcp_server.McpLaunchOptions(), ("budget_status", {"project": PROJECT_ID})
    )

    assert result["ok"]
    assert core.paths() == [f"GET /api/v1/projects/{PROJECT_ID}/budget"]
    assert core.requests[0].headers["Solwyn-Project"] == PROJECT_ID


# --- doctor --mcp checks the key and launch-project pair ---------------------------


def _diagnose(monkeypatch: pytest.MonkeyPatch, project: str | None) -> Any:
    def no_child(*args: object, **kwargs: object) -> None:
        raise ValueError("no child process needed")

    monkeypatch.setattr(mcp_doctor, "resolve_executable", no_child)
    context = runtime.resolve_command_context(
        api_url=None,
        project=project,
        output_format=None,
        json_flag=True,
        no_color=True,
    )
    return mcp_doctor.diagnose(context, None, "read")


def test_doctor_mcp_reports_the_key_project_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    data = _diagnose(monkeypatch, "checkout-bot")

    pair = data.key_project
    assert pair.status == "ok"
    assert (pair.project_id, pair.project_name, pair.budget_mode) == (
        PROJECT_ID,
        "checkout-bot",
        "hard_deny",
    )
    assert pair.expires_at == "2027-11-04T10:12:00Z"
    assert core.requests[0].headers["Solwyn-Project"] == "checkout-bot"


def test_doctor_mcp_names_cores_refusal_of_the_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.auth_key_status = [401]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    data = _diagnose(monkeypatch, PROJECT_ID)

    assert data.status == "degraded"
    assert data.key_project.status == "failed"
    assert data.key_project.code == "api_key_expired"
    assert data.key_project.detail.startswith("This API key expired on 2026-11-01.")


def test_doctor_mcp_reports_an_unknown_launch_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    data = _diagnose(monkeypatch, "nope")

    assert data.key_project.status == "failed"
    assert data.key_project.code == "project_not_found"


# --- Review round 1 (#57): the launch inputs are frozen at server start --------


def _repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> Path:
    _repo(tmp_path, monkeypatch, value)
    return tmp_path / "repo"


def _session(server: Any, edit: Any, *, before_first: bool = False) -> list[dict]:
    async def probe() -> list[dict]:
        async with Client(server, cache=None) as client:
            if before_first:
                edit()
            first = await client.call_tool("budget_check", CHECK_ARGS)
            if not before_first:
                edit()
            second = await client.call_tool("budget_check", CHECK_ARGS)
            return [first.structured_content, second.structured_content]

    return anyio.run(probe)


def _sent_projects(core: Core) -> set[str | None]:
    return {
        r.headers.get("Solwyn-Project")
        for r in core.requests
        if r.url.path.startswith("/api/v1/budgets")
    }


def test_a_retry_after_a_busy_first_bind_keeps_the_launch_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo_root(tmp_path, monkeypatch, "checkout-bot")
    core = Core(monkeypatch)
    core.auth_key_status = [503, 503, 200]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    server = mcp_server.create_server(launch_options=mcp_server.McpLaunchOptions())

    first, second = _session(
        server,
        lambda: (root / ".solwyn.env").write_text(repo_file.render("research")),
    )

    assert first["error"]["code"] == "service_busy"
    assert second["ok"] and second["project_id"] == PROJECT_ID
    assert _sent_projects(core) == {PROJECT_ID}


def test_a_repo_file_edit_before_the_first_call_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo_root(tmp_path, monkeypatch, "checkout-bot")
    core = Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    server = mcp_server.create_server(launch_options=mcp_server.McpLaunchOptions())

    first, second = _session(
        server,
        lambda: (root / ".solwyn.env").write_text(repo_file.render("research")),
        before_first=True,
    )

    assert first["ok"] and first["project_id"] == PROJECT_ID
    assert second["ok"] and second["project_id"] == PROJECT_ID
    assert _sent_projects(core) == {PROJECT_ID}


def test_env_and_cwd_changes_after_launch_change_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo_root(tmp_path, monkeypatch, "checkout-bot")
    core = Core(monkeypatch)
    core.auth_key_status = [503, 503, 200]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    server = mcp_server.create_server(launch_options=mcp_server.McpLaunchOptions())

    def elsewhere() -> None:
        monkeypatch.setenv("SOLWYN_PROJECT", "research")
        monkeypatch.setenv("SOLWYN_API_KEY", OTHER_KEY)
        monkeypatch.chdir(tmp_path)

    first, second = _session(server, elsewhere)

    assert first["error"]["code"] == "service_busy"
    assert second["ok"] and second["project_id"] == PROJECT_ID
    assert {r.headers.get("Authorization") for r in core.requests} == {f"Bearer {KEY}"}


def test_a_busy_name_lookup_cannot_switch_between_stored_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two stored keys and a login: a 503 on the login's project list, then a
    repo-file edit, must not make the retry bind the other project's key."""
    root = _repo_root(tmp_path, monkeypatch, "checkout-bot")
    store_key(project_name="checkout-bot")
    store_key(
        project_id=OTHER_PROJECT_ID,
        project_name="research",
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=ACCOUNT_ID,
    )
    auth.store_login(LOGIN_TOKEN, login_meta(account_id=ACCOUNT_ID))
    core = Core(monkeypatch)
    reads = {"projects": 0}
    plain = core.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/projects":
            core.requests.append(request)
            reads["projects"] += 1
            if reads["projects"] <= 2:
                return httpx.Response(
                    503, json={"detail": "busy"}, headers={"Retry-After": "0"}
                )
            return httpx.Response(
                200,
                json=[
                    {"id": PROJECT_ID, "name": "checkout-bot"},
                    {"id": OTHER_PROJECT_ID, "name": "research"},
                ],
            )
        return plain(request)

    core.handle = handle  # type: ignore[method-assign]
    server = mcp_server.create_server()

    first, second = _session(
        server,
        lambda: (root / ".solwyn.env").write_text(repo_file.render("research")),
    )

    assert first["error"]["code"] == "service_busy"
    assert second["ok"] and second["project_id"] == PROJECT_ID
    assert not any(
        r.headers.get("Authorization") == f"Bearer {OTHER_KEY}" for r in core.requests
    )


# --- Review round 2 (#57): the credential store and login are frozen too -------


class _TwoAccountCore(Core):
    """Two accounts, each with a project named checkout-bot (A: PROJECT_ID
    with KEY and LOGIN_TOKEN; B: OTHER_PROJECT_ID with OTHER_KEY and
    OTHER_LOGIN_TOKEN)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.busy_projects = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization")
        if request.url.path == "/api/v1/projects":
            self.requests.append(request)
            if self.busy_projects:
                self.busy_projects -= 1
                return httpx.Response(
                    503, json={"detail": "busy"}, headers={"Retry-After": "0"}
                )
            other = bearer == f"Bearer {OTHER_LOGIN_TOKEN}"
            project = OTHER_PROJECT_ID if other else PROJECT_ID
            return httpx.Response(200, json=[{"id": project, "name": "checkout-bot"}])
        if request.url.path == "/api/v1/accounts/me":
            self.requests.append(request)
            other = bearer == f"Bearer {OTHER_LOGIN_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "id": OTHER_ACCOUNT_ID if other else ACCOUNT_ID,
                    "display_name": "A",
                },
            )
        if request.url.path == "/api/v1/auth/key":
            self.requests.append(request)
            other = bearer == f"Bearer {OTHER_KEY}"
            project = OTHER_PROJECT_ID if other else PROJECT_ID
            assert request.headers["Solwyn-Project"] == project
            return httpx.Response(
                200,
                json=auth_key_body(
                    project_id=project,
                    project_name="checkout-bot",
                    account_id=OTHER_ACCOUNT_ID if other else ACCOUNT_ID,
                    key=OTHER_KEY if other else KEY,
                    key_id=OTHER_KEY_ID if other else KEY_ID,
                ),
            )
        return super().handle(request)

    def used_other_key(self) -> bool:
        return any(
            r.headers.get("Authorization") == f"Bearer {OTHER_KEY}"
            for r in self.requests
        )


def _store_other_account(*, login: bool = False, mode: Any = None) -> None:
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=OTHER_ACCOUNT_ID,
        project_name="checkout-bot",
        mode=mode,
    )
    if login:
        auth.store_login(OTHER_LOGIN_TOKEN, login_meta(account_id=OTHER_ACCOUNT_ID))


@pytest.mark.parametrize("selector", ["file", "mode"])
def test_the_credential_store_is_chosen_at_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selector: str
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    original = credential_store.credentials_path()
    store_key(project_name="checkout-bot")
    alternate = tmp_path / "alternate" / "credentials.json"
    if selector == "file":
        monkeypatch.setenv(credential_store.FILE_ENV, str(alternate))
        _store_other_account(mode="file")
        monkeypatch.setenv(credential_store.FILE_ENV, str(original))
    else:
        _store_other_account(mode="keyring")
    core = _TwoAccountCore(monkeypatch)
    server = mcp_server.create_server()
    assert core.requests == []

    def edit() -> None:
        if selector == "file":
            monkeypatch.setenv(credential_store.FILE_ENV, str(alternate))
        else:
            monkeypatch.setenv(credential_store.STORE_ENV, "keyring")

    first, second = _session(server, edit, before_first=True)

    assert first["ok"] and second["ok"], (first, second)
    assert first["project_id"] == second["project_id"] == PROJECT_ID
    assert not core.used_other_key()


@pytest.mark.parametrize("edit_kind", ["unchanged", "file-env", "stored-login"])
def test_a_retry_never_resolves_through_another_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, edit_kind: str
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    original = credential_store.credentials_path()
    store_key(project_name="checkout-bot")
    _store_other_account()
    auth.store_login(LOGIN_TOKEN, login_meta(account_id=ACCOUNT_ID))
    alternate = tmp_path / "alternate" / "credentials.json"
    monkeypatch.setenv(credential_store.FILE_ENV, str(alternate))
    _store_other_account(login=True)
    monkeypatch.setenv(credential_store.FILE_ENV, str(original))
    core = _TwoAccountCore(monkeypatch)
    core.busy_projects = 2
    server = mcp_server.create_server()
    assert core.requests == []

    def edit() -> None:
        if edit_kind == "file-env":
            monkeypatch.setenv(credential_store.FILE_ENV, str(alternate))
        elif edit_kind == "stored-login":
            auth.store_login(OTHER_LOGIN_TOKEN, login_meta(account_id=OTHER_ACCOUNT_ID))

    first, second = _session(server, edit)

    assert first["error"]["code"] == "service_busy"
    assert not core.used_other_key()
    assert _sent_projects(core) <= {PROJECT_ID}
    if edit_kind == "stored-login":
        # The login the launch project was to be resolved through is gone:
        # refused for the server's life, never resolved through the new one.
        assert second["error"]["code"] == "credentials_changed", second
        assert not any(
            r.headers.get("Authorization") == f"Bearer {OTHER_LOGIN_TOKEN}"
            for r in core.requests
        )
        third = _session(server, lambda: None)
        assert [r["error"]["code"] for r in third] == ["credentials_changed"] * 2
        return
    assert second["ok"], second
    assert second["project_id"] == PROJECT_ID


def test_a_login_change_after_binding_keeps_the_bound_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    store_key(project_name="checkout-bot")
    _store_other_account()
    auth.store_login(LOGIN_TOKEN, login_meta(account_id=ACCOUNT_ID))
    core = _TwoAccountCore(monkeypatch)
    server = mcp_server.create_server()

    first, second = _session(
        server,
        lambda: auth.store_login(
            OTHER_LOGIN_TOKEN, login_meta(account_id=OTHER_ACCOUNT_ID)
        ),
    )

    assert first["ok"] and second["ok"], (first, second)
    assert first["project_id"] == second["project_id"] == PROJECT_ID
    assert not core.used_other_key()


def test_an_environment_login_ignores_the_stored_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    store_key(project_name="checkout-bot")
    _store_other_account()
    monkeypatch.setenv("SOLWYN_TOKEN", LOGIN_TOKEN)
    core = _TwoAccountCore(monkeypatch)
    core.busy_projects = 2
    server = mcp_server.create_server()

    def edit() -> None:
        auth.store_login(OTHER_LOGIN_TOKEN, login_meta(account_id=OTHER_ACCOUNT_ID))
        monkeypatch.setenv("SOLWYN_TOKEN", OTHER_LOGIN_TOKEN)

    first, second = _session(server, edit)

    assert first["error"]["code"] == "service_busy"
    assert second["ok"], second
    assert second["project_id"] == PROJECT_ID
    assert not core.used_other_key()


# --- PR #58 review: get_whoami names the frozen launch project -----------------


class _IdentityCore(Core):
    """Core plus the three identity reads a login's get_whoami makes."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/account/me":
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "user_id": "user_123",
                    "email_verified": True,
                    "email": "person@example.com",
                    "display_name": "Person Example",
                    "subscription_tier": "production",
                    "first_name": "Person",
                    "marketing_emails_subscribed": True,
                    "account_id": ACCOUNT_ID,
                    "alert_email_override": None,
                },
            )
        if path == "/api/v1/accounts/me":
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": ACCOUNT_ID,
                    "display_name": "Person Example",
                    "subscription_tier": "production",
                    "alert_email_override": None,
                    "seat_limit": 2,
                    "seats_used": 1,
                    "your_role": "owner",
                },
            )
        if path == "/api/v1/projects" and request.method == "GET":
            self.requests.append(request)
            return httpx.Response(200, json=[])
        return super().handle(request)


def test_get_whoami_names_the_launch_project_after_the_repo_file_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _repo_root(tmp_path, monkeypatch, "checkout-bot")
    auth.store_login(LOGIN_TOKEN, login_meta(account_id=ACCOUNT_ID))
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    core = _IdentityCore(monkeypatch)
    server = mcp_server.create_server()

    async def probe() -> list[dict]:
        async with Client(server, cache=None) as client:
            before = await client.call_tool("get_whoami", {})
            (root / ".solwyn.env").write_text(repo_file.render("research"))
            after = await client.call_tool("get_whoami", {})
            spend = await client.call_tool("budget_check", CHECK_ARGS)
            return [
                before.structured_content,
                after.structured_content,
                spend.structured_content,
            ]

    before, after, spend = anyio.run(probe)

    assert before["ok"] and after["ok"] and spend["ok"], (before, after, spend)
    assert before["data"]["auth_type"] == "login"
    assert before["data"]["configured_project"] == "checkout-bot"
    # The project calls actually spend from, not the edited file's.
    assert after["data"]["configured_project"] == "checkout-bot"
    assert spend["project_id"] == PROJECT_ID
    assert _sent_projects(core) == {PROJECT_ID}


# --- Audit F4: a failed /accounts/me is retried, kept refusals say restart ------


class _FlakyAccountCore(_TwoAccountCore):
    """/api/v1/accounts/me fails the first times with the given failures."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, failures: list[Any]) -> None:
        super().__init__(monkeypatch)
        self.account_failures = failures

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts/me" and self.account_failures:
            self.requests.append(request)
            failure = self.account_failures.pop(0)
            if isinstance(failure, int):
                return httpx.Response(
                    failure, json={"detail": "busy"}, headers={"Retry-After": "0"}
                )
            raise httpx.ConnectError("synthetic Wi-Fi blip", request=request)
        return super().handle(request)


@pytest.mark.parametrize(
    "failures",
    [["transport"], [429, 429], [503, 503]],
    ids=["transport", "429", "503"],
)
def test_a_transient_accounts_me_failure_is_retried_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failures: list[Any]
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    store_key(project_name="checkout-bot")
    monkeypatch.setenv("SOLWYN_TOKEN", LOGIN_TOKEN)
    core = _FlakyAccountCore(monkeypatch, list(failures))

    first, second = call(
        mcp_server.McpLaunchOptions(),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert first["ok"] is False
    assert first["error"]["code"] == "account_unresolved"
    assert "Restart" not in (first["error"]["hint"] or "")
    assert second["ok"], second
    assert second["project_id"] == PROJECT_ID
    assert core.paths().count("GET /api/v1/accounts/me") >= 2


def test_a_kept_refusal_says_to_restart_the_mcp_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, monkeypatch, "checkout-bot")
    Core(monkeypatch)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id="research"),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    for refused in (first, second):
        assert refused["error"]["code"] == "project_mismatch"
        assert "Restart the MCP server" in refused["error"]["hint"]


def test_a_kept_core_refusal_says_to_restart_the_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = Core(monkeypatch)
    core.auth_key_status = [401]
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)

    first, second = call(
        mcp_server.McpLaunchOptions(project_id=PROJECT_ID),
        ("budget_check", CHECK_ARGS),
        ("budget_check", CHECK_ARGS),
    )

    assert first["ok"] is False and second["ok"] is False
    assert first["error"]["code"] == second["error"]["code"]
    assert "Restart the MCP server" in (second["error"]["hint"] or "")
    assert core.paths().count("GET /api/v1/auth/key") == 1
