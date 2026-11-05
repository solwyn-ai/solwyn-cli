"""Setup commands run the shared setup checks before creating or writing
anything (spec section 7.3 step 3 and 5, review round 1 item 4)."""

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from key_fixtures import ACCOUNT_ID, OTHER_PROJECT_ID, PROJECT_ID
from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth, project_resolver
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import init as init_module
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.main import app
from test_projects_command import PROJECT_CREATE_RESPONSE

runner = CliRunner()
ENV_TOKEN = "solwyn_cli_" + "e7" * 32
PROJECTS = [
    {
        "id": PROJECT_ID,
        "name": "checkout-bot",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "hard_deny",
        "created_at": "2026-11-01T00:00:00Z",
        "dedicated_keys_only": True,
    },
    {
        "id": OTHER_PROJECT_ID,
        "name": "research",
        "budget_limit": 50.0,
        "budget_period": "monthly",
        "budget_mode": "alert_only",
        "created_at": "2026-11-01T00:00:00Z",
        "dedicated_keys_only": False,
    },
]


@pytest.fixture(autouse=True)
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (
        "SOLWYN_API_KEY",
        "SOLWYN_PROJECT",
        "SOLWYN_PROJECT_ID",
        "SOLWYN_TOKEN",
        "SOLWYN_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".solwyn.env").write_text('SOLWYN_PROJECT="checkout-bot"\n')
    monkeypatch.chdir(root)
    return root


def serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = functools.partial(SolwynClient, transport=httpx.MockTransport(record))
    for module in (projects_module, init_module):
        monkeypatch.setattr(module, "SolwynClient", client)
    monkeypatch.setattr(project_resolver, "_client_factory", lambda: client)
    return requests


def routes(table: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        status, body = table.get(
            f"{request.method} {request.url.path}", (599, {"detail": "unexpected"})
        )
        return httpx.Response(status, json=body, headers={"Retry-After": "0"})

    return handle


def files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("store_key", [True, False])
def test_projects_create_refuses_a_flag_that_disagrees_with_the_repo_file(
    monkeypatch: pytest.MonkeyPatch, store_key: bool
) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta())
    requests = serve(
        monkeypatch,
        routes(
            {
                "GET /api/v1/projects": (200, PROJECTS),
                "POST /api/v1/projects": (201, PROJECT_CREATE_RESPONSE),
            }
        ),
    )

    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "research",
            "projects",
            "create",
            "--name",
            "Created project",
            *(["--store-key"] if store_key else []),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_mismatch"
    assert all(request.method == "GET" for request in requests)
    assert auth.stored_api_keys() == {}


def test_projects_create_resolves_an_env_logins_account_before_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    requests = serve(
        monkeypatch,
        routes(
            {
                "GET /api/v1/accounts/me": (503, {"detail": "unavailable"}),
                "POST /api/v1/projects": (201, PROJECT_CREATE_RESPONSE),
            }
        ),
    )

    result = runner.invoke(
        app, ["--json", "projects", "create", "--name", "Created project"]
    )

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "account_unresolved"
    assert [request.method for request in requests if request.method == "POST"] == []


def test_init_agents_refuses_a_flag_that_disagrees_with_the_repo_file(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta())
    before = files(repo)
    requests = serve(monkeypatch, routes({"GET /api/v1/projects": (200, PROJECTS)}))

    result = runner.invoke(app, ["--json", "--project", "research", "init", "--agents"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_mismatch"
    assert files(repo) == before
    assert all(request.method == "GET" for request in requests)


def test_interactive_env_login_init_knows_its_account_before_creating_a_project(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    (repo / ".solwyn.env").unlink()
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    requests = serve(
        monkeypatch,
        routes(
            {
                "GET /api/v1/projects": (200, []),
                "POST /api/v1/projects": (201, PROJECT_CREATE_RESPONSE),
                "GET /api/v1/accounts/me": (503, {"detail": "unavailable"}),
            }
        ),
    )

    result = runner.invoke(app, ["--json", "init"], input="Created project\n100\n")

    assert result.exit_code == 3
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error["code"] == "account_unresolved"
    assert not any(request.method == "POST" for request in requests)
    assert auth.stored_api_keys() == {}


def test_interactive_env_login_init_stores_the_new_key_with_its_account(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    (repo / ".solwyn.env").unlink()
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module, "_verify_budget", lambda *a, **kw: None)
    serve(
        monkeypatch,
        routes(
            {
                "GET /api/v1/projects": (200, []),
                "POST /api/v1/projects": (201, PROJECT_CREATE_RESPONSE),
                "GET /api/v1/accounts/me": (
                    200,
                    {"id": ACCOUNT_ID, "display_name": "A"},
                ),
            }
        ),
    )

    runner.invoke(app, ["--json", "init"], input="Created project\n100\n")

    stored = auth.stored_api_keys()
    assert list(stored) == [PROJECT_CREATE_RESPONSE["id"]]
    assert stored[PROJECT_CREATE_RESPONSE["id"]].account_id == ACCOUNT_ID
