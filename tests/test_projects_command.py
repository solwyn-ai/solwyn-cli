"""Project list/show command contract tests."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from golden.harness import assert_matches_golden
from key_fixtures import FakeCore, store_key
from login_fixtures import login_credential
from typer.main import get_command
from typer.testing import CliRunner

from solwyn_cli import auth, runtime
from solwyn_cli.client import SolwynClient as RealSolwynClient
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.config import Config, ConfigLoadError, ConfigWriteError
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "d4" * 32
CREATED_KEY = "solwyn_sk_" + "ab" * 32
CREATED_PREFIX = CREATED_KEY[:26]
ACCESS_TOKEN = "projects-access-token"
REFRESH_TOKEN = "projects-refresh-token"

PROJECTS_RESPONSE = [
    {
        "id": "proj_be76331b95dfc399cd776d2f",
        "name": "Alpha",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "hard_deny",
        "created_at": "2026-07-09T14:30:00Z",
        "api_key": "server-must-not-expose-this",
    },
    {
        "id": "proj_a295e0bdde1938d1fbfd343e",
        "name": "Beta",
        "budget_limit": 25.5,
        "budget_period": "weekly",
        "budget_mode": "alert_only",
        "created_at": "2026-07-10T08:15:30Z",
    },
]

PROJECT_CREATE_RESPONSE = {
    "id": "proj_21c50805b553b7a40e48394a",
    "name": "Created project",
    "budget_limit": 125.5,
    "budget_period": "weekly",
    "budget_mode": "hard_deny",
    "created_at": "2026-07-10T12:30:00Z",
    "dedicated_keys_only": True,
    "key": CREATED_KEY,
    "key_id": "6f1d0c9e-0000-4000-8000-0000000000c1",
    "key_prefix": CREATED_PREFIX,
    "scope": "full",
    "created_via": "cli",
    "expires_at": "2027-07-10T12:30:00Z",
}


class FakeClient:
    """Minimal closeable command client with queued request results."""

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
def reset_fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    monkeypatch.setattr(projects_module, "SolwynClient", FakeClient, raising=False)
    yield


def _jwt(**_: object) -> auth.LoginCredential:
    return login_credential(ACCESS_TOKEN)


def _key(source: str = "environment", **_: object) -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source=source)


def _queue_projects(payload: object = PROJECTS_RESPONSE) -> None:
    FakeClient.responses = [httpx.Response(200, json=payload)]


def test_projects_group_registers_the_full_lifecycle_surface() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["projects", "--help"])

    assert root.exit_code == 0
    assert "projects" in root.stdout
    assert group.exit_code == 0
    projects_command = get_command(app).commands["projects"]
    assert set(projects_command.commands) == {
        "create",
        "list",
        "show",
        "rename",
        "delete",
    }


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_rename_puts_once_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [
        httpx.Response(200, json={**PROJECTS_RESPONSE[0], "name": "Renamed project"}),
        RuntimeError("must not retry"),
    ]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *options,
            "projects",
            "rename",
            "proj/lifecycle",
            "--name",
            "  Renamed project  ",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        (
            "PUT",
            "/api/v1/projects/proj%2Flifecycle",
            {"json": {"name": "Renamed project"}},
        )
    ]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-rename-{output_format}.{suffix}",
    )
    # Apps that select the project by its old name stop resolving it.
    assert result.stderr == (
        "Apps that set SOLWYN_PROJECT to the old name will fail at their next "
        "start. Update SOLWYN_PROJECT or use the proj_ id.\n"
    )


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_projects_rename_rejects_invalid_names_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: pytest.fail("must validate before auth"),
    )

    result = runner.invoke(
        app,
        ["--json", "projects", "rename", "proj", "--name", name],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_project_update_request"
    assert FakeClient.instances == []


def test_projects_rename_maps_no_fields_to_update_to_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        ApiError(
            status_code=400,
            code="http_error",
            message="No fields to update",
            hint=None,
            detail="No fields to update",
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "rename", "proj", "--name", "Renamed"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["message"] == "No fields to update"


@pytest.mark.parametrize(
    "command", [["rename", "proj", "--name", "Renamed"], ["delete", "proj", "--yes"]]
)
@pytest.mark.parametrize("source", ["environment", "keyring"])
def test_project_mutations_reject_project_keys_before_network(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    source: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: _key(source))
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False, raising=False)

    result = runner.invoke(app, ["--json", "projects", *command])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert "solwyn login" in json.loads(result.stderr)["hint"]
    assert FakeClient.instances == []


def test_project_mutation_role_error_preserves_role_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        ApiError(
            status_code=403,
            code="permission_required",
            message="Request failed with status 403.",
            hint="Your role cannot perform this action; use solwyn whoami to inspect it.",
            detail="permission_required",
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "rename", "proj", "--name", "Renamed"],
    )

    assert result.exit_code == 3
    assert "whoami" in json.loads(result.stderr)["hint"]


def test_projects_delete_requires_yes_before_auth_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: pytest.fail("must require --yes before auth"),
    )

    result = runner.invoke(app, ["--json", "projects", "delete", "proj"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "confirmation_required"
    assert FakeClient.instances == []


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_delete_non_tty_deletes_once_clears_matching_default_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    cleared: list[str] = []
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: credential)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False, raising=False)
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda project_id: cleared.append(project_id) or True,
        raising=False,
    )
    FakeClient.responses = [httpx.Response(204), RuntimeError("must not retry")]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*options, "projects", "delete", "proj/lifecycle", "--yes"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "DELETE",
            "/api/v1/projects/proj%2Flifecycle",
            {},
        )
    ]
    assert cleared == ["proj/lifecycle"]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-delete-{output_format}.{suffix}",
    )
    assert result.stderr == ""


def test_projects_delete_uses_authenticated_transport_without_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handle)

    def client_factory(*, api_url: str, credential: object) -> RealSolwynClient:
        return RealSolwynClient(
            api_url=api_url,
            credential=credential,
            transport=transport,
        )

    monkeypatch.setattr(projects_module, "SolwynClient", client_factory)
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(projects_module, "clear_default_project", lambda _id: False)

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj/transport", "--yes"],
    )

    assert result.exit_code == 0, result.stderr
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/v1/projects/proj/transport"
    assert requests[0].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def _successful_lifecycle_response(
    operation: str,
    request: httpx.Request,
) -> httpx.Response:
    if operation == "rename":
        return httpx.Response(
            200,
            json={**PROJECTS_RESPONSE[0], "name": "Renamed"},
            request=request,
        )
    return httpx.Response(204, request=request)


def _lifecycle_arguments(operation: str) -> list[str]:
    if operation == "rename":
        return ["rename", "proj_708269ae0043898dacb34d81", "--name", "Renamed"]
    return ["delete", "proj_708269ae0043898dacb34d81", "--yes"]


@pytest.mark.parametrize(
    ("operation", "method"),
    [("rename", "PUT"), ("delete", "DELETE")],
)
def test_project_lifecycle_retries_safe_429_rejection_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    method: str,
) -> None:
    requests: list[httpx.Request] = []
    waits: list[float] = []
    cleared: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"detail": "Try again."},
                request=request,
            )
        return _successful_lifecycle_response(operation, request)

    transport = httpx.MockTransport(handle)

    def client_factory(*, api_url: str, credential: object) -> RealSolwynClient:
        return RealSolwynClient(
            api_url=api_url,
            credential=credential,
            transport=transport,
            sleeper=waits.append,
        )

    monkeypatch.setattr(projects_module, "SolwynClient", client_factory)
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda project_id: cleared.append(project_id) or True,
    )

    result = runner.invoke(
        app,
        ["--json", "projects", *_lifecycle_arguments(operation)],
    )

    assert result.exit_code == 0, result.stderr
    assert [request.method for request in requests] == [method, method]
    assert [request.headers["Authorization"] for request in requests] == [
        f"Bearer {ACCESS_TOKEN}",
        f"Bearer {ACCESS_TOKEN}",
    ]
    assert waits == [0.0]
    assert cleared == (
        ["proj_708269ae0043898dacb34d81"] if operation == "delete" else []
    )


@pytest.mark.parametrize(
    ("operation", "method"),
    [("rename", "PUT"), ("delete", "DELETE")],
)
@pytest.mark.parametrize("failure", ["503", "connect_error"])
def test_project_lifecycle_does_not_replay_ambiguous_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    method: str,
    failure: str,
) -> None:
    requests: list[httpx.Request] = []
    cleared: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "connect_error":
            raise httpx.ConnectError("private transport detail", request=request)
        return httpx.Response(
            503,
            json={"detail": "Service unavailable."},
            request=request,
        )

    transport = httpx.MockTransport(handle)

    def client_factory(*, api_url: str, credential: object) -> RealSolwynClient:
        return RealSolwynClient(
            api_url=api_url,
            credential=credential,
            transport=transport,
        )

    monkeypatch.setattr(projects_module, "SolwynClient", client_factory)
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda project_id: cleared.append(project_id) or True,
    )

    result = runner.invoke(
        app,
        ["--json", "projects", *_lifecycle_arguments(operation)],
    )

    assert result.exit_code == 1
    assert [(request.method, request.url.path) for request in requests] == [
        (method, "/api/v1/projects/proj_708269ae0043898dacb34d81")
    ]
    assert cleared == []
    if failure == "connect_error":
        assert "private transport detail" not in result.stderr + repr(result.exception)


def test_projects_delete_tty_fetches_and_requires_exact_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        projects_module.typer,
        "prompt",
        lambda label, **kwargs: prompts.append((label, kwargs)) or "Alpha",
    )
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda _project_id: True,
        raising=False,
    )
    FakeClient.responses = [
        httpx.Response(200, json={**PROJECTS_RESPONSE[0], "id": "proj/lifecycle"}),
        httpx.Response(204),
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj/lifecycle", "--yes"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj%2Flifecycle", {}),
        (
            "DELETE",
            "/api/v1/projects/proj%2Flifecycle",
            {},
        ),
    ]
    assert prompts == [
        (
            "Type the project name 'Alpha' to confirm",
            {"err": True, "hide_input": True},
        )
    ]


def test_projects_delete_tty_name_mismatch_never_deletes_or_clears_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared: list[str] = []
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        projects_module.typer,
        "prompt",
        lambda _label, **_kwargs: "wrong name",
    )
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda project_id: cleared.append(project_id) or True,
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json=PROJECTS_RESPONSE[0])]

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_be76331b95dfc399cd776d2f", "--yes"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "confirmation_failed"
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f", {})
    ]
    assert cleared == []


@pytest.mark.parametrize(
    "secret", ["solwyn_cli_" + "9a" * 32, "solwyn_dc_" + "4f" * 32]
)
@pytest.mark.parametrize(
    ("typed", "deleted"),
    [
        ("proj_be76331b95dfc399cd776d2f", True),
        # The redacted name is never a confirmation: another project may be
        # named exactly that.
        ("name_[REDACTED]_suffix", False),
        ("wrong name", False),
    ],
)
def test_projects_delete_tty_redacted_name_requires_the_project_id(
    monkeypatch: pytest.MonkeyPatch, secret: str, typed: str, deleted: bool
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        projects_module, "clear_default_project", lambda _id: False, raising=False
    )
    FakeClient.responses = [
        httpx.Response(
            200, json={**PROJECTS_RESPONSE[0], "name": f"name_{secret}_suffix"}
        ),
        httpx.Response(204),
    ]

    # The real prompt, so the terminal text is what a TTY would print.
    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_be76331b95dfc399cd776d2f", "--yes"],
        input=f"{typed}\n",
    )

    assert secret not in result.stdout + result.stderr
    assert (
        "Type the project ID 'proj_be76331b95dfc399cd776d2f' to confirm"
        in result.stderr
    )
    assert "name_[REDACTED]_suffix" not in result.stderr
    methods = [method for method, *_ in FakeClient.instances[0].requests]
    if deleted:
        assert result.exit_code == 0, result.stderr
        assert methods == ["GET", "DELETE"]
    else:
        assert result.exit_code == 2
        # Hidden input leaves no newline after the prompt text.
        assert '"code":"confirmation_failed"' in result.stderr
        assert methods == ["GET"]


def test_projects_delete_redaction_collision_never_deletes_the_other_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Project A is literally named "name_[REDACTED]_suffix"; B's real name
    # redacts to the same text. Deleting B by ID while typing A's name must fail.
    project_a_name = "name_[REDACTED]_suffix"
    project_b = {
        **PROJECTS_RESPONSE[1],
        "id": "proj_a295e0bdde1938d1fbfd343e",
        "name": "name_solwyn_cli_" + "9a" * 32 + "_suffix",
    }
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        projects_module, "clear_default_project", lambda _id: False, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=project_b), httpx.Response(204)]

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_a295e0bdde1938d1fbfd343e", "--yes"],
        input=f"{project_a_name}\n",
    )

    assert result.exit_code == 2
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_a295e0bdde1938d1fbfd343e", {})
    ]


def test_projects_delete_tty_unchanged_name_still_requires_the_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        projects_module, "clear_default_project", lambda _id: False, raising=False
    )
    FakeClient.responses = [httpx.Response(200, json=PROJECTS_RESPONSE[0])]

    # The ID is not an alternative when the name is shown unchanged.
    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_be76331b95dfc399cd776d2f", "--yes"],
        input="proj_be76331b95dfc399cd776d2f\n",
    )

    assert "Type the project name 'Alpha' to confirm" in result.stderr
    assert result.exit_code == 2
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f", {})
    ]


def test_projects_delete_rejects_nonempty_204_and_keeps_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared: list[str] = []
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False, raising=False)
    monkeypatch.setattr(
        projects_module,
        "clear_default_project",
        lambda project_id: cleared.append(project_id) or True,
        raising=False,
    )
    FakeClient.responses = [httpx.Response(204, content=b"unexpected")]

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_be76331b95dfc399cd776d2f", "--yes"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    assert cleared == []


@pytest.mark.parametrize("error_type", [ConfigLoadError, ConfigWriteError])
def test_projects_delete_reports_remote_success_when_local_config_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False)

    def fail_to_clear(_project_id: str) -> bool:
        raise error_type("private local detail")

    monkeypatch.setattr(projects_module, "clear_default_project", fail_to_clear)
    FakeClient.responses = [httpx.Response(204)]

    result = runner.invoke(
        app,
        ["--json", "projects", "delete", "proj_be76331b95dfc399cd776d2f", "--yes"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "project_deleted_config_update_failed"
    assert "was archived" in error["message"]
    assert "default_project" in error["message"]
    assert "private local detail" not in result.stderr + repr(result.exception)


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_create_posts_once_as_jwt_and_displays_key_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *options,
            "projects",
            "create",
            "--name",
            "Created project",
            "--budget",
            "125.5",
            "--period",
            "weekly",
            "--mode",
            "hard_deny",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/projects",
            {
                "json": {
                    "name": "Created project",
                    "budget_limit": 125.5,
                    "budget_period": "weekly",
                    "budget_mode": "hard_deny",
                },
                "retry_rate_limit": False,
            },
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    # Table mode adds the SDK hand-off lines under the one-time panel.
    assert rendered.count(CREATED_KEY) == (1 if output_format == "json" else 2)
    if output_format != "json":
        assert (
            f"\nexport SOLWYN_API_KEY='{CREATED_KEY}'\n"
            "export SOLWYN_PROJECT='Created project'\n"
        ) in result.stdout
    assert result.stderr == ""
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["schema"] == "solwyn.projects-create.v1"
        assert payload["key"] == CREATED_KEY
        assert payload["key_id"] == PROJECT_CREATE_RESPONSE["key_id"]
        assert "stored" not in payload
    else:
        assert "One-time API key" in result.stdout
        assert "Created project" in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_create_store_key_never_displays_it(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    stored: list[dict[str, Any]] = []

    def store(**fields: Any) -> auth.ApiKeyCredential:
        stored.append(fields)
        return auth.ApiKeyCredential(
            api_key=fields["key"], source="file", location="/test/credentials.json"
        )

    monkeypatch.setattr(projects_module.auth, "store_api_key", store)
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [
            *options,
            "projects",
            "create",
            "--name",
            "Created project",
            "--store-key",
        ],
    )

    assert result.exit_code == 0, result.stderr
    # Stored under (API, project id) with the login's account (its metadata).
    assert stored == [
        {
            "project_id": PROJECT_CREATE_RESPONSE["id"],
            "key": CREATED_KEY,
            "key_id": PROJECT_CREATE_RESPONSE["key_id"],
            "account_id": "acct_synthetic",
            "project_name": "Created project",
            "api_url": "https://api.solwyn.ai",
        }
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert CREATED_KEY not in rendered
    assert CREATED_PREFIX in result.stdout
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["stored"] is True
        assert "key" not in payload
    else:
        assert "Stored" in result.stdout
        assert "true" in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
@pytest.mark.parametrize(
    ("store_key", "golden_stem"),
    [(False, "display"), (True, "stored")],
)
def test_projects_create_matches_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    store_key: bool,
    golden_stem: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(
        projects_module.auth,
        "store_api_key",
        lambda **fields: auth.ApiKeyCredential(api_key=fields["key"], source="keyring"),
    )
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]
    options = ["--json"] if output_format == "json" else ["--no-color"]
    storage = ["--store-key"] if store_key else []

    result = runner.invoke(
        app,
        [*options, "projects", "create", "--name", "Created project", *storage],
    )

    assert result.exit_code == 0, result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-create-{golden_stem}-{output_format}.{suffix}",
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("source", "expected_hint"),
    [
        (
            "environment",
            "Run solwyn login, then retry projects create.",
        ),
        (
            "keyring",
            "Run solwyn login, then retry projects create.",
        ),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_create_is_jwt_only_with_source_aware_refusal(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_hint: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: _key(source))
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*options, "projects", "create", "--name", "Created project"],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert expected_hint in result.stderr
    assert FakeClient.instances == []


def test_projects_create_requires_auth_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: None)

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project"],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "authentication_required"
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["--name", ""],
        ["--name", "   "],
        ["--name", "Created project", "--budget", "0"],
        ["--name", "Created project", "--budget", "-1"],
        ["--name", "Created project", "--budget", "nan"],
        ["--name", "Created project", "--budget", "inf"],
        ["--name", "x" * 101],
    ],
)
def test_projects_create_validates_request_before_auth_or_network(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: pytest.fail("must validate before auth"),
    )

    result = runner.invoke(app, ["--json", "projects", "create", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_project_create_request"
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["--name", "Created project", "--period", "yearly"],
        ["--name", "Created project", "--mode", "block"],
    ],
)
def test_projects_create_rejects_unknown_enum_options_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: pytest.fail("must parse before auth"),
    )

    result = runner.invoke(app, ["projects", "create", *args])

    assert result.exit_code == 2
    assert FakeClient.instances == []


def test_projects_create_uses_frozen_defaults_and_does_not_select_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(
            201,
            json={
                **PROJECT_CREATE_RESPONSE,
                "budget_limit": 100.0,
                "budget_period": "monthly",
                "budget_mode": "alert_only",
            },
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "  Created project  "],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "name": "Created project",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "alert_only",
    }
    assert runtime.load_config().default_project is None


@pytest.mark.parametrize("scope", ["full", "read_only"])
def test_projects_create_posts_the_chosen_key_scope(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(201, json={**PROJECT_CREATE_RESPONSE, "scope": scope})
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project", "--scope", scope],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "name": "Created project",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "alert_only",
        "scope": scope,
    }
    assert json.loads(result.stdout)["scope"] == scope


def test_projects_create_omits_scope_entirely_when_it_is_not_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API's create scope is not nullable, so an unchosen scope is omitted."""
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project"],
    )

    assert result.exit_code == 0, result.stderr
    assert "scope" not in FakeClient.instances[0].requests[0][2]["json"]


def test_projects_create_rejects_an_unknown_scope_before_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: pytest.fail("must parse before auth"),
    )

    result = runner.invoke(
        app,
        ["projects", "create", "--name", "Created project", "--scope", "admin"],
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []


@pytest.mark.parametrize("status_code", [200, 202, 204])
def test_projects_create_accepts_only_201_without_replay(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(status_code, json=PROJECT_CREATE_RESPONSE)]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in PROJECT_CREATE_RESPONSE.items() if key != "key"},
        {**PROJECT_CREATE_RESPONSE, "key": "sk_proj_not-a-valid-key"},
        {**PROJECT_CREATE_RESPONSE, "key_prefix": "sk_proj_dead"},
        {**PROJECT_CREATE_RESPONSE, "created_at": "2026-07-10T12:30:00"},
        {**PROJECT_CREATE_RESPONSE, "budget_limit": "Infinity"},
        {**PROJECT_CREATE_RESPONSE, "budget_period": "yearly"},
        {**PROJECT_CREATE_RESPONSE, "budget_mode": "block"},
    ],
)
def test_projects_create_rejects_malformed_response_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(201, json=payload)]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            ["--json", "projects", "create", "--name", "Created project"],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    rendered = result.stderr + repr(result.exception) + caplog.text
    assert CREATED_KEY not in rendered
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    "key_prefix",
    [
        CREATED_KEY[:-1],
        "solwyn_sk_" + "cd" * 8,
    ],
)
def test_projects_create_rejects_noncanonical_key_prefix_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    key_prefix: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    stored: list[dict[str, Any]] = []

    def store(**fields: Any) -> auth.ApiKeyCredential:
        stored.append(fields)
        return auth.ApiKeyCredential(
            api_key=fields["key"], source="file", location="/test/credentials.json"
        )

    monkeypatch.setattr(projects_module.auth, "store_api_key", store)
    FakeClient.responses = [
        httpx.Response(
            201,
            json={**PROJECT_CREATE_RESPONSE, "key_prefix": key_prefix},
        )
    ]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            [
                "--json",
                "projects",
                "create",
                "--name",
                "Created project",
                "--store-key",
            ],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    rendered = result.stdout + result.stderr + repr(result.exception) + caplog.text
    assert CREATED_KEY not in rendered
    assert stored == []
    assert len(FakeClient.instances[0].requests) == 1


def test_projects_create_storage_failure_never_falls_back_to_display(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]

    def fail_store(**_fields: Any) -> None:
        raise auth.CredentialStorageError("Keychain unavailable.")

    monkeypatch.setattr(projects_module.auth, "store_api_key", fail_store)

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            [
                "--json",
                "projects",
                "create",
                "--name",
                "Created project",
                "--store-key",
            ],
        )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "credential_storage_failed"
    rendered = result.stderr + repr(result.exception) + caplog.text
    assert CREATED_KEY not in rendered
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(402, "project_limit_reached"), (422, "validation_error")],
)
def test_projects_create_relays_api_errors_without_secret_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    code: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        ApiError(
            status_code=status_code,
            code=code,
            message="Project could not be created.",
            hint=(
                "Manage project limits in the dashboard: /settings/billing"
                if status_code == 402
                else None
            ),
            detail={"code": code, "message": "Project could not be created."},
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == code
    if status_code == 402:
        assert error["hint"] == (
            "Manage project limits in the dashboard: /settings/billing"
        )
    assert CREATED_KEY not in result.stderr + repr(result.exception)
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_list_is_jwt_only_and_matches_goldens(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: credential)
    _queue_projects()
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*options, "projects", "list"])

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.api_url == "https://api.solwyn.ai"
    assert client.credential is credential
    assert client.requests == [("GET", "/api/v1/projects", {})]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-list-jwt-{output_format}.{suffix}",
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("source", "expected_hint"),
    [
        (
            "environment",
            "Run solwyn login, then retry projects list.",
        ),
        (
            "keyring",
            "Run solwyn login, then retry projects list.",
        ),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_list_rejects_both_key_sources_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_hint: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: _key(source))
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*options, "projects", "list"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert expected_hint in result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr,
        GOLDEN_DIR / f"projects-list-{source}-key-refusal-{output_format}.{suffix}",
    )
    assert FakeClient.instances == []


def test_projects_list_requires_auth_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: None)

    result = runner.invoke(app, ["--json", "projects", "list"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "authentication_required"
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    ("credential", "auth_name"),
    [
        (_jwt(), "jwt"),
        (_key("environment"), "key"),
        (_key("keyring"), "key"),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_show_supports_jwt_and_both_key_sources_with_goldens(
    monkeypatch: pytest.MonkeyPatch,
    credential: auth.ResolvedCredential,
    auth_name: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda **_: credential)
    _queue_projects(PROJECTS_RESPONSE[0])
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*options, "--project", "proj_be76331b95dfc399cd776d2f", "projects", "show"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    if isinstance(credential, auth.ApiKeyCredential):
        # The key is bound to the selected id: it is sent as Solwyn-Project.
        assert client.credential == credential.model_copy(
            update={"project": "proj_be76331b95dfc399cd776d2f"}
        )
    else:
        assert client.credential is credential
    assert client.requests == [
        ("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f", {})
    ]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-show-{auth_name}-{output_format}.{suffix}",
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("options", "environment_project", "configured_project", "expected_project"),
    [
        (
            ["--project", "proj_112f3a99b283a4e1788dedd8"],
            "proj_920f8f5815b381ea692e9e7c",
            "proj_dfba7aade0868074c2861c98",
            "proj_112f3a99b283a4e1788dedd8",
        ),
        (
            [],
            "proj_920f8f5815b381ea692e9e7c",
            "proj_dfba7aade0868074c2861c98",
            "proj_920f8f5815b381ea692e9e7c",
        ),
        ([], "", "proj_dfba7aade0868074c2861c98", "proj_dfba7aade0868074c2861c98"),
    ],
)
def test_projects_show_uses_global_project_precedence_for_key_scope(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    environment_project: str,
    configured_project: str,
    expected_project: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _key)
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(default_project=configured_project),
    )
    _queue_projects({**PROJECTS_RESPONSE[0], "id": expected_project})

    result = runner.invoke(
        app,
        ["--json", *options, "projects", "show"],
        env={"SOLWYN_PROJECT_ID": environment_project},
    )

    assert result.exit_code == 0
    assert FakeClient.instances[0].requests == [
        ("GET", f"/api/v1/projects/{expected_project}", {})
    ]
    assert json.loads(result.stdout)["id"] == expected_project


def test_projects_show_requires_a_resolved_project_before_auth_or_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_calls: list[None] = []
    monkeypatch.setattr(runtime, "load_config", lambda: Config(default_project=None))
    monkeypatch.setattr(
        projects_module,
        "resolve_credential",
        lambda **_: credential_calls.append(None),
    )

    result = runner.invoke(
        app,
        ["--json", "projects", "show"],
        env={"SOLWYN_PROJECT_ID": ""},
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "project_required"
    assert "--project" in error["hint"]
    assert "SOLWYN_PROJECT" in error["hint"]
    assert credential_calls == []
    assert FakeClient.instances == []


def test_projects_show_preserves_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _key)
    FakeClient.responses = [
        ApiError(
            status_code=404,
            code="project_not_found",
            message="Project not found.",
            hint=None,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    ]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_5a013c49508291c6816ac388", "projects", "show"],
    )

    # A selector that names no project is a usage error, whoever finds it.
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert len(FakeClient.instances[0].requests) == 1


def test_projects_show_resolves_a_name_through_the_login(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS_RESPONSE))
    _queue_projects(PROJECTS_RESPONSE[1])

    result = runner.invoke(app, ["--json", "--project", " beta ", "projects", "show"])

    assert result.exit_code == 0, result.stderr
    assert [call[:2] for call in fake_core.calls] == [("GET", "/api/v1/projects")]
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_a295e0bdde1938d1fbfd343e", {})
    ]
    assert json.loads(result.stdout)["name"] == "Beta"


def test_projects_show_unknown_name_names_the_account(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=PROJECTS_RESPONSE))

    result = runner.invoke(app, ["--json", "--project", "gamma", "projects", "show"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_not_found"
    assert error["message"] == "No project named gamma in Acme."
    assert FakeClient.instances == []


def test_projects_show_rejects_a_key_scoped_response_for_a_different_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _key)
    _queue_projects({**PROJECTS_RESPONSE[0], "id": "proj_d0941e68da8f38151ff86a61"})

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "projects", "show"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f", {})
    ]


@pytest.mark.parametrize(
    ("subcommand", "payload"),
    [
        (
            "list",
            [
                {
                    key: value
                    for key, value in PROJECTS_RESPONSE[0].items()
                    if key != "created_at"
                }
            ],
        ),
        (
            "show",
            {
                key: value
                for key, value in PROJECTS_RESPONSE[0].items()
                if key != "budget_limit"
            },
        ),
        ("show", {**PROJECTS_RESPONSE[0], "created_at": "not-a-date-time"}),
    ],
)
def test_projects_rejects_invalid_responses_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    subcommand: str,
    payload: object,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    _queue_projects(payload)
    project_options = (
        ["--project", "proj_be76331b95dfc399cd776d2f"] if subcommand == "show" else []
    )

    result = runner.invoke(
        app,
        ["--json", *project_options, "projects", subcommand],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    rendered = result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


def test_goldens_limit_created_key_to_authorized_one_time_outputs() -> None:
    authorized = {
        "projects-create-display-table.txt",
        "projects-create-display-json.json",
    }
    for golden in GOLDEN_DIR.glob("*"):
        if not golden.is_file():
            continue
        rendered = golden.read_text(encoding="utf-8")
        # The display table shows the key in its panel and in the export line.
        expected_occurrences = (
            {"projects-create-display-table.txt": 2}.get(golden.name, 1)
            if golden.name in authorized
            else 0
        )
        assert rendered.count(CREATED_KEY) == expected_occurrences, golden.name
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered
        assert REFRESH_TOKEN not in rendered
        assert "server-must-not-expose-this" not in rendered


# --- Stored API keys (spec 7.1, 7.5) ----------------------------------------


def _env_login(**_: object) -> auth.LoginCredential:
    """A SOLWYN_TOKEN login: no local metadata, so no known account."""
    from pydantic import SecretStr

    return auth.LoginCredential.model_construct(
        token=SecretStr(ACCESS_TOKEN), meta=None, source="environment", location=None
    )


def test_projects_create_store_key_stores_under_the_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project", "--store-key"],
    )

    assert result.exit_code == 0, result.stderr
    stored = auth.stored_api_keys()
    entry = stored[PROJECT_CREATE_RESPONSE["id"]]
    assert entry.key.get_secret_value() == CREATED_KEY
    assert entry.key_id == PROJECT_CREATE_RESPONSE["key_id"]
    assert entry.account_id == "acct_synthetic"
    assert entry.project_name == "Created project"
    assert CREATED_KEY not in result.stdout + result.stderr


def test_projects_create_store_key_with_env_login_asks_for_the_account_first(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(projects_module, "resolve_credential", _env_login)
    fake_core.on(
        "GET",
        "/api/v1/accounts/me",
        httpx.Response(200, json={"id": "acct_env", "display_name": "Env Co"}),
    )
    FakeClient.responses = [httpx.Response(201, json=PROJECT_CREATE_RESPONSE)]

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project", "--store-key"],
    )

    assert result.exit_code == 0, result.stderr
    assert [call[:2] for call in fake_core.calls] == [("GET", "/api/v1/accounts/me")]
    assert (
        auth.stored_api_keys()[PROJECT_CREATE_RESPONSE["id"]].account_id == "acct_env"
    )


def test_projects_create_store_key_fails_closed_when_the_account_is_unknown(
    monkeypatch: pytest.MonkeyPatch, fake_core: FakeCore
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(projects_module, "resolve_credential", _env_login)
    fake_core.on("GET", "/api/v1/accounts/me", httpx.Response(503))

    result = runner.invoke(
        app,
        ["--json", "projects", "create", "--name", "Created project", "--store-key"],
    )

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "account_unresolved"
    # Nothing is created before the account is known.
    assert FakeClient.instances == []
    assert auth.stored_api_keys() == {}


def test_projects_delete_deletes_the_stored_key_for_that_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_CREDENTIAL_STORE", "file")
    deleted = "proj_" + "8a" * 12
    kept = "proj_" + "9b" * 12
    store_key(project_id=deleted)
    store_key(project_id=kept, project_name="other")
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    monkeypatch.setattr(projects_module, "_stdin_is_tty", lambda: False)
    FakeClient.responses = [httpx.Response(204)]

    result = runner.invoke(app, ["--json", "projects", "delete", deleted, "--yes"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["removed_stored_key"] is True
    assert set(auth.stored_api_keys()) == {kept}
