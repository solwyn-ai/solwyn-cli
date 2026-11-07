"""End-to-end contract tests for ``solwyn init``."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from click import unstyle
from golden.harness import assert_matches_golden
from key_fixtures import OTHER_PROJECT_ID, store_key
from login_fixtures import login_credential
from typer.testing import CliRunner

from solwyn_cli import artifacts as agent_artifacts
from solwyn_cli import auth
from solwyn_cli import config as config_module
from solwyn_cli.commands import budget as budget_module
from solwyn_cli.commands import init as init_module
from solwyn_cli.commands import login as login_module
from solwyn_cli.commands import projects as projects_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "solwyn_sk_" + "ab" * 32
CREATED_KEY = "solwyn_sk_" + "cd" * 32
ACCESS_TOKEN = "init-access-token"
REFRESH_TOKEN = "init-refresh-token"
PROJECT = {
    "id": "proj_be76331b95dfc399cd776d2f",
    "name": "Alpha",
    "budget_limit": 100.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "created_at": "2026-07-10T12:00:00Z",
}
BUDGET = {
    "project_id": "proj_be76331b95dfc399cd776d2f",
    "budget_limit": 100.0,
    "current_usage": 25.0,
    "remaining_budget": 75.0,
    "budget_period": "monthly",
    "budget_mode": "hard_deny",
    "period_date": "2026-07",
    "outage_overspend_ceiling_usd": 0.0,
    "utilization_pct": 25.0,
    "thresholds": [],
    "constraints": {
        "allowed_modes": ["alert_only", "hard_deny"],
        "allowed_periods": ["daily", "weekly", "monthly"],
        "sub_period_enabled": True,
        "max_thresholds": 50,
    },
}


# What a project create response adds for its first key (spec 3.5).
CREATED_KEY_FIELDS = {
    "dedicated_keys_only": False,
    "key_id": "6f1d0c9e-0000-4000-8000-0000000000d1",
    "created_via": "cli",
    "expires_at": "2027-07-10T12:00:00Z",
}


def _store_fields(stored: list[dict[str, Any]]) -> Any:
    """A stand-in for auth.store_api_key that records the stored fields."""

    def store(**fields: Any) -> auth.ApiKeyCredential:
        stored.append(fields)
        return auth.ApiKeyCredential(
            api_key=fields["key"], source="keyring", project=fields["project_id"]
        )

    return store


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

    @property
    def current_credential(self) -> object:
        return self.credential

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
    monkeypatch.setattr(login_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(projects_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(budget_module, "SolwynClient", FakeClient)
    monkeypatch.setattr(init_module, "SolwynClient", FakeClient)
    FakeClient.instances = []
    FakeClient.responses = []
    yield repository


def _key(**_: object) -> auth.ApiKeyCredential:
    return auth.ApiKeyCredential(api_key=VALID_KEY, source="environment")


def _jwt(**_: object) -> auth.LoginCredential:
    return login_credential(ACCESS_TOKEN)


def _invoke(*arguments: str) -> Any:
    return runner.invoke(app, ["--json", *arguments, "init"])


def test_init_is_registered_with_agents_but_without_secret_options() -> None:
    root = runner.invoke(app, ["--help"])
    command = runner.invoke(app, ["init", "--help"])

    assert root.exit_code == 0
    assert "init" in root.stdout
    assert command.exit_code == 0
    assert "--api-key" not in command.stdout
    assert "--password" not in command.stdout
    assert "--agents" in unstyle(command.stdout)


def test_init_agents_writes_artifacts_after_successful_safe_onboarding(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "init", "--agents"],
    )

    assert result.exit_code == 0, result.stderr
    assert (isolated_init / ".claude/skills/solwyn/SKILL.md").is_file()
    assert (isolated_init / ".cursor/rules/solwyn.mdc").is_file()
    assert (isolated_init / "AGENTS.md").is_file()


def test_init_without_agents_preserves_existing_source_tree_behavior(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "app.py").write_text("import openai\n", encoding="utf-8")
    before = {
        path.relative_to(isolated_init): path.read_bytes()
        for path in isolated_init.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    after = {
        path.relative_to(isolated_init): path.read_bytes()
        for path in isolated_init.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_init_agents_reports_artifact_write_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    monkeypatch.setattr(
        init_module,
        "write_agent_artifacts",
        lambda _root: (_ for _ in ()).throw(OSError("read-only fixture")),
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "init", "--agents"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "agent_artifact_write_failed"
    assert "read-only fixture" not in result.stderr
    assert "Traceback" not in result.stderr


def test_init_agents_reports_retained_backup_for_manual_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery_path = Path(".claude/skills/solwyn/.solwyn-agent-backup-SKILL.md-recovery")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    monkeypatch.setattr(
        init_module,
        "write_agent_artifacts",
        lambda _root: (_ for _ in ()).throw(
            agent_artifacts.ArtifactRecoveryError((recovery_path,))
        ),
        raising=False,
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "init", "--agents"],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "agent_artifact_recovery_required"
    assert "manual recovery" in error["hint"].lower()
    assert str(recovery_path) in error["hint"]
    assert "Traceback" not in result.stderr


INIT_CONTINUE = "solwyn login --continue, then run solwyn init again"


def test_non_tty_without_credentials_runs_the_browser_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logins: list[dict[str, object]] = []
    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        init_module,
        "device_login",
        lambda context, **kwargs: logins.append(kwargs) or _jwt(),
    )
    monkeypatch.setattr(
        init_module.typer,
        "prompt",
        lambda *args, **kwargs: pytest.fail("non-TTY init must not prompt"),
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert logins == [{"continue_command": INIT_CONTINUE}]
    assert json.loads(result.stdout)["project_id"] == "proj_be76331b95dfc399cd776d2f"


def test_tty_can_paste_hidden_key_and_it_is_stored_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: list[tuple[str, bool]] = []
    values = iter(["key", VALID_KEY])

    def prompt(label: str, **kwargs: object) -> str:
        prompt_calls.append((label, bool(kwargs.get("hide_input", False))))
        return next(values)

    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module.typer, "prompt", prompt)
    stored: list[str] = []
    # The pasted key is checked with /auth/key and stored by login's helper.
    monkeypatch.setattr(
        init_module,
        "store_supplied_key",
        lambda context, value, store: (
            stored.append(value)
            or _key().model_copy(
                update={"project": "proj_be76331b95dfc399cd776d2f", "source": "keyring"}
            )
        ),
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert prompt_calls == [
        ("Authentication method (login/key)", False),
        ("API key", True),
    ]
    assert stored == [VALID_KEY]
    assert VALID_KEY not in result.stdout + result.stderr + repr(result.exception)


def test_tty_login_is_the_default_choice_and_runs_the_device_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_calls: list[tuple[str, object]] = []
    logins: list[dict[str, object]] = []

    def prompt(label: str, **kwargs: object) -> object:
        prompt_calls.append((label, kwargs.get("default")))
        return kwargs["default"]

    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module.typer, "prompt", prompt)
    monkeypatch.setattr(
        init_module,
        "device_login",
        lambda context, **kwargs: logins.append(kwargs) or _jwt(),
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert prompt_calls == [("Authentication method (login/key)", "login")]
    assert logins == [{"continue_command": INIT_CONTINUE}]
    requests = [
        request for client in FakeClient.instances for request in client.requests
    ]
    # A proj_ id needs no lookup: only the final budget read.
    assert [request[0:2] for request in requests] == [
        ("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f/budget"),
    ]
    rendered = result.stdout + result.stderr + repr(result.exception)
    assert ACCESS_TOKEN not in rendered


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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solwyn.init.v2"
    assert payload["detected_providers"] == expected
    rendered_snippets = json.dumps(payload["snippets"])
    assert "from solwyn import Solwyn" in rendered_snippets
    assert VALID_KEY not in rendered_snippets
    assert "api_key=" not in rendered_snippets.replace("api_key=os.environ[", "")
    assert source_path.read_bytes() == before
    assert [
        request for client in FakeClient.instances for request in client.requests
    ] == [("GET", "/api/v1/projects/proj_be76331b95dfc399cd776d2f/budget", {})]
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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert expected_fragment in json.loads(result.stdout)["snippets"][0]["code"]


@pytest.mark.parametrize(
    ("source", "variable"),
    [
        ("import openai\n", "OPENAI_API_KEY"),
        ("from openai import OpenAI\n", "OPENAI_API_KEY"),
        ("import anthropic\n", "ANTHROPIC_API_KEY"),
        ("from anthropic import Anthropic\n", "ANTHROPIC_API_KEY"),
        ("from google import genai\n", "GOOGLE_API_KEY"),
    ],
)
def test_snippets_read_provider_keys_from_the_environment(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    variable: str,
) -> None:
    """Never invite pasting a provider key into code."""
    (isolated_init / "app.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    code = json.loads(result.stdout)["snippets"][0]["code"]
    assert code.startswith("import os")
    assert f'api_key=os.environ["{variable}"]' in code
    assert 'api_key="' not in code


def test_no_provider_uses_deterministic_generic_snippet(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "README.md").write_text("no Python imports", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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
        [*options, "--project", "proj_be76331b95dfc399cd776d2f", "init"],
    )

    assert result.exit_code == 0, result.stderr
    normalized = result.stdout
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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_uses_git_root_from_nested_invocation(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / ".git").mkdir()
    (isolated_init / "app.py").write_text("import openai\n", encoding="utf-8")
    nested = isolated_init / "src" / "package"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_from_fallback_root_prunes_nested_repositories(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    unrelated = isolated_init / "unrelated-repo"
    (unrelated / ".git").mkdir(parents=True)
    (unrelated / "provider.py").write_text("import anthropic\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_prunes_nested_repository_inside_outer_git_root(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_init / ".git").mkdir()
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    nested_repository = isolated_init / "nested-repo"
    (nested_repository / ".git").mkdir(parents=True)
    (nested_repository / "provider.py").write_text(
        "import anthropic\n", encoding="utf-8"
    )
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_skips_oversized_python_sources(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "_MAX_PROVIDER_SOURCE_BYTES", 32, raising=False)
    (isolated_init / "main.py").write_text("import openai\n", encoding="utf-8")
    (isolated_init / "huge.py").write_text(
        "import anthropic\n" + "#" * 64,
        encoding="utf-8",
    )
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_stops_at_deterministic_python_file_limit(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "_MAX_PROVIDER_SOURCE_FILES", 1, raising=False)
    (isolated_init / "a.py").write_text("import openai\n", encoding="utf-8")
    (isolated_init / "b.py").write_text("import anthropic\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["detected_providers"] == ["openai"]


def test_provider_detection_stops_at_deterministic_total_byte_limit(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "_MAX_PROVIDER_TOTAL_BYTES", 20, raising=False)
    (isolated_init / "a.py").write_text("import openai\n", encoding="utf-8")
    (isolated_init / "b.py").write_text("import anthropic\n", encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 0, result.stderr


def test_init_invalid_existing_config_is_structured_before_context_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_module.get_config_path()
    path.parent.mkdir(parents=True)
    path.write_text('api_url = "unterminated-sensitive-value', encoding="utf-8")
    monkeypatch.setattr(init_module, "resolve_credential", _key)

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

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
    path = config_module.get_config_path()
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


def test_final_budget_response_must_match_project_and_be_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [
        httpx.Response(
            200, json={**BUDGET, "project_id": "proj_d0941e68da8f38151ff86a61"}
        )
    ]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_budget_response"


def test_live_budget_failure_is_safe_and_contains_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [
        httpx.Response(200, json={"project_id": "proj_be76331b95dfc399cd776d2f"})
    ]

    result = _invoke("--project", "proj_be76331b95dfc399cd776d2f")

    assert result.exit_code == 1
    assert result.stdout == ""
    rendered = result.stderr + repr(result.exception)
    assert VALID_KEY not in rendered


def test_init_invalid_run_cap_does_not_create_project():
    result = runner.invoke(app, ["--json", "init", "--run-cap", "0.001"])
    assert result.exit_code == 2
    assert not FakeClient.instances


# Review item 3: onboarding honors SOLWYN_AUTH and the login-first class.


def test_solwyn_auth_project_key_never_starts_a_browser_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        init_module,
        "device_login",
        lambda context, **kwargs: pytest.fail(
            "SOLWYN_AUTH=project_key must not log in"
        ),
    )

    result = runner.invoke(app, ["--json", "init"], env={"SOLWYN_AUTH": "project_key"})

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert "SOLWYN_API_KEY" in error["hint"]
    assert FakeClient.instances == []


def test_solwyn_auth_project_key_in_a_terminal_only_asks_for_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []

    def prompt(label: str, **kwargs: object) -> str:
        labels.append(label)
        return VALID_KEY

    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(init_module.typer, "prompt", prompt)
    monkeypatch.setattr(
        init_module,
        "store_supplied_key",
        lambda context, value, store: _key().model_copy(
            update={"project": "proj_be76331b95dfc399cd776d2f", "source": "keyring"}
        ),
    )
    monkeypatch.setattr(
        init_module,
        "device_login",
        lambda context, **kwargs: pytest.fail(
            "SOLWYN_AUTH=project_key must not log in"
        ),
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "init"],
        env={"SOLWYN_AUTH": "project_key"},
    )

    assert result.exit_code == 0, result.stderr
    assert labels == ["API key"]


def test_solwyn_auth_login_in_a_terminal_never_offers_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logins: list[dict[str, object]] = []
    monkeypatch.setattr(init_module, "resolve_credential", lambda **_: None)
    monkeypatch.setattr(init_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        init_module.typer,
        "prompt",
        lambda label, **kwargs: "key" if "method" in label else VALID_KEY,
    )
    monkeypatch.setattr(
        init_module,
        "store_supplied_key",
        lambda *args: pytest.fail("SOLWYN_AUTH=login must not store a key"),
    )
    monkeypatch.setattr(
        init_module,
        "device_login",
        lambda context, **kwargs: logins.append(kwargs) or _jwt(),
    )
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj_be76331b95dfc399cd776d2f", "init"],
        env={"SOLWYN_AUTH": "login"},
    )

    assert result.exit_code == 0, result.stderr
    assert logins == [{"continue_command": INIT_CONTINUE}]


# --- Spec 7.6: which project init uses, and the repo file -------------------

PROJECT_ID = PROJECT["id"]


def _git(repository: Path) -> Path:
    (repository / ".git").mkdir()
    return repository


def _created(name: str, project_id: str = PROJECT_ID) -> dict[str, Any]:
    return {
        **PROJECT,
        "id": project_id,
        "name": name,
        "key": CREATED_KEY,
        "key_prefix": CREATED_KEY[:26],
        "scope": "full",
        **CREATED_KEY_FIELDS,
    }


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _requests() -> list[tuple[str, str]]:
    return [
        (method, path)
        for client in FakeClient.instances
        for method, path, _ in client.requests
    ]


def test_a_flag_that_disagrees_with_the_repo_file_exits_2_before_anything(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    (isolated_init / ".solwyn.env").write_text('SOLWYN_PROJECT="Alpha"\n')
    before = _files(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on(
        "GET",
        "/api/v1/projects",
        httpx.Response(
            200,
            json=[PROJECT, {**PROJECT, "id": "proj_" + "1" * 24, "name": "Other"}],
        ),
    )

    result = runner.invoke(app, ["--json", "--project", "other", "init", "--agents"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_mismatch"
    assert _files(isolated_init) == before
    assert _requests() == []
    assert auth.stored_api_keys() == {}


def test_the_repo_file_selects_the_project_and_is_left_untouched(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    repo_file = isolated_init / ".solwyn.env"
    repo_file.write_text("# mine\nSOLWYN_PROJECT='alpha'\n")
    before = repo_file.read_bytes()
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["project_id"] == PROJECT_ID
    assert payload["project_name"] == "Alpha"
    assert payload["project_created"] is False
    assert payload["repo_file"] == str(repo_file)
    assert payload["repo_file_written"] is False
    assert repo_file.read_bytes() == before
    assert _requests() == [("GET", f"/api/v1/projects/{PROJECT_ID}/budget")]


@pytest.mark.parametrize("output", ["json", "table"])
def test_init_says_a_key_is_stored_when_one_already_is(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any, output: str
) -> None:
    # Run-through finding 4: init said "Key stored: no" while the project's
    # key was stored; key_stored means "stored on this computer".
    _git(isolated_init)
    store_key(project_id=PROJECT_ID, project_name="Alpha")
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app,
        [*(["--json"] if output == "json" else ["--no-color"]), "init"],
        env={"SOLWYN_PROJECT": "alpha"},
    )

    assert result.exit_code == 0, result.stderr
    if output == "json":
        assert json.loads(result.stdout)["key_stored"] is True
    else:
        assert re.search(r"Key stored\s+yes", result.stdout), result.stdout


def test_an_env_selection_uses_the_existing_project_and_writes_it_as_given(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(
        app, ["--json", "init", "--agents"], env={"SOLWYN_PROJECT": "alpha"}
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["project_id"] == PROJECT_ID
    assert payload["project_created"] is False
    assert payload["key_stored"] is False
    assert payload["repo_file_written"] is True
    assert (isolated_init / ".solwyn.env").read_text() == (
        "# Solwyn project for this repo. Not a secret: commit this file.\n"
        'SOLWYN_PROJECT="alpha"\n'
    )
    # init never creates a project from a flag or env value.
    assert ("POST", "/api/v1/projects") not in _requests()
    assert auth.stored_api_keys() == {}


def test_a_flag_naming_no_project_exits_2_and_creates_nothing(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))

    result = runner.invoke(app, ["--json", "--project", "missing", "init"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_not_found"
    assert _requests() == []
    assert not (isolated_init / ".solwyn.env").exists()


def test_an_existing_repo_file_naming_it_differently_is_never_overwritten(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    repo_file = isolated_init / ".solwyn.env"
    repo_file.write_text('SOLWYN_PROJECT="Alpha"\n')
    before = repo_file.read_bytes()
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    # The same project by id: no mismatch, and the file keeps its name.
    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, "init"])

    assert result.exit_code == 0, result.stderr
    assert repo_file.read_bytes() == before
    assert ".solwyn.env already names Alpha; not changed." in result.stderr
    assert json.loads(result.stdout)["repo_file_written"] is False


def test_nothing_selected_creates_a_project_named_after_the_repo(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[{**PROJECT, "name": "REPO"}]),
        httpx.Response(201, json=_created("repo-2")),
        httpx.Response(200, json=BUDGET),
    ]

    result = runner.invoke(
        app,
        [
            "--json",
            "init",
            "--budget",
            "250",
            "--period",
            "weekly",
            "--mode",
            "hard_deny",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert _requests() == [
        ("GET", "/api/v1/projects"),
        ("POST", "/api/v1/projects"),
        ("GET", f"/api/v1/projects/{PROJECT_ID}/budget"),
    ]
    posted = FakeClient.instances[0].requests[1][2]["json"]
    assert posted == {
        "name": "repo-2",
        "budget_limit": 250.0,
        "budget_period": "weekly",
        "budget_mode": "hard_deny",
    }
    stored = auth.stored_api_keys()
    assert list(stored) == [PROJECT_ID]
    assert stored[PROJECT_ID].account_id == "acct_synthetic"
    assert stored[PROJECT_ID].project_name == "repo-2"
    assert (isolated_init / ".solwyn.env").read_text() == (
        "# Solwyn project for this repo. Not a secret: commit this file.\n"
        'SOLWYN_PROJECT="repo-2"\n'
    )
    payload = json.loads(result.stdout)
    assert payload["project_created"] is True
    assert payload["key_stored"] is True
    assert payload["project_name"] == "repo-2"
    assert payload["repo_file_written"] is True
    assert CREATED_KEY not in result.stdout + result.stderr


def test_a_name_taken_in_a_race_retries_the_next_suffix_once(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    from solwyn_cli.errors import error_from_response

    taken = error_from_response(
        httpx.Response(
            409,
            json={"detail": {"code": "project_name_taken", "message": "Taken."}},
        )
    )
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        taken,
        httpx.Response(201, json=_created("repo-2")),
        httpx.Response(200, json=BUDGET),
    ]

    result = _invoke()

    assert result.exit_code == 0, result.stderr
    posts = [
        kwargs["json"]["name"]
        for client in FakeClient.instances
        for method, _path, kwargs in client.requests
        if method == "POST"
    ]
    assert posts == ["repo", "repo-2"]


def test_a_second_race_is_reported_and_not_retried_again(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    from solwyn_cli.errors import error_from_response

    def taken() -> Exception:
        return error_from_response(
            httpx.Response(
                409,
                json={"detail": {"code": "project_name_taken", "message": "Taken."}},
            )
        )

    FakeClient.responses = [httpx.Response(200, json=[]), taken(), taken()]

    result = _invoke()

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "project_name_taken"
    assert _requests().count(("POST", "/api/v1/projects")) == 2
    assert not (isolated_init / ".solwyn.env").exists()


def test_outside_a_git_repo_init_needs_a_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)

    result = _invoke()

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "project_required"
    assert "--project" in error["hint"]
    assert _requests() == []


def test_an_api_key_with_nothing_selected_cannot_create_a_project(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _key)

    result = _invoke()

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "project_required"
    assert _requests() == []
    assert not (isolated_init / ".solwyn.env").exists()


def test_init_writes_no_computer_wide_configuration(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _key)
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = _invoke("--project", PROJECT_ID)

    assert result.exit_code == 0, result.stderr
    assert not config_module.get_config_path().exists()


def test_run_cap_with_a_selected_project_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)

    result = runner.invoke(
        app, ["--json", "--project", PROJECT_ID, "init", "--run-cap", "5"]
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "run_cap_requires_creation"
    assert _requests() == []


def test_run_cap_is_added_right_after_creation(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    rule = {
        "type": "runaway_run",
        "spend_threshold_usd": 5,
        "mode": "hard_deny",
        "channel_ids": [],
        "paused": False,
        "snoozed_until": None,
        "rule_key": "run-cap",
        "routing": {
            "will_send": False,
            "channels": [],
            "source": "project_default",
            "muted": None,
        },
    }
    capped = {**BUDGET, "thresholds": [rule]}
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(200, json=BUDGET),
        httpx.Response(200, json=BUDGET),
        httpx.Response(200, json=capped),
        httpx.Response(200, json=capped),
    ]

    result = runner.invoke(app, ["--json", "init", "--run-cap", "5"])

    assert result.exit_code == 0, result.output
    requests = FakeClient.instances[0].requests
    assert [request[0] for request in requests] == ["GET", "POST", "GET", "GET", "PUT"]
    cap = requests[-1][2]["json"]["thresholds"][0]
    assert cap["type"] == "runaway_run" and cap["mode"] == "hard_deny"
    assert cap["spend_threshold_usd"] == 5
    assert "caps every run at min(caps)" in result.stderr
    assert list(auth.stored_api_keys()) == [PROJECT_ID]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (auth.AuthError("Authentication refresh failed."), "authentication_failed"),
        (
            auth.CredentialStorageError("Refreshed credentials could not be stored."),
            "credential_storage_failed",
        ),
    ],
)
def test_a_run_cap_failure_reports_the_created_project(
    isolated_init: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: auth.AuthError,
    code: str,
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        error,
    ]

    result = runner.invoke(app, ["--json", "init", "--run-cap", "5"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert (
        f"Project {PROJECT_ID} was created; review its budget rules before "
        "retrying the run-cap setup."
    ) in result.stderr
    assert json.loads(result.stderr.splitlines()[-1])["code"] == code
    for secret in (CREATED_KEY, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in result.stdout + result.stderr + repr(result.exception)


def test_a_created_project_is_verified_with_the_login(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(200, json=BUDGET),
    ]

    result = runner.invoke(app, ["--json", "init"], env={"SOLWYN_AUTH": "login"})

    assert result.exit_code == 0, result.stderr
    assert isinstance(FakeClient.instances[-1].credential, auth.LoginCredential)


# --- Review round 1 (#57) ----------------------------------------------------


def _requests() -> list[tuple[str, str]]:
    return [(m, p) for c in FakeClient.instances for m, p, _ in c.requests]


def test_init_refuses_a_leftover_solwyn_project_id_before_creating_anything(
    monkeypatch: pytest.MonkeyPatch, isolated_init: Path
) -> None:
    (isolated_init / ".git").mkdir()
    monkeypatch.setenv("SOLWYN_PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(200, json=BUDGET),
    ]

    result = _invoke()

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "usage_error"
    assert "SOLWYN_PROJECT_ID was replaced by SOLWYN_PROJECT" in error["message"]
    assert _requests() == []
    assert not (isolated_init / ".solwyn.env").exists()
    assert auth.stored_api_keys() == {}


def test_init_never_writes_a_project_it_could_not_verify(
    monkeypatch: pytest.MonkeyPatch, isolated_init: Path
) -> None:
    from solwyn_cli.errors import error_from_response

    (isolated_init / ".git").mkdir()
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    missing = error_from_response(
        httpx.Response(
            404,
            json={"detail": "Project not found"},
            request=httpx.Request("GET", "https://api.test/x"),
        )
    )
    FakeClient.responses = [missing]

    result = runner.invoke(
        app, ["--json", "--project", OTHER_PROJECT_ID, "init", "--agents"]
    )

    assert result.exit_code != 0
    assert not (isolated_init / ".solwyn.env").exists()
    assert not (isolated_init / "AGENTS.md").exists()


# --- A minted key is stored, shown once, or revoked (audit F1) ----------------

CREATED_KEY_ID = CREATED_KEY_FIELDS["key_id"]


def _init_store_fails(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, *, tty: bool
) -> None:
    from solwyn_cli.commands import _minted

    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    monkeypatch.setattr(_minted, "_stdout_is_terminal", lambda: tty)

    def fail(**_fields: Any) -> Any:
        raise error

    monkeypatch.setattr(init_module.auth, "store_api_key", fail)


def test_init_revokes_a_key_it_cannot_store_or_show(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    _init_store_fails(
        monkeypatch, auth.CredentialStorageError("Keychain timed out."), tty=False
    )
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(204),
    ]

    result = runner.invoke(app, ["--json", "init"])

    assert result.exit_code == 3
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "credential_storage_failed"
    assert f"The new key {CREATED_KEY_ID} was revoked" in error["message"]
    assert ("DELETE", f"/api/v1/keys/{CREATED_KEY_ID}") in _requests()
    assert CREATED_KEY not in result.stdout + result.stderr


def test_init_shows_a_key_it_cannot_store_once_on_a_terminal(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    _init_store_fails(monkeypatch, RuntimeError("synthetic crash"), tty=True)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(200, json=BUDGET),
    ]

    result = runner.invoke(app, ["--json", "init"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["key"] == CREATED_KEY
    assert payload["key_stored"] is False
    assert payload["project_created"] is True
    assert "Could not store the new key" in result.stderr
    assert CREATED_KEY not in result.stderr
    assert not any(method == "DELETE" for method, _ in _requests())


def test_init_revokes_an_unshown_key_when_a_later_step_fails(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    _init_store_fails(monkeypatch, RuntimeError("synthetic crash"), tty=True)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        auth.AuthError("Authentication refresh failed."),
        httpx.Response(204),
    ]

    result = runner.invoke(app, ["--json", "init", "--run-cap", "5"])

    assert result.exit_code == 3
    assert CREATED_KEY not in result.stdout + result.stderr
    assert ("DELETE", f"/api/v1/keys/{CREATED_KEY_ID}") in _requests()
    assert "it was revoked" in result.stderr


def test_init_revokes_the_key_on_ctrl_c_while_storing(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    _init_store_fails(monkeypatch, KeyboardInterrupt(), tty=True)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(204),
    ]

    result = runner.invoke(app, ["--json", "init"])

    assert result.exit_code != 0
    assert CREATED_KEY not in result.stdout + result.stderr
    assert ("DELETE", f"/api/v1/keys/{CREATED_KEY_ID}") in _requests()


# --- The repo file is written right after the project exists (audit F1) -------

RERUN = f"solwyn --project {PROJECT_ID} init"


def _repo_names(root: Path) -> str | None:
    path = root / ".solwyn.env"
    return path.read_text() if path.exists() else None


def test_a_key_store_failure_leaves_the_repo_file_and_the_rerun_command(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    _init_store_fails(
        monkeypatch, auth.CredentialStorageError("Keychain timed out."), tty=False
    )
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(204),
    ]

    result = runner.invoke(app, ["--json", "init"])

    assert result.exit_code == 3
    assert 'SOLWYN_PROJECT="repo"' in (_repo_names(isolated_init) or "")
    assert PROJECT_ID in result.stderr
    assert RERUN in result.stderr


def test_a_run_cap_failure_has_its_own_code_and_leaves_the_repo_file(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        httpx.Response(200, json={"unexpected": True}),
    ]

    result = runner.invoke(app, ["--json", "init", "--run-cap", "5"])

    assert result.exit_code == 1
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "run_cap_failed"
    assert PROJECT_ID in error["message"]
    assert 'SOLWYN_PROJECT="repo"' in (_repo_names(isolated_init) or "")
    assert RERUN in result.stderr
    # The key was stored before the run cap: it is kept, never revoked.
    assert list(auth.stored_api_keys()) == [PROJECT_ID]
    assert not any(method == "DELETE" for method, _ in _requests())


def test_ctrl_c_after_creation_leaves_the_repo_file_and_the_rerun_command(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        KeyboardInterrupt(),
    ]

    result = runner.invoke(app, ["--json", "init", "--run-cap", "5"])

    assert result.exit_code != 0
    assert 'SOLWYN_PROJECT="repo"' in (_repo_names(isolated_init) or "")
    assert RERUN in result.stderr


def test_a_verification_failure_after_creation_prints_the_rerun_command(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(isolated_init)
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json=[]),
        httpx.Response(201, json=_created("repo")),
        ApiError(
            status_code=None,
            code="transport_error",
            message="Could not connect to the Solwyn API.",
            hint=None,
            detail=None,
        ),
    ]

    result = runner.invoke(app, ["--json", "init"])

    assert result.exit_code == 1
    assert RERUN in result.stderr
    assert 'SOLWYN_PROJECT="repo"' in (_repo_names(isolated_init) or "")


def test_a_rerun_with_the_printed_command_finishes_without_a_new_project(
    isolated_init: Path, monkeypatch: pytest.MonkeyPatch, fake_core: Any
) -> None:
    _git(isolated_init)
    (isolated_init / ".solwyn.env").write_text('SOLWYN_PROJECT="Alpha"\n')
    monkeypatch.setattr(init_module, "resolve_credential", _jwt)
    fake_core.on("GET", "/api/v1/projects", httpx.Response(200, json=[PROJECT]))
    FakeClient.responses = [httpx.Response(200, json=BUDGET)]

    result = runner.invoke(app, ["--json", "--project", PROJECT_ID, "init"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["project_created"] is False
    assert not any(method == "POST" for method, _ in _requests())
