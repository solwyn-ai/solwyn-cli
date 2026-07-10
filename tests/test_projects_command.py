"""Project list/show command contract tests."""

import json
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


def test_projects_group_registers_only_list_and_show() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["projects", "--help"])

    assert root.exit_code == 0
    assert "projects" in root.stdout
    assert group.exit_code == 0
    assert "list" in group.stdout
    assert "show" in group.stdout
    assert "create" not in group.stdout


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


@pytest.mark.parametrize("source", ["environment", "keyring"])
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_projects_list_rejects_both_key_sources_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(projects_module, "resolve_credential", lambda: _key(source))
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(app, [*options, "projects", "list"])

    assert result.exit_code == 3
    assert result.stdout == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr,
        GOLDEN_DIR / f"projects-list-key-refusal-{output_format}.{suffix}",
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


def test_project_outputs_and_goldens_exclude_every_synthetic_credential() -> None:
    for golden in GOLDEN_DIR.glob("projects-*"):
        rendered = golden.read_text(encoding="utf-8")
        assert VALID_KEY not in rendered
        assert ACCESS_TOKEN not in rendered
        assert REFRESH_TOKEN not in rendered
        assert "server-must-not-expose-this" not in rendered
