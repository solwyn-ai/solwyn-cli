"""API key command contract tests (spec 7.5): list, create, rotate, revoke,
remove-project, the dashboard-only add-project redirect, and keys env."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from key_fixtures import (
    ACCOUNT_ID,
    KEY,
    KEY_ID,
    OTHER_ACCOUNT_ID,
    OTHER_KEY,
    OTHER_KEY_ID,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    FakeCore,
    store_key,
)
from login_fixtures import LOGIN_TOKEN, login_credential, login_meta
from solwyn_cli import auth
from solwyn_cli.commands import keys as keys_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
NEW_KEY = "solwyn_sk_" + "cd" * 32
NEW_KEY_ID = "6f1d0c9e-0000-4000-8000-0000000000f1"
STAGING = "https://api.staging.example.test"

PROJECT = {
    "id": PROJECT_ID,
    "name": "checkout-bot",
    "budget_mode": "hard_deny",
    "dedicated_keys_only": True,
}
OTHER_PROJECT = {
    "id": OTHER_PROJECT_ID,
    "name": "research",
    "budget_mode": "alert_only",
    "dedicated_keys_only": False,
}
SUMMARY: dict[str, Any] = {
    "id": KEY_ID,
    "name": "checkout-bot",
    "key_prefix": KEY[:26],
    "scope": "full",
    "created_via": "cli",
    "created_by": {"user_id": "user_ada", "display_name": "Ada"},
    "approval_id": "appr_1",
    "created_at": "2026-11-04T10:12:00Z",
    "expires_at": "2027-11-04T10:12:00Z",
    "last_used_at": None,
    "rotation_grace_until": None,
    "status": "active",
    "projects": [PROJECT],
}
LIST = {
    "keys": [
        SUMMARY,
        {
            **SUMMARY,
            "id": OTHER_KEY_ID,
            "name": "shared",
            "key_prefix": OTHER_KEY[:26],
            "created_via": "dashboard",
            "created_by": None,
            "approval_id": None,
            "status": "grace",
            "rotation_grace_until": "2026-11-08T10:12:00Z",
            "projects": [PROJECT, OTHER_PROJECT],
        },
    ],
    "used": 2,
    "limit": 25,
}
CREATED = {**SUMMARY, "id": NEW_KEY_ID, "key_prefix": NEW_KEY[:26], "key": NEW_KEY}
ROTATED = {
    **SUMMARY,
    "id": NEW_KEY_ID,
    "key_prefix": NEW_KEY[:26],
    "key": NEW_KEY,
    "replaced_key_id": KEY_ID,
    "replaced_key_rotation_grace_until": "2026-11-08T10:12:00Z",
}
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


class FakeClient:
    """The keys commands' client: queued answers and recorded requests."""

    instances: list["FakeClient"] = []
    responses: list[httpx.Response | BaseException] = []

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url
        self.credential = credential
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        result = self.__class__.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def keys_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(keys_module, "SolwynClient", FakeClient)
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    for name in (
        "SOLWYN_API_KEY",
        "SOLWYN_PROJECT",
        "SOLWYN_PROJECT_ID",
        "SOLWYN_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)
    # Outside any git repo: no .solwyn.env applies unless a test makes one.
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    yield


def _login(monkeypatch: pytest.MonkeyPatch) -> auth.LoginCredential:
    credential = login_credential()
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: credential)
    return credential


def _options(output_format: str) -> list[str]:
    return ["--json"] if output_format == "json" else ["--no-color"]


def _suffix(output_format: str) -> str:
    return "json" if output_format == "json" else "txt"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: str) -> Path:
    """A git repo whose .solwyn.env names ``project``; cwd moves into it."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".solwyn.env").write_text(
        "# Solwyn project for this repo. Not a secret: commit this file.\n"
        f'SOLWYN_PROJECT="{project}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return root


def _last_error(result: Any) -> dict[str, Any]:
    """The JSON error; confirmed effects are printed to stderr before it."""
    return json.loads(result.stderr.strip().splitlines()[-1])


def _rendered(result: Any) -> str:
    return result.stdout + result.stderr + repr(result.exception)


def test_keys_group_registers_the_cli_surface_and_hides_add_project() -> None:
    group = get_command(app).commands["keys"]
    assert set(group.commands) == {
        "list",
        "create",
        "rotate",
        "revoke",
        "remove-project",
        "add-project",
        "env",
    }
    assert group.commands["add-project"].hidden is True
    shown = runner.invoke(app, ["keys", "--help"], env={"COLUMNS": "200"})
    assert shown.exit_code == 0
    assert "add-project" not in shown.stdout
    assert "show" not in shown.stdout.split()


# --- keys list ----------------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_list_marks_keys_stored_here_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    credential = _login(monkeypatch)
    store_key()
    FakeClient.responses = [httpx.Response(200, json=LIST)]

    result = runner.invoke(
        app, [*_options(output_format), "keys", "list"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [("GET", "/api/v1/keys", {})]
    assert KEY not in _rendered(result)
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert [key["stored_here"] for key in payload["keys"]] == [[PROJECT_ID], []]
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-list-{output_format}.{_suffix(output_format)}",
    )


def test_keys_list_refuses_an_api_key_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = auth.ApiKeyCredential(api_key=KEY, source="environment")
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: key)

    result = runner.invoke(app, ["--json", "keys", "list"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "authentication_required"
    assert FakeClient.instances == []
    assert KEY not in _rendered(result)


# --- keys create ----------------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_create_shows_the_key_once_with_export_lines(
    monkeypatch: pytest.MonkeyPatch, output_format: str, fake_core: FakeCore
) -> None:
    _login(monkeypatch)
    FakeClient.responses = [httpx.Response(201, json=CREATED)]

    result = runner.invoke(
        app,
        [*_options(output_format), "keys", "create", "--project", PROJECT_ID],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    # A proj_ id and a stored login need no read.
    assert fake_core.calls == []
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/keys",
            {"json": {"project_id": PROJECT_ID}, "retry_rate_limit": False},
        )
    ]
    if output_format == "json":
        assert _rendered(result).count(NEW_KEY) == 1
        assert json.loads(result.stdout)["key"] == NEW_KEY
        assert json.loads(result.stdout)["stored"] is False
    else:
        # The panel, then the export lines.
        assert _rendered(result).count(NEW_KEY) == 2
        assert (
            f"export SOLWYN_API_KEY='{NEW_KEY}'\nexport SOLWYN_PROJECT='{PROJECT_ID}'\n"
        ) in result.stdout
    assert auth.stored_api_keys() == {}
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-create-{output_format}.{_suffix(output_format)}",
    )


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_create_store_key_stores_it_for_the_project_and_prints_no_secret(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    _login(monkeypatch)
    FakeClient.responses = [httpx.Response(201, json=CREATED)]

    result = runner.invoke(
        app,
        [*_options(output_format), "keys", "create", "-p", PROJECT_ID, "--store-key"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    assert NEW_KEY not in _rendered(result)
    entry = auth.stored_api_keys()[PROJECT_ID]
    assert entry.key.get_secret_value() == NEW_KEY
    assert entry.key_id == NEW_KEY_ID
    assert entry.account_id == ACCOUNT_ID
    assert entry.project_name == "checkout-bot"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-create-stored-{output_format}.{_suffix(output_format)}",
    )


def test_keys_create_resolves_a_name_through_the_login_and_sends_chosen_fields(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    _login(monkeypatch)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS))
    FakeClient.responses = [
        httpx.Response(
            201,
            json={
                **CREATED,
                "name": "ci",
                "scope": "read_only",
                "projects": [OTHER_PROJECT],
            },
        )
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "keys",
            "create",
            "--project",
            "Research",
            "--name",
            " ci ",
            "--scope",
            "read_only",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert [call[:2] for call in fake_core.calls] == [("GET", "/api/v1/projects")]
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "project_id": OTHER_PROJECT_ID,
        "name": "ci",
        "scope": "read_only",
    }


def test_keys_create_refuses_a_repo_mismatch_before_creating_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_core: FakeCore
) -> None:
    _login(monkeypatch)
    _repo(tmp_path, monkeypatch, "research")
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS))

    result = runner.invoke(app, ["--json", "keys", "create", "--project", PROJECT_ID])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_mismatch"
    assert error["message"] == (
        "This repo is set to research in .solwyn.env, but --project says "
        f"{PROJECT_ID}. Edit .solwyn.env if the repo should use another project."
    )
    assert FakeClient.instances == []


def test_keys_create_limit_reached_says_how_to_free_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)
    response = httpx.Response(
        402,
        json={
            "detail": {
                "code": "api_key_limit_reached",
                "message": "Your Free plan allows 3 API keys. Revoke a key or upgrade.",
                "current_tier": "free",
                "limit": 3,
                "cta_path": "/billing",
            }
        },
    )
    from solwyn_cli.errors import error_from_response

    FakeClient.responses = [error_from_response(response)]

    result = runner.invoke(app, ["--json", "keys", "create", "--project", PROJECT_ID])

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "api_key_limit_reached"
    assert error["hint"] == (
        "Revoke a key you no longer use (solwyn keys list, then solwyn keys "
        "revoke KEY_ID), or review plan limits in the dashboard: /billing"
    )
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {**CREATED, "projects": [OTHER_PROJECT]},
        {**CREATED, "projects": [PROJECT, OTHER_PROJECT]},
        {**CREATED, "key_prefix": KEY[:26]},
        {**CREATED, "key": "sk_proj_" + "cd" * 32},
    ],
)
def test_keys_create_rejects_a_wrong_response_without_storing_or_leaking(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    _login(monkeypatch)
    FakeClient.responses = [httpx.Response(201, json=body)]

    result = runner.invoke(
        app, ["--json", "keys", "create", "--project", PROJECT_ID, "--store-key"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "invalid_keys_response"
    assert NEW_KEY not in _rendered(result)
    assert auth.stored_api_keys() == {}


# --- keys rotate ----------------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_rotate_shows_the_replacement_once_when_not_stored_here(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    _login(monkeypatch)
    FakeClient.responses = [httpx.Response(200, json=ROTATED)]

    result = runner.invoke(
        app,
        [
            *_options(output_format),
            "keys",
            "rotate",
            KEY_ID,
            "--grace-seconds",
            "3600",
            "--yes",
        ],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            f"/api/v1/keys/{KEY_ID}/rotate",
            {"json": {"grace_seconds": 3600, "scope": None}, "retry_rate_limit": False},
        )
    ]
    assert _rendered(result).count(NEW_KEY) == 1
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-rotate-{output_format}.{_suffix(output_format)}",
    )


def test_keys_rotate_replaces_the_stored_copy_and_prints_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    FakeClient.responses = [httpx.Response(200, json=ROTATED)]

    result = runner.invoke(app, ["--json", "keys", "rotate", KEY_ID, "--yes"])

    assert result.exit_code == 0, result.stderr
    assert NEW_KEY not in _rendered(result)
    assert KEY not in _rendered(result)
    payload = json.loads(result.stdout)
    assert payload["stored"] is True
    assert payload["stored_projects"] == [PROJECT_ID]
    assert "key" not in payload
    stored = auth.stored_api_keys()
    assert stored[PROJECT_ID].key.get_secret_value() == NEW_KEY
    assert stored[PROJECT_ID].key_id == NEW_KEY_ID
    assert stored[PROJECT_ID].account_id == ACCOUNT_ID
    # A key with another id is left alone.
    assert stored[OTHER_PROJECT_ID].key.get_secret_value() == OTHER_KEY
    assert "Replaced the stored key on this computer" in result.stderr


def _store_fails_after_the_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keychain stops answering once Core has rotated (a lock after the
    browser approval, say): every read and write of stored keys fails."""

    class Locking(FakeClient):
        def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
            response = super().request(method, path, **kwargs)

            def locked(*args: Any, **kwargs: Any) -> Any:
                raise auth.CredentialStorageError("Synthetic keychain locked.")

            monkeypatch.setattr(auth, "stored_api_keys", locked)
            monkeypatch.setattr(auth, "store_api_key", locked)
            return response

    monkeypatch.setattr(keys_module, "SolwynClient", Locking)


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_rotate_still_shows_the_replacement_when_the_store_locks_after_core(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    _login(monkeypatch)
    _store_fails_after_the_rotation(monkeypatch)
    FakeClient.responses = [httpx.Response(200, json=ROTATED)]

    result = runner.invoke(
        app,
        [
            *_options(output_format),
            "keys",
            "rotate",
            KEY_ID,
            "--grace-seconds",
            "0",
            "--yes",
        ],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    assert NEW_KEY in result.stdout
    if output_format == "json":
        assert json.loads(result.stdout)["key"] == NEW_KEY


def test_keys_rotate_shows_the_replacement_when_the_stored_copy_cannot_be_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)
    store_key()
    _store_fails_after_the_rotation(monkeypatch)
    FakeClient.responses = [httpx.Response(200, json=ROTATED)]

    result = runner.invoke(
        app, ["--json", "keys", "rotate", KEY_ID, "--grace-seconds", "0", "--yes"]
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["key"] == NEW_KEY
    assert payload["stored"] is False
    assert "Could not replace the stored key for checkout-bot" in result.stderr


def test_keys_rotate_reads_local_holdings_before_asking_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)

    def locked(*args: Any, **kwargs: Any) -> Any:
        raise auth.CredentialStorageError("Synthetic keychain locked.")

    monkeypatch.setattr(auth, "stored_api_keys", locked)

    result = runner.invoke(app, ["--json", "keys", "rotate", KEY_ID, "--yes"])

    assert result.exit_code == 3
    assert _last_error(result)["code"] == "credential_storage_failed"
    assert FakeClient.instances == []


def test_keys_rotate_projects_changed_says_to_run_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)
    FakeClient.responses = [
        ApiError(
            status_code=409,
            code="key_projects_changed",
            message="This key's projects changed while it was being rotated. Try again.",
            hint="Run the same solwyn keys rotate command again.",
            detail=None,
        ),
        RuntimeError("must not retry"),
    ]

    result = runner.invoke(app, ["--json", "keys", "rotate", KEY_ID, "--yes"])

    assert result.exit_code == 1
    error = _last_error(result)
    assert error["code"] == "key_projects_changed"
    assert error["hint"] == "Run the same solwyn keys rotate command again."
    assert len(FakeClient.instances[0].requests) == 1


def test_key_projects_changed_hint_comes_from_the_error_mapping() -> None:
    from solwyn_cli.errors import error_from_response

    error = error_from_response(
        httpx.Response(
            409,
            json={
                "detail": {
                    "code": "key_projects_changed",
                    "message": "This key's projects changed while it was being rotated.",
                }
            },
        )
    )
    assert error.hint == "Run the same solwyn keys rotate command again."


@pytest.mark.parametrize(
    "arguments",
    [
        ["rotate", KEY_ID],
        ["revoke", KEY_ID],
        ["remove-project", KEY_ID, "--project", PROJECT_ID],
    ],
)
def test_destructive_key_commands_need_yes_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(
        keys_module, "resolve_credential", lambda **_: pytest.fail("no auth first")
    )

    result = runner.invoke(app, ["--json", "keys", *arguments])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "confirmation_required"
    assert FakeClient.instances == []


@pytest.mark.parametrize("grace", ["-1", "604801"])
def test_keys_rotate_rejects_an_out_of_range_grace_before_auth(
    monkeypatch: pytest.MonkeyPatch, grace: str
) -> None:
    monkeypatch.setattr(
        keys_module, "resolve_credential", lambda **_: pytest.fail("no auth first")
    )

    result = runner.invoke(
        app, ["--json", "keys", "rotate", KEY_ID, "--grace-seconds", grace, "--yes"]
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "invalid_key_rotation_request"


# --- keys revoke ----------------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_revoke_deletes_the_stored_copies(
    monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    _login(monkeypatch)
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    FakeClient.responses = [httpx.Response(204)]

    result = runner.invoke(
        app,
        [*_options(output_format), "keys", "revoke", KEY_ID, "--yes"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        ("DELETE", f"/api/v1/keys/{KEY_ID}", {"retry_rate_limit": False})
    ]
    assert set(auth.stored_api_keys()) == {OTHER_PROJECT_ID}
    assert KEY not in _rendered(result)
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-revoke-{output_format}.{_suffix(output_format)}",
    )


def test_keys_revoke_requires_a_bodyless_204(monkeypatch: pytest.MonkeyPatch) -> None:
    _login(monkeypatch)
    store_key()
    FakeClient.responses = [httpx.Response(200, json={})]

    result = runner.invoke(app, ["--json", "keys", "revoke", KEY_ID, "--yes"])

    assert result.exit_code == 1
    assert _last_error(result)["code"] == "invalid_keys_response"
    # Nothing is deleted locally when the server outcome is not the contract.
    assert set(auth.stored_api_keys()) == {PROJECT_ID}


def test_keys_revoke_not_found_points_at_keys_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solwyn_cli.errors import error_from_response

    _login(monkeypatch)
    FakeClient.responses = [
        error_from_response(
            httpx.Response(
                404,
                json={
                    "detail": {
                        "code": "api_key_not_found",
                        "message": "No live API key with that id in this account.",
                    }
                },
            )
        )
    ]

    result = runner.invoke(app, ["--json", "keys", "revoke", KEY_ID, "--yes"])

    assert result.exit_code == 5
    error = _last_error(result)
    assert error["code"] == "api_key_not_found"
    assert error["hint"] == "Run solwyn keys list to see this account's live keys."


# --- keys remove-project -----------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_remove_project_resolves_the_name_and_drops_the_stored_entry(
    monkeypatch: pytest.MonkeyPatch, output_format: str, fake_core: FakeCore
) -> None:
    _login(monkeypatch)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS))
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        project_name="research",
    )
    store_key()
    FakeClient.responses = [
        httpx.Response(200, json={**LIST["keys"][1], "projects": [PROJECT]})
    ]

    result = runner.invoke(
        app,
        [
            *_options(output_format),
            "keys",
            "remove-project",
            OTHER_KEY_ID,
            "--project",
            "research",
            "--yes",
        ],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "DELETE",
            f"/api/v1/keys/{OTHER_KEY_ID}/projects/{OTHER_PROJECT_ID}",
            {"retry_rate_limit": False},
        )
    ]
    assert set(auth.stored_api_keys()) == {PROJECT_ID}
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-remove-project-{output_format}.{_suffix(output_format)}",
    )


def test_keys_remove_project_keeps_a_stored_entry_of_another_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login(monkeypatch)
    store_key(project_id=OTHER_PROJECT_ID, project_name="research")
    FakeClient.responses = [
        httpx.Response(200, json={**LIST["keys"][1], "projects": [PROJECT]})
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "keys",
            "remove-project",
            OTHER_KEY_ID,
            "--project",
            OTHER_PROJECT_ID,
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["removed_stored_project"] is False
    assert set(auth.stored_api_keys()) == {OTHER_PROJECT_ID}


# --- keys add-project (dashboard only) ------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [[], [KEY_ID], [KEY_ID, "--project", PROJECT_ID]],
)
def test_adding_a_project_to_a_key_is_dashboard_only(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore, arguments: list[str]
) -> None:
    monkeypatch.setattr(
        keys_module, "resolve_credential", lambda **_: pytest.fail("no auth")
    )

    result = runner.invoke(app, ["--json", "keys", "add-project", *arguments])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "dashboard_only"
    assert error["message"] == (
        "Adding a project to a key is done in the dashboard: "
        "https://app.solwyn.ai/settings/keys"
    )
    assert FakeClient.instances == []
    assert fake_core.calls == []


def test_no_help_suggests_widening_a_key_from_the_cli() -> None:
    texts = [
        runner.invoke(app, ["keys", *command, "--help"], env={"COLUMNS": "200"}).stdout
        for command in ([], ["list"], ["create"], ["rotate"], ["revoke"], ["env"])
    ]
    assert not any("add-project" in text for text in texts)


# --- keys env -------------------------------------------------------------------


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_env_exports_the_stored_key_and_project(
    monkeypatch: pytest.MonkeyPatch, output_format: str, fake_core: FakeCore
) -> None:
    store_key()
    # SOLWYN_API_KEY is never read by keys env.
    monkeypatch.setenv("SOLWYN_API_KEY", OTHER_KEY)

    result = runner.invoke(
        app, [*_options(output_format), "keys", "env", "--project", "Checkout-Bot"]
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert OTHER_KEY not in _rendered(result)
    # No login: the name matched the stored entry locally, with no request.
    assert fake_core.calls == []
    expected = f"export SOLWYN_API_KEY='{KEY}'\nexport SOLWYN_PROJECT='Checkout-Bot'"
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["export"] == expected
        assert payload["project"] == {"selector": "Checkout-Bot", "id": PROJECT_ID}
    else:
        assert result.stdout == expected + "\n"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"keys-env-{output_format}.{_suffix(output_format)}",
    )


def test_keys_env_uses_the_repo_file_value_as_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_key(api_url=STAGING)
    _repo(tmp_path, monkeypatch, "checkout-bot")

    result = runner.invoke(app, ["--api-url", STAGING, "keys", "env"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout == (
        f"export SOLWYN_API_KEY='{KEY}'\n"
        "export SOLWYN_PROJECT='checkout-bot'\n"
        f"export SOLWYN_API_URL='{STAGING}'\n"
    )


def test_keys_env_refuses_a_project_other_than_the_repo_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_key()
    store_key(project_id=OTHER_PROJECT_ID, key=OTHER_KEY, project_name="research")
    _repo(tmp_path, monkeypatch, "checkout-bot")
    monkeypatch.setenv("SOLWYN_PROJECT", "research")

    result = runner.invoke(app, ["--json", "keys", "env"])

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "project_mismatch"
    assert error["message"] == (
        "This repo is set to checkout-bot in .solwyn.env, but SOLWYN_PROJECT says "
        "research. Edit .solwyn.env if the repo should use another project."
    )
    assert KEY not in _rendered(result)
    assert OTHER_KEY not in _rendered(result)


def test_keys_env_without_a_stored_key_says_how_to_store_one() -> None:
    store_key(project_id=OTHER_PROJECT_ID, project_name="research")

    result = runner.invoke(app, ["--json", "keys", "env", "--project", "checkout-bot"])

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "api_key_not_stored"
    assert error["message"] == "No API key for checkout-bot is stored on this computer."
    assert error["hint"] == (
        "Run solwyn keys create --project checkout-bot --store-key."
    )


def test_keys_env_with_an_env_login_never_prints_another_accounts_key(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    # A stored login and a stored key, both for account B; SOLWYN_TOKEN is A.
    auth.store_login("solwyn_cli_" + "e5" * 32, login_meta(account_id=OTHER_ACCOUNT_ID))
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=OTHER_ACCOUNT_ID,
    )
    monkeypatch.setenv("SOLWYN_TOKEN", LOGIN_TOKEN)
    fake_core.on(
        "GET",
        "/api/v1/accounts/me",
        httpx.Response(200, json={"id": ACCOUNT_ID, "display_name": "Acme"}),
    )
    fake_core.on(
        "GET",
        "/api/v1/projects",
        httpx.Response(200, json=[{**PROJECTS[0], "name": "checkout-bot"}]),
    )

    result = runner.invoke(app, ["--json", "keys", "env", "--project", "checkout-bot"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "api_key_not_stored"
    assert OTHER_KEY not in _rendered(result)
    assert [call[:2] for call in fake_core.calls] == [
        ("GET", "/api/v1/accounts/me"),
        ("GET", "/api/v1/projects"),
    ]
    assert fake_core.calls[0][2]["authorization"] == f"Bearer {LOGIN_TOKEN}"


def test_keys_env_fails_closed_when_an_env_login_account_is_unknown(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    store_key()
    monkeypatch.setenv("SOLWYN_TOKEN", LOGIN_TOKEN)
    fake_core.on("GET", "/api/v1/accounts/me", httpx.Response(503))

    result = runner.invoke(app, ["--json", "keys", "env", "--project", "checkout-bot"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "account_unresolved"
    assert error["message"] == (
        "Could not confirm which account your Solwyn login belongs to. Check your "
        "connection and try again."
    )
    # No fallback to the stored names.
    assert KEY not in _rendered(result)


def test_keys_env_refuses_same_named_keys_of_two_accounts_without_a_login() -> None:
    store_key()
    store_key(
        project_id=OTHER_PROJECT_ID,
        key=OTHER_KEY,
        key_id=OTHER_KEY_ID,
        account_id=OTHER_ACCOUNT_ID,
    )

    result = runner.invoke(app, ["--json", "keys", "env", "--project", "checkout-bot"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_ambiguous"
    assert error["message"] == (
        "More than one stored API key is for a project named checkout-bot "
        "(different accounts). Use the proj_ id, or sign in with solwyn login."
    )
    assert KEY not in _rendered(result)
    assert OTHER_KEY not in _rendered(result)

    by_id = runner.invoke(app, ["keys", "env", "--project", OTHER_PROJECT_ID])
    assert by_id.exit_code == 0, by_id.stderr
    assert f"export SOLWYN_API_KEY='{OTHER_KEY}'" in by_id.stdout


def test_keys_env_refuses_a_stored_key_of_another_account_than_the_login(
    fake_core: FakeCore,
) -> None:
    auth.store_login(LOGIN_TOKEN, login_meta())
    store_key(account_id=OTHER_ACCOUNT_ID)

    result = runner.invoke(app, ["--json", "keys", "env", "--project", PROJECT_ID])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "api_key_account_mismatch"
    assert error["message"] == (
        f"The stored API key for {PROJECT_ID} belongs to another account than your "
        f"current login. Run solwyn keys create --project {PROJECT_ID} --store-key."
    )
    assert KEY not in _rendered(result)
    # A proj_ id and a stored login need no read.
    assert fake_core.calls == []


def test_keys_env_never_calls_auth_key(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    store_key()
    auth.store_login(LOGIN_TOKEN, login_meta())
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS))

    result = runner.invoke(app, ["keys", "env", "--project", "checkout-bot"])

    assert result.exit_code == 0, result.stderr
    assert [call[:2] for call in fake_core.calls] == [("GET", "/api/v1/projects")]


def test_keys_env_needs_a_project() -> None:
    store_key()

    result = runner.invoke(app, ["--json", "keys", "env"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_required"
    assert KEY not in _rendered(result)
