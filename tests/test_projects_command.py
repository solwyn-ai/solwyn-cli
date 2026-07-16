"""Project list/show command contract tests."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth, runtime
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.config import Config
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "d4" * 32
CREATED_KEY = "sk_proj_" + "ab" * 32
CREATED_PREFIX = CREATED_KEY[:24]
ACCESS_TOKEN = "projects-access-token"
REFRESH_TOKEN = "projects-refresh-token"

PROJECTS_RESPONSE = [
    {
        "id": "proj_alpha",
        "name": "Alpha",
        "budget_limit": 100.0,
        "budget_period": "monthly",
        "budget_mode": "hard_deny",
        "created_at": "2026-07-09T14:30:00Z",
        "api_key": "server-must-not-expose-this",
    },
    {
        "id": "proj_beta",
        "name": "Beta",
        "budget_limit": 25.5,
        "budget_period": "weekly",
        "budget_mode": "alert_only",
        "created_at": "2026-07-10T08:15:30Z",
    },
]

PROJECT_CREATE_RESPONSE = {
    "id": "proj_created",
    "name": "Created project",
    "budget_limit": 125.5,
    "budget_period": "weekly",
    "budget_mode": "hard_deny",
    "created_at": "2026-07-10T12:30:00Z",
    "key": CREATED_KEY,
    "key_prefix": CREATED_PREFIX,
    "scope": "full",
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


def _jwt() -> auth.JwtCredential:
    return auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )


def _key(source: str = "environment") -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _queue_projects(payload: object = PROJECTS_RESPONSE) -> None:
    FakeClient.responses = [httpx.Response(200, json=payload)]


def test_projects_group_registers_create_list_and_show_only() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["projects", "--help"])

    assert root.exit_code == 0
    assert "projects" in root.stdout
    assert group.exit_code == 0
    assert "create" in group.stdout
    assert "list" in group.stdout
    assert "show" in group.stdout
    assert "keys" not in group.stdout
    assert "delete" not in group.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_create_posts_once_as_jwt_and_displays_key_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: credential)
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
                "retry_auth": False,
            },
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert rendered.count(CREATED_KEY) == 1
    assert result.stderr == ""
    if output_format == "json":
        payload = json.loads(result.stdout)
        assert payload["schema"] == "solwyn.projects-create.v1"
        assert payload["key"] == CREATED_KEY
        assert "stored" not in payload
    else:
        assert "One-time project key" in result.stdout
        assert "Created project" in result.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_create_store_key_never_displays_it(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    stored: list[str] = []
    monkeypatch.setattr(projects_module, "store_project_key", stored.append)
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
    assert stored == [CREATED_KEY]
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert CREATED_KEY not in rendered
    assert CREATED_PREFIX in result.stdout
    assert result.stderr == ""
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
    monkeypatch.setattr(projects_module, "store_project_key", lambda _key: None)
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
            "Unset SOLWYN_API_KEY, run solwyn login, then retry projects create.",
        ),
        (
            "keyring",
            "Run solwyn logout, then solwyn login, and retry projects create.",
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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: _key(source))
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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: None)

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
        lambda: pytest.fail("must validate before auth"),
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
        lambda: pytest.fail("must parse before auth"),
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
        lambda: pytest.fail("must parse before auth"),
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
        "sk_proj_" + "cd" * 8,
    ],
)
def test_projects_create_rejects_noncanonical_key_prefix_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    key_prefix: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _jwt)
    stored: list[str] = []
    monkeypatch.setattr(projects_module, "store_project_key", stored.append)
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

    def fail_store(_key: str) -> None:
        raise auth.CredentialStorageError("Keychain unavailable.")

    monkeypatch.setattr(projects_module, "store_project_key", fail_store)

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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: credential)
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
            "Unset SOLWYN_API_KEY, run solwyn login, then retry projects list.",
        ),
        (
            "keyring",
            "Run solwyn logout, then solwyn login, and retry projects list.",
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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: _key(source))
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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: None)

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
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: credential)
    _queue_projects(PROJECTS_RESPONSE[0])
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*options, "--project", "proj_alpha", "projects", "show"],
    )

    assert result.exit_code == 0
    client = FakeClient.instances[0]
    assert client.credential is credential
    assert client.requests == [("GET", "/api/v1/projects/proj_alpha", {})]
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout,
        GOLDEN_DIR / f"projects-show-{auth_name}-{output_format}.{suffix}",
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("options", "environment_project", "configured_project", "expected_project"),
    [
        (["--project", "proj_flag"], "proj_env", "proj_config", "proj_flag"),
        ([], "proj_env", "proj_config", "proj_env"),
        ([], "", "proj_config", "proj_config"),
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
        lambda: credential_calls.append(None),
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
    assert "SOLWYN_PROJECT_ID" in error["hint"]
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
        ["--json", "--project", "proj_missing", "projects", "show"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert len(FakeClient.instances[0].requests) == 1


def test_projects_show_rejects_a_key_scoped_response_for_a_different_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", _key)
    _queue_projects({**PROJECTS_RESPONSE[0], "id": "proj_other"})

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "projects", "show"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_projects_response"
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj_alpha", {})
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
    project_options = ["--project", "proj_alpha"] if subcommand == "show" else []

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
        expected_occurrences = 1 if golden.name in authorized else 0
        assert rendered.count(CREATED_KEY) == expected_occurrences, golden.name
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered
        assert REFRESH_TOKEN not in rendered
        assert "server-must-not-expose-this" not in rendered
