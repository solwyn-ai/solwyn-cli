"""End-to-end contract tests for ``solwyn init``."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli import config as config_module
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.commands import init as init_module
from solwyn_cli.commands import login as login_module
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "ab" * 32
CREATED_KEY = "sk_proj_" + "cd" * 32
ACCESS_TOKEN = "init-access-token"
REFRESH_TOKEN = "init-refresh-token"
PROJECT = {
    "id": "proj_alpha",
    "name": "Alpha",
    "budget_limit": 100.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "created_at": "2026-07-10T12:00:00Z",
}
BUDGET = {
    "project_id": "proj_alpha",
    "budget_limit": 100.0,
    "current_usage": 25.0,
    "remaining_budget": 75.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "period_date": "2026-07",
    "utilization_pct": 25.0,
    "thresholds": [],
    "constraints": {
        "allowed_modes": ["alert_only", "hard_deny"],
        "allowed_periods": ["daily", "weekly", "monthly"],
        "sub_period_enabled": True,
        "max_thresholds": 50,
    },
}


class FakeClient:
    """Closeable client with ordered responses and request capture."""

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

    def request_unauthenticated(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        return self.request(method, path, **kwargs)


@pytest.fixture(autouse=True)
def isolated_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    config_path = tmp_path / "config" / "config.toml"
    monkeypatch.chdir(repository)
    monkeypatch.setattr(config_module, "get_config_path", lambda: config_path)
    monkeypatch.setattr(init_module, "get_config_path", lambda: config_path)
    monkeypatch.setattr(login_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(projects_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(budget_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(init_module, "SolwynClient", FakeClient)
    FakeClient.instances = []
    FakeClient.responses = []
    yield repository


def _key() -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")


def _jwt() -> auth.JwtCredential:
    return auth.JwtCredential(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    )


def _invoke(*arguments: str) -> Any:
    return runner.invoke(app, ["--json", *arguments, "init"])


def test_init_is_registered_without_secret_or_agent_options() -> None:
    root = runner.invoke(app, ["--help"])
    command = runner.invoke(app, ["init", "--help"])

    assert root.exit_code == 0
    assert "init" in root.stdout
    assert command.exit_code == 0
    assert "--api-key" not in command.stdout
    assert "--password" not in command.stdout
    assert "--agents" not in command.stdout


def test_non_tty_without_credentials_exits_auth_with_api_key_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", lambda: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: False)

    result = _invoke()

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "interactive_auth_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert FakeClient.instances == []


def test_tty_can_paste_hidden_key_and_it_is_stored_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: list[tuple[str, bool]] = []
    values = iter(["key", VALID_KEY])

    def prompt(label: str, **kwargs: object) -> str:
        prompt_calls.append((label, bool(kwargs.get("hide_input", False))))
        return next(values)

    monkeypatch.setattr(init_module, "resolve_credential", lambda: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module.typer, "prompt", prompt)
    monkeypatch.setattr(init_module, "store_project_key", lambda _value: _key())
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    assert prompt_calls == [
        ("Authentication method (key/login)", False),
        ("Project key", True),
    ]
    assert VALID_KEY not in result.stdout + result.stderr + repr(result.exception)


def test_tty_can_login_with_hidden_password_and_stored_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: list[tuple[str, bool]] = []
    values = iter(["login", "person@example.com", "password-value"])

    def prompt(label: str, **kwargs: object) -> str:
        prompt_calls.append((label, bool(kwargs.get("hide_input", False))))
        return next(values)

    monkeypatch.setattr(init_module, "resolve_credential", lambda: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module.typer, "prompt", prompt)
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        login_module,
        "store_jwt_pair",
        lambda access, refresh: stored.append((access, refresh)) or _jwt(),
    )
    FakeClient.responses = [
        httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "token_type": "bearer",
            },
        ),
        httpx.Response(200, json=[PROJECT]),
        httpx.Response(200, json=BUDGET),
    ]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    assert prompt_calls == [
        ("Authentication method (key/login)", False),
        ("Email", False),
        ("Password", True),
    ]
    assert stored == [(ACCESS_TOKEN, REFRESH_TOKEN)]
    requests = [
        request for client in FakeClient.instances for request in client.requests
    ]
    assert requests[0][0:2] == ("POST", "/api/v1/auth/login")
    rendered = result.stdout + result.stderr + repr(result.exception)
    for secret in ("password-value", ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in rendered


def test_init_composes_shared_login_project_and_budget_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    prompt_values = iter(["login", "person@example.com", "password-value"])
    credential = _jwt()

    monkeypatch.setattr(init_module, "resolve_credential", lambda: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        init_module.typer, "prompt", lambda *args, **kwargs: next(prompt_values)
    )
    monkeypatch.setattr(
        init_module,
        "authenticate_with_password",
        lambda **kwargs: calls.append(("login", kwargs)) or credential,
        raising=False,
    )
    monkeypatch.setattr(
        init_module,
        "fetch_projects",
        lambda **kwargs: (
            calls.append(("projects", kwargs))
            or projects_module._PROJECTS.validate_python([PROJECT])
        ),
        raising=False,
    )
    monkeypatch.setattr(
        init_module,
        "fetch_budget_status",
        lambda **kwargs: (
            calls.append(("budget", kwargs))
            or budget_module._BudgetStatusData.model_validate(BUDGET)
        ),
        raising=False,
    )

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    assert [name for name, _payload in calls] == ["login", "projects", "budget"]
    assert calls[0][1] == {
        "api_url": "https://api.solwyn.ai",
        "email": "person@example.com",
        "password": "password-value",
    }
    assert len(FakeClient.instances) == 1
    assert FakeClient.instances[0].requests == []


def test_project_key_requires_resolved_project_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)

    result = _invoke()

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--project" in json.loads(result.stderr)["hint"]
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import openai\n", ["openai"]),
        ("from anthropic import Anthropic\n", ["anthropic"]),
        ("from google import genai\n", ["google-genai"]),
        ("import google.genai as genai\n", ["google-genai"]),
        ("import boto3\n", ["boto3"]),
        (
            "import boto3\nimport openai\nfrom anthropic import Anthropic\n",
            ["openai", "anthropic", "boto3"],
        ),
    ],
)
def test_provider_detection_drives_placeholder_only_snippets(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected: list[str],
) -> None:
    source_path = isolated_init / "app.py"
    source_path.write_text(source, encoding="utf-8")
    before = source_path.read_bytes()
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solwyn.init.v1"
    assert payload["detected_providers"] == expected
    rendered_snippets = json.dumps(payload["snippets"])
    assert "from solwyn import Solwyn" in rendered_snippets
    assert VALID_KEY not in rendered_snippets
    assert "<" in rendered_snippets and ">" in rendered_snippets
    assert source_path.read_bytes() == before
    assert [
        request for client in FakeClient.instances for request in client.requests
    ] == [("GET", "/api/v1/projects/proj_alpha/budget", {})]
    assert "/budgets/check" not in repr(FakeClient.instances[0].requests)


def test_init_leaves_entire_source_tree_byte_for_byte_unchanged(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "app.py": b"import openai\n",
        "package/client.py": b"from anthropic import Anthropic\n",
        "package/broken.py": b"import (\n",
        "package/blob.py": b"\x00\xffprovider bytes",
        "README.md": b"provider documentation\n",
    }
    for relative_path, content in files.items():
        path = isolated_init / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = {
        path.relative_to(isolated_init): path.read_bytes()
        for path in isolated_init.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    after = {
        path.relative_to(isolated_init): path.read_bytes()
        for path in isolated_init.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("source", "expected_fragment"),
    [
        ("import openai\n", "openai.OpenAI"),
        ("from openai import OpenAI\n", "Solwyn(OpenAI("),
        ("import anthropic\n", "anthropic.Anthropic"),
        ("from anthropic import Anthropic\n", "Solwyn(Anthropic("),
    ],
)
def test_snippet_matches_detected_client_import_style(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_fragment: str,
) -> None:
    (isolated_init / "app.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    assert expected_fragment in json.loads(result.stdout)["snippets"][0]["code"]


def test_no_provider_uses_deterministic_generic_snippet(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "README.md").write_text("no Python imports", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["detected_providers"] == []
    assert payload["snippets"][0]["provider"] == "generic"
    assert "<YOUR_PROVIDER_CLIENT>" in payload["snippets"][0]["code"]


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_init_matches_stable_goldens(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    (isolated_init / "app.py").write_text("import openai\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app,
        [*options, "--project", "proj_alpha", "init"],
    )

    assert result.exit_code == 0, result.stderr
    normalized = result.stdout.replace(
        str(init_module.get_config_path()), "<CONFIG_PATH>"
    )
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        normalized,
        GOLDEN_DIR / f"init-{output_format}.{suffix}",
    )
    assert result.stderr == ""
    assert "\x1b" not in result.stdout


def test_detection_ignores_generated_invalid_binary_and_non_python_files(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    (isolated_init / "broken.py").write_text("import (\n", encoding="utf-8")
    (isolated_init / "secrets.txt").write_text("import anthropic\n", encoding="utf-8")
    (isolated_init / "binary.py").write_bytes(b"\x00import boto3\n")
    ignored = isolated_init / ".git"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("import boto3\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_does_not_follow_source_symlinks_outside_repository(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    outside = isolated_init.parent / "outside.py"
    outside.write_text("import anthropic\n", encoding="utf-8")
    (isolated_init / "escaped.py").symlink_to(outside)
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_prunes_arbitrarily_named_virtualenv_roots(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    environment = isolated_init / "env"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (environment / "provider.py").write_text("import anthropic\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_environment_values_are_never_read_or_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GuardedEnvironment(dict[str, str]):
        def get(self, key: str, default: str | None = None) -> str | None:
            if key in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}:
                raise AssertionError("provider credentials must not be read")
            return super().get(key, default)

    monkeypatch.setattr(init_module.os, "environ", GuardedEnvironment())
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr


def test_existing_config_is_preserved_and_nondefault_api_url_is_persisted(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = init_module.get_config_path()
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'api_url = "https://staging.example.test"\n'
        'default_project = "proj_old"\n'
        'format = "json"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    saved = config_module.load_config()
    assert saved.default_project == "proj_alpha"
    assert saved.api_url == "https://staging.example.test"
    assert saved.format == "json"
    assert "api_key" not in config_path.read_text(encoding="utf-8")
    assert "provider" not in config_path.read_text(encoding="utf-8")


def test_default_api_url_is_not_written_to_new_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 0, result.stderr
    content = init_module.get_config_path().read_text(encoding="utf-8")
    assert 'default_project = "proj_alpha"' in content
    assert "api_url" not in content
    assert "api_key" not in content


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (config_module.ConfigLoadError("sensitive load detail"), "config_load_failed"),
        (
            config_module.ConfigWriteError("sensitive write detail"),
            "config_write_failed",
        ),
    ],
)
def test_init_config_failures_are_structured_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    target = (
        "load_config"
        if isinstance(failure, config_module.ConfigLoadError)
        else "save_config"
    )
    monkeypatch.setattr(
        init_module,
        target,
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == expected_code
    assert "sensitive" not in result.stderr + repr(result.exception)
    assert FakeClient.instances == []


def test_init_invalid_existing_config_is_structured_before_context_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = init_module.get_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "config_load_failed"
    assert "sensitive" not in result.stderr + repr(result.exception)
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "options",
    [
        ["--format", "bogus"],
        ["--json", "--format", "table"],
    ],
)
def test_invalid_output_intent_wins_over_malformed_config_as_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
) -> None:
    path = init_module.get_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)

    result = runner.invoke(app, [*options, "init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Invalid or conflicting output options" in result.stderr
    assert "sensitive" not in result.stderr + repr(result.exception)
    assert "Traceback" not in result.stderr
    assert FakeClient.instances == []


def test_jwt_single_project_is_selected_and_validated_before_final_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(init_module, "resolve_credential", lambda: credential)
    FakeClient.responses = [
        httpx.Response(200, json=[PROJECT]),
        httpx.Response(200, json=BUDGET),
    ]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert [
        request for client in FakeClient.instances for request in client.requests
    ] == [
        ("GET", "/api/v1/projects", {}),
        ("GET", "/api/v1/projects/proj_alpha/budget", {}),
    ]


def test_non_tty_jwt_project_ambiguity_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: False)
    FakeClient.responses = [
        httpx.Response(200, json=[PROJECT, {**PROJECT, "id": "proj_beta"}])
    ]

    result = _invoke()

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--project" in json.loads(result.stderr)["hint"]


def test_explicit_jwt_project_is_validated_against_project_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(200, json=[PROJECT])]

    result = _invoke("--project", "proj_missing")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert [
        request for client in FakeClient.instances for request in client.requests
    ] == [("GET", "/api/v1/projects", {})]


def test_tty_jwt_multiple_projects_can_be_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        init_module.typer, "prompt", lambda *args, **kwargs: "proj_beta"
    )
    FakeClient.responses = [
        httpx.Response(200, json=[PROJECT, {**PROJECT, "id": "proj_beta"}]),
        httpx.Response(200, json={**BUDGET, "project_id": "proj_beta"}),
    ]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["project_id"] == "proj_beta"


def test_tty_no_projects_creates_and_stores_key_without_displaying_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    prompt_values = iter(["Created project", 100.0])
    monkeypatch.setattr(
        init_module.typer, "prompt", lambda *args, **kwargs: next(prompt_values)
    )
    stored: list[str] = []
    monkeypatch.setattr(init_module, "store_project_key", stored.append)
    created = {
        **PROJECT,
        "name": "Created project",
        "key": CREATED_KEY,
        "key_prefix": CREATED_KEY[:24],
    }
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=created),
        httpx.Response(200, json=BUDGET),
    ]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    assert stored == [CREATED_KEY]
    assert CREATED_KEY not in result.stdout + result.stderr + repr(result.exception)
    project_requests = FakeClient.instances[0].requests
    assert project_requests[0] == ("GET", "/api/v1/projects", {})
    assert project_requests[1][0:2] == ("POST", "/api/v1/projects")
    assert project_requests[1][2]["retry_auth"] is False
    assert project_requests[1][2]["retry_rate_limit"] is False


def test_created_key_storage_failure_never_leaks_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    prompt_values = iter(["Created project", 100.0])
    monkeypatch.setattr(
        init_module.typer, "prompt", lambda *args, **kwargs: next(prompt_values)
    )
    monkeypatch.setattr(
        init_module,
        "store_project_key",
        lambda _value: (_ for _ in ()).throw(
            auth.CredentialStorageError("no keychain")
        ),
    )
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(
            201,
            json={
                **PROJECT,
                "key": CREATED_KEY,
                "key_prefix": CREATED_KEY[:24],
            },
        ),
    ]

    result = _invoke()

    assert result.exit_code == 3
    assert result.stdout == ""
    assert CREATED_KEY not in result.stderr + repr(result.exception)


def test_final_budget_response_must_match_project_and_be_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [
        httpx.Response(200, json={**BUDGET, "project_id": "proj_other"})
    ]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"


def test_live_budget_failure_is_safe_and_contains_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json={"project_id": "proj_alpha"})]

    result = _invoke("--project", "proj_alpha")

    assert result.exit_code == 1
    assert result.stdout == ""
    rendered = result.stderr + repr(result.exception)
    assert VALID_KEY not in rendered
