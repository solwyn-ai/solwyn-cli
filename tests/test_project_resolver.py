"""The one project resolver (spec section 7.3), end to end through the real client.

Every request goes through SolwynClient over an httpx MockTransport, so these
tests see the Authorization and Solwyn-Project headers Core would see.
"""

import copy
import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

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
from login_fixtures import LOGIN_TOKEN, login_meta
from solwyn_cli import auth, project_resolver, runtime
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.commands import costs as costs_module
from solwyn_cli.commands import status as status_module
from solwyn_cli.config import Config
from solwyn_cli.main import app
from test_budget_check_command import ALLOW_RESPONSE, BASE_ARGS
from test_costs_command import _response as costs_response
from test_status_command import BUDGET_RESPONSE, PROVIDERS_RESPONSE

runner = CliRunner()
ENV_TOKEN = "solwyn_cli_" + "e7" * 32
NOT_FOUND = {
    "detail": {
        "code": "project_not_found",
        "message": (
            "No project on this API key matches Solwyn-Project. Check the name or "
            "proj_ id. A renamed project's old name stops working; its proj_ id "
            "always works."
        ),
    }
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    # Outside any git repo, so no .solwyn.env is found unless a test makes one.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(costs_module, "get_capabilities", lambda **_: None)


Handler = Callable[[httpx.Request], httpx.Response]


def serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route the commands' and the resolver's requests to one fake Core."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = functools.partial(SolwynClient, transport=httpx.MockTransport(record))
    for module in (costs_module, status_module, budget_module):
        monkeypatch.setattr(module, "SolwynClient", client)
    monkeypatch.setattr(project_resolver, "_client_factory", lambda: client)
    return requests


def core(routes: dict[str, Any]) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        answer = routes.get(f"{request.method} {request.url.path}")
        if answer is None:
            return httpx.Response(599, json={"detail": "unexpected request"})
        if callable(answer):
            return answer(request)
        status, body = answer
        return httpx.Response(status, json=body)

    return handle


def paths(requests: list[httpx.Request]) -> list[str]:
    return [f"{request.method} {request.url.path}" for request in requests]


def project_header(request: httpx.Request) -> str | None:
    return request.headers.get("Solwyn-Project")


def store_login(account_id: str = ACCOUNT_ID, account_name: str = "Acme") -> None:
    auth.store_login(
        LOGIN_TOKEN, login_meta(account_id=account_id, account_name=account_name)
    )


def costs_routes(project_id: str = PROJECT_ID) -> dict[str, Any]:
    return {
        f"GET /api/v1/projects/{project_id}/costs": (
            200,
            costs_response(project_id=project_id),
        )
    }


# --- Step 1: candidates --------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "environ", "repo", "config", "selected", "source"),
    [
        ("flag", {"SOLWYN_PROJECT": "env"}, "repo", "cfg", "flag", "cli"),
        (None, {"SOLWYN_PROJECT": "env"}, "repo", "cfg", "env", "environment"),
        (None, {"SOLWYN_PROJECT_ID": "legacy"}, "repo", "cfg", "repo", "repo"),
        (
            None,
            {"SOLWYN_PROJECT_ID": "legacy"},
            None,
            "cfg",
            "legacy",
            "legacy_environment",
        ),
        (None, {}, None, "cfg", "cfg", "config"),
        (None, {}, None, None, None, "none"),
        (" \tflag\t ", {}, None, None, "flag", "cli"),
    ],
)
def test_selector_precedence_is_flag_then_env_then_repo_file(
    tmp_path: Path,
    flag: str | None,
    environ: dict[str, str],
    repo: str | None,
    config: str | None,
    selected: str | None,
    source: str,
) -> None:
    (tmp_path / ".git").mkdir()
    if repo is not None:
        (tmp_path / ".solwyn.env").write_text(f'SOLWYN_PROJECT="{repo}"\n')

    value, chosen, repo_value, error = runtime.project_candidates(
        flag=flag,
        environ=environ,
        config=Config(default_project=config),
        cwd=tmp_path,
    )

    assert (value, chosen, repo_value, error) == (selected, source, repo, None)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        (
            '# Solwyn project for this repo. Not a secret: commit this file.\nSOLWYN_PROJECT="checkout-bot"\n',
            "checkout-bot",
        ),
        (
            "\n  SOLWYN_PROJECT = 'checkout bot'  \n\n# trailing comment\n",
            "checkout bot",
        ),
        (f"SOLWYN_PROJECT={PROJECT_ID}\n", PROJECT_ID),
    ],
)
def test_repo_file_accepts_one_assignment_quotes_comments_and_blank_lines(
    text: str, value: str
) -> None:
    assert project_resolver.parse_repo_file(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        "SOLWYN_API_KEY=solwyn_sk_x\n",
        'SOLWYN_PROJECT="a"\nSOLWYN_PROJECT="b"\n',
        'SOLWYN_PROJECT="unbalanced\n',
        "export SOLWYN_PROJECT=a\n",
        'SOLWYN_PROJECT="$HOME"\n',
        'SOLWYN_PROJECT=""\n',
    ],
)
def test_anything_else_in_the_repo_file_is_refused(text: str) -> None:
    with pytest.raises(project_resolver.RepoFileError):
        project_resolver.parse_repo_file(text)


def test_the_repo_file_is_found_up_to_the_git_root_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".solwyn.env").write_text('SOLWYN_PROJECT="checkout-bot"\n')
    (tmp_path / ".solwyn.env").write_text('SOLWYN_PROJECT="outside"\n')

    assert project_resolver.read_repo_project(nested) == ("checkout-bot", None)
    assert project_resolver.read_repo_project(tmp_path) == (None, None)


def test_an_invalid_repo_file_exits_2_when_a_project_is_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".solwyn.env").write_text("SOLWYN_PROJECT=a\nOTHER=b\n")
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    # The file is still read for the mismatch check when env selects.
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")
    requests = serve(monkeypatch, core({}))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "repo_file_invalid"
    assert requests == []


# --- Key-only reads (SOLWYN_API_KEY, no login) ---------------------------------


@pytest.mark.parametrize("command", ["costs", "status"])
def test_a_key_only_read_with_a_name_asks_auth_key_once_then_uses_the_id(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")
    routes: dict[str, Any] = {
        "GET /api/v1/auth/key": (200, auth_key_body()),
        **costs_routes(),
        f"GET /api/v1/projects/{PROJECT_ID}/budget": (
            200,
            {**BUDGET_RESPONSE, "project_id": PROJECT_ID},
        ),
        f"GET /api/v1/projects/{PROJECT_ID}/providers": (200, PROVIDERS_RESPONSE),
    }
    requests = serve(monkeypatch, core(routes))

    result = runner.invoke(app, ["--json", command])

    assert result.exit_code == 0, result.stderr
    assert paths(requests)[0] == "GET /api/v1/auth/key"
    assert paths(requests).count("GET /api/v1/auth/key") == 1
    assert project_header(requests[0]) == "checkout-bot"
    for request in requests[1:]:
        assert request.url.path.startswith(f"/api/v1/projects/{PROJECT_ID}/")
        assert project_header(request) == PROJECT_ID
        assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_a_key_only_read_with_a_proj_id_makes_no_auth_key_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT", PROJECT_ID.upper().replace("PROJ_", "proj_"))
    requests = serve(monkeypatch, core(costs_routes()))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == [f"GET /api/v1/projects/{PROJECT_ID}/costs"]
    assert project_header(requests[0]) == PROJECT_ID


def test_a_renamed_projects_old_name_exits_2_with_cores_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT", "old-name")
    requests = serve(monkeypatch, core({"GET /api/v1/auth/key": (404, NOT_FOUND)}))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_not_found"
    assert error["message"] == NOT_FOUND["detail"]["message"]
    assert paths(requests) == ["GET /api/v1/auth/key"]


def test_a_key_with_no_project_selected_lets_core_name_the_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    detail = {
        "detail": {
            "code": "project_selector_required",
            "message": (
                "This API key needs a project. Set SOLWYN_PROJECT, or send the "
                "Solwyn-Project header, to a project name or proj_ id."
            ),
        }
    }
    requests = serve(monkeypatch, core({"POST /api/v1/budgets/check": (400, detail)}))

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_selector_required"
    assert "SOLWYN_PROJECT" in error["hint"]
    assert project_header(requests[0]) is None


@pytest.mark.parametrize(
    ("status", "body", "exit_code"),
    [
        (
            401,
            {
                "detail": {
                    "code": "api_key_expired",
                    "message": "This API key expired on 2026-11-01. Rotate it in the dashboard or with solwyn keys rotate.",
                    "expired_at": "2026-11-01T00:00:00Z",
                    "api_key_id": KEY_ID,
                    "project_budget_mode": "hard_deny",
                }
            },
            3,
        ),
        (
            401,
            {
                "detail": {
                    "code": "api_key_invalid",
                    "message": "Invalid or revoked API key.",
                    "project_budget_mode": None,
                }
            },
            3,
        ),
        (
            403,
            {
                "detail": {
                    "code": "key_not_dedicated",
                    "message": "Project checkout-bot only accepts API keys dedicated to it, and this key also reaches other projects. Use a key that holds only this project.",
                    "project_id": PROJECT_ID,
                }
            },
            3,
        ),
        (
            400,
            {
                "detail": {
                    "code": "project_selector_conflict",
                    "message": "Send one Solwyn-Project header.",
                    "reason": "multiple_headers",
                }
            },
            2,
        ),
    ],
)
def test_auth_key_refusals_keep_cores_code_and_message(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], exit_code: int
) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")
    serve(monkeypatch, core({"GET /api/v1/auth/key": (status, body)}))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == exit_code
    error = json.loads(result.stderr)
    assert error["code"] == body["detail"]["code"]
    assert error["message"] == body["detail"]["message"]
    assert error["hint"]
    assert KEY not in result.output


# --- Stored keys without a login -------------------------------------------------


def test_without_a_login_or_env_key_a_name_picks_the_stored_entry_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_key()
    monkeypatch.setenv("SOLWYN_PROJECT", "Checkout-Bot")
    requests = serve(monkeypatch, core(costs_routes()))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == [f"GET /api/v1/projects/{PROJECT_ID}/costs"]
    assert project_header(requests[0]) == PROJECT_ID
    assert requests[0].headers["Authorization"] == f"Bearer {KEY}"


def test_two_stored_same_named_projects_without_a_login_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=OTHER_ACCOUNT_ID,
    )
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")
    requests = serve(monkeypatch, core({}))

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_ambiguous"
    assert error["message"] == (
        "More than one stored API key is for a project named checkout-bot "
        "(different accounts). Use the proj_ id, or sign in with solwyn login."
    )
    assert requests == []


def test_an_env_key_for_spend_is_never_blocked_by_stored_name_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=OTHER_ACCOUNT_ID,
    )
    env_key = "solwyn_sk_" + "c3" * 32
    monkeypatch.setenv("SOLWYN_API_KEY", env_key)
    monkeypatch.setenv("SOLWYN_PROJECT", "checkout-bot")
    requests = serve(
        monkeypatch,
        core(
            {
                "GET /api/v1/auth/key": (200, auth_key_body(key=env_key)),
                "POST /api/v1/budgets/check": (
                    200,
                    {**ALLOW_RESPONSE, "project_id": PROJECT_ID},
                ),
            }
        ),
    )

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == ["GET /api/v1/auth/key", "POST /api/v1/budgets/check"]
    assert all(r.headers["Authorization"] == f"Bearer {env_key}" for r in requests)
    assert project_header(requests[1]) == PROJECT_ID


def test_spend_with_a_stored_key_uses_the_selected_projects_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    requests = serve(
        monkeypatch,
        core(
            {
                "POST /api/v1/budgets/check": (
                    200,
                    {**ALLOW_RESPONSE, "project_id": OTHER_PROJECT_ID},
                )
            }
        ),
    )

    result = runner.invoke(app, ["--json", "--project", "research", *BASE_ARGS])

    assert result.exit_code == 0, result.stderr
    assert requests[0].headers["Authorization"] == f"Bearer {OTHER_KEY}"
    assert project_header(requests[0]) == OTHER_PROJECT_ID


# --- Logins ------------------------------------------------------------------------


def test_a_login_turns_a_name_into_an_id_through_its_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_login()
    monkeypatch.setenv("SOLWYN_PROJECT", "CHECKOUT-BOT")
    projects = [
        {**BUDGET_RESPONSE, "id": OTHER_PROJECT_ID, "name": "research"},
        {"id": PROJECT_ID, "name": "checkout-bot"},
    ]
    requests = serve(
        monkeypatch,
        core({"GET /api/v1/projects": (200, projects), **costs_routes()}),
    )

    result = runner.invoke(app, ["--json", "costs"])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == [
        "GET /api/v1/projects",
        f"GET /api/v1/projects/{PROJECT_ID}/costs",
    ]
    # A login never sends Solwyn-Project: Core takes its project from the URL.
    assert all(project_header(request) is None for request in requests)


def test_a_login_with_an_unknown_name_names_its_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_login(account_name="Acme")
    serve(monkeypatch, core({"GET /api/v1/projects": (200, [])}))

    result = runner.invoke(app, ["--json", "--project", "nope", "costs"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_not_found"
    assert error["message"] == "No project named nope in Acme."


def test_an_env_login_resolves_its_account_before_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    requests = serve(
        monkeypatch,
        core(
            {
                "GET /api/v1/accounts/me": (
                    200,
                    {"id": ACCOUNT_ID, "display_name": "Acme"},
                ),
                "GET /api/v1/projects": (
                    200,
                    [{"id": PROJECT_ID, "name": "checkout-bot"}],
                ),
                **costs_routes(),
            }
        ),
    )

    result = runner.invoke(app, ["--json", "--project", "checkout-bot", "costs"])

    assert result.exit_code == 0, result.stderr
    assert paths(requests)[:2] == ["GET /api/v1/accounts/me", "GET /api/v1/projects"]


@pytest.mark.parametrize("status", [500, 401])
def test_an_env_login_whose_account_cannot_be_read_fails_closed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    store_key()
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    requests = serve(
        monkeypatch,
        core({"GET /api/v1/accounts/me": (status, {"detail": "nope"})}),
    )

    result = runner.invoke(app, ["--json", "--project", "checkout-bot", "costs"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "account_unresolved"
    assert error["message"] == (
        "Could not confirm which account your Solwyn login belongs to. Check your "
        "connection and try again."
    )
    assert paths(requests) == ["GET /api/v1/accounts/me"]


def test_a_login_with_a_proj_id_makes_no_resolution_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_TOKEN", ENV_TOKEN)
    requests = serve(monkeypatch, core(costs_routes()))

    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, "costs"])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == [f"GET /api/v1/projects/{PROJECT_ID}/costs"]


# --- Spend: account check and mismatch ---------------------------------------------


def test_spend_refuses_a_stored_key_from_another_account_than_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_key(account_id=OTHER_ACCOUNT_ID)
    store_login(account_id=ACCOUNT_ID)
    requests = serve(monkeypatch, core({}))

    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, *BASE_ARGS])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "api_key_account_mismatch"
    assert error["message"] == (
        f"The stored API key for {PROJECT_ID} belongs to another account than your "
        f"current login. Run solwyn keys create --project {PROJECT_ID} --store-key."
    )
    assert requests == []


def test_spend_compares_an_env_keys_account_with_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_login(account_id=ACCOUNT_ID)
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    requests = serve(
        monkeypatch,
        core(
            {"GET /api/v1/auth/key": (200, auth_key_body(account_id=OTHER_ACCOUNT_ID))}
        ),
    )

    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, *BASE_ARGS])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "api_key_account_mismatch"
    assert paths(requests) == ["GET /api/v1/auth/key"]


def _repo(tmp_path: Path, value: str) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".solwyn.env").write_text(
        "# Solwyn project for this repo. Not a secret: commit this file.\n"
        f'SOLWYN_PROJECT="{value}"\n'
    )


def test_spend_exits_on_a_mismatch_with_the_repo_file_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, "checkout-bot")
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    monkeypatch.setenv("SOLWYN_PROJECT", "research")
    requests = serve(monkeypatch, core({}))

    result = runner.invoke(app, ["--json", *BASE_ARGS])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_mismatch"
    assert error["message"] == (
        "This repo is set to checkout-bot in .solwyn.env, but SOLWYN_PROJECT says "
        "research. Edit .solwyn.env if the repo should use another project."
    )
    assert requests == []


def test_a_read_warns_once_on_a_mismatch_and_uses_the_selected_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, "checkout-bot")
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    requests = serve(monkeypatch, core(costs_routes(OTHER_PROJECT_ID)))

    result = runner.invoke(app, ["--json", "--project", "research", "costs"])

    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("This repo is set to checkout-bot in .solwyn.env") == 1
    assert "--project says research" in result.stderr
    assert paths(requests) == [f"GET /api/v1/projects/{OTHER_PROJECT_ID}/costs"]


def test_the_same_project_by_name_and_by_id_is_not_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, "checkout-bot")
    store_key()
    requests = serve(
        monkeypatch,
        core(
            {
                "POST /api/v1/budgets/check": (
                    200,
                    {**ALLOW_RESPONSE, "project_id": PROJECT_ID},
                )
            }
        ),
    )

    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, *BASE_ARGS])

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == ["POST /api/v1/budgets/check"]


# --- login --with-key ----------------------------------------------------------------


def test_login_with_key_checks_the_key_then_stores_it_under_the_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = serve(
        monkeypatch, core({"GET /api/v1/auth/key": (200, auth_key_body())})
    )

    result = runner.invoke(
        app,
        ["--json", "--project", "checkout-bot", "login", "--with-key"],
        input=KEY,
    )

    assert result.exit_code == 0, result.stderr
    assert paths(requests) == ["GET /api/v1/auth/key"]
    assert project_header(requests[0]) == "checkout-bot"
    stored = auth.stored_api_keys()
    assert list(stored) == [PROJECT_ID]
    assert stored[PROJECT_ID].account_id == ACCOUNT_ID
    assert stored[PROJECT_ID].key_id == KEY_ID
    assert stored[PROJECT_ID].project_name == "checkout-bot"
    data = json.loads(result.stdout)
    assert data["project_id"] == PROJECT_ID
    assert KEY not in result.output


def test_login_with_key_needs_a_project_and_stores_nothing_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = serve(monkeypatch, core({}))

    result = runner.invoke(app, ["--json", "login", "--with-key"], input=KEY)

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_required"
    assert requests == []
    assert auth.stored_api_keys() == {}


def test_login_with_key_refuses_an_old_project_key_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = serve(monkeypatch, core({}))
    old = "sk_proj_" + "a1" * 32

    result = runner.invoke(
        app, ["--json", "--project", PROJECT_ID, "login", "--with-key"], input=old
    )

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "invalid_api_key"
    assert "solwyn_sk_" in error["hint"]
    assert old not in result.output
    assert requests == []


def test_login_with_key_stores_nothing_on_a_repo_file_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _repo(tmp_path, "checkout-bot")
    requests = serve(
        monkeypatch,
        core(
            {
                "GET /api/v1/auth/key": lambda request: httpx.Response(
                    200,
                    json=auth_key_body(
                        project_id=OTHER_PROJECT_ID
                        if project_header(request) == "research"
                        else PROJECT_ID,
                        project_name=project_header(request) or "",
                    ),
                )
            }
        ),
    )

    result = runner.invoke(
        app, ["--json", "--project", "research", "login", "--with-key"], input=KEY
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_mismatch"
    assert {project_header(r) for r in requests} == {"research", "checkout-bot"}
    assert auth.stored_api_keys() == {}


# --- The client --------------------------------------------------------------------


def test_the_client_sends_solwyn_project_for_a_key_and_never_for_a_login() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(record)
    key = auth.ApiKeyCredential(api_key=KEY, source="environment", project=PROJECT_ID)
    login = auth.LoginCredential(token=LOGIN_TOKEN, meta=login_meta())
    for credential in (key, login):
        with SolwynClient(
            api_url="https://api.test", credential=credential, transport=transport
        ) as client:
            client.request(
                "GET", "/api/v1/whatever", headers={"Solwyn-Project": "forged"}
            )

    assert seen[0].headers["Solwyn-Project"] == PROJECT_ID
    assert "Solwyn-Project" not in seen[1].headers


def test_resolution_errors_never_echo_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLWYN_API_KEY", KEY)
    echoed = copy.deepcopy(NOT_FOUND)
    echoed["detail"]["message"] = f"bad key {KEY}"
    serve(monkeypatch, core({"GET /api/v1/auth/key": (404, echoed)}))

    result = runner.invoke(app, ["--json", "--project", "x", "costs"])

    assert KEY not in result.output
    assert "[REDACTED]" in result.stderr
