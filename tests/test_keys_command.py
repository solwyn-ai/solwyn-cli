"""Project key recovery command contract tests."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import keys as keys_module
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app
from login_fixtures import login_credential


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "ab" * 32
ACCESS_TOKEN = "keys-access-token"
REFRESH_TOKEN = "keys-refresh-token"
PROJECT_ID = "proj/recovery"
KEY_ID = "key/old"
ROTATED_KEY = "sk_proj_" + "cd" * 32

KEY_METADATA = {
    "id": "key_current",
    "key_prefix": "sk_proj_1234567890abcdef",
    "name": "Current key",
    "last_used_at": "2026-07-09T14:30:00Z",
    "expires_at": None,
    "rotation_grace_until": None,
    "created_at": "2026-07-01T08:00:00Z",
    "scope": "read_only",
}
ROTATE_RESPONSE = {
    "id": "key_replacement",
    "key": ROTATED_KEY,
    "key_prefix": ROTATED_KEY[:24],
    "rotation_grace_until": "2026-07-10T13:30:00Z",
    "scope": "full",
}


class FakeClient:
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
    monkeypatch.setattr(keys_module, "SolwynClient", FakeClient)
    yield


def _jwt(**_: object) -> auth.LoginCredential:
    return login_credential(ACCESS_TOKEN)


def _key(source: str) -> auth.ProjectKeyCredential:
    return auth.ProjectKeyCredential(api_key=VALID_KEY, source=source)


def _options(output_format: str) -> list[str]:
    return ["--json"] if output_format == "json" else ["--no-color"]


def test_keys_group_registers_exact_recovery_surface() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["keys", "--help"])

    assert root.exit_code == 0
    assert "keys" in root.stdout
    assert group.exit_code == 0
    assert "show" in group.stdout
    assert "rotate" in group.stdout
    assert "revoke" in group.stdout
    assert "create" not in group.stdout


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_show_uses_quoted_project_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [httpx.Response(200, json=KEY_METADATA)]

    result = runner.invoke(
        app,
        [*_options(output_format), "--project", PROJECT_ID, "keys", "show"],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].credential is credential
    assert FakeClient.instances[0].requests == [
        ("GET", "/api/v1/projects/proj%2Frecovery/key", {})
    ]
    assert "key" not in json.loads(result.stdout) if output_format == "json" else True
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"keys-show-{output_format}.{suffix}"
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        (["show"], {**KEY_METADATA, "server_note": "additive metadata"}),
        (["rotate", "--yes"], {**ROTATE_RESPONSE, "server_note": "additive metadata"}),
    ],
)
def test_keys_responses_ignore_benign_additive_fields_without_rendering_them(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(app, ["--json", "--project", "proj", "keys", *command])

    assert result.exit_code == 0, result.stderr
    assert "server_note" not in result.stdout + result.stderr


def test_keys_show_rejects_raw_key_before_metadata_model_validation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json={**KEY_METADATA, "key": ROTATED_KEY})
    ]
    monkeypatch.setattr(
        keys_module._ProjectKeyMetadata,
        "model_validate",
        lambda _payload: pytest.fail("raw key reached metadata validation"),
    )

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(app, ["--json", "--project", "proj", "keys", "show"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "invalid_keys_response"
    assert ROTATED_KEY not in result.stderr + repr(result.exception) + caplog.text


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_rotate_posts_once_and_outputs_secret_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = _jwt()
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: credential)
    FakeClient.responses = [
        httpx.Response(200, json=ROTATE_RESPONSE),
        RuntimeError("must not retry"),
    ]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            [
                *_options(output_format),
                "--project",
                PROJECT_ID,
                "keys",
                "rotate",
                "--yes",
                "--grace-seconds",
                "3600",
            ],
        )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "POST",
            "/api/v1/projects/proj%2Frecovery/key/rotate",
            {
                "json": {"grace_seconds": 3600, "scope": None},
                "retry_rate_limit": False,
            },
        )
    ]
    rendered = result.stdout + result.stderr + repr(result.exception) + caplog.text
    assert rendered.count(ROTATED_KEY) == 1
    assert (
        result.stderr
        == "Rotate the active key in project 'proj/recovery': its replacement is shown once; the old key becomes invalid after 3600 seconds.\n"
    )
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"keys-rotate-{output_format}.{suffix}"
    )


def test_keys_rotate_maps_omitted_grace_and_scope_to_explicit_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An omitted scope posts null, which asks the API to keep the current one."""
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(200, json=ROTATE_RESPONSE)]

    result = runner.invoke(
        app, ["--json", "--project", "proj", "keys", "rotate", "--yes"]
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "grace_seconds": None,
        "scope": None,
    }


@pytest.mark.parametrize("scope", ["full", "read_only"])
def test_keys_rotate_posts_the_chosen_scope_for_the_replacement_key(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [
        httpx.Response(200, json={**ROTATE_RESPONSE, "scope": scope})
    ]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "keys", "rotate", "--yes", "--scope", scope],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][2]["json"] == {
        "grace_seconds": None,
        "scope": scope,
    }
    # The minted scope is echoed back so the result confirms what now exists.
    assert json.loads(result.stdout)["scope"] == scope


def test_keys_rotate_rejects_an_unknown_scope_before_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        keys_module,
        "resolve_credential",
        lambda **_: pytest.fail("must validate before auth"),
    )

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "keys", "rotate", "--yes", "--scope", "admin"],
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_revoke_deletes_once_requires_204_and_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(204), RuntimeError("must not retry")]

    result = runner.invoke(
        app,
        [
            *_options(output_format),
            "--project",
            PROJECT_ID,
            "keys",
            "revoke",
            "--yes",
            "--key-id",
            KEY_ID,
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests == [
        (
            "DELETE",
            "/api/v1/projects/proj%2Frecovery/key/key%2Fold",
            {"retry_rate_limit": False},
        )
    ]
    assert ROTATED_KEY not in result.stdout + result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"keys-revoke-{output_format}.{suffix}"
    )
    assert (
        result.stderr
        == "Hard-revoke key 'key/old' in project 'proj/recovery': invalidates it without a replacement.\n"
    )


@pytest.mark.parametrize(
    "command", [["show"], ["rotate", "--yes"], ["revoke", "--yes", "--key-id", "key"]]
)
@pytest.mark.parametrize("source", ["environment", "keyring"])
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_commands_reject_both_key_sources_without_request(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    source: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: _key(source))

    result = runner.invoke(
        app,
        [*_options(output_format), "--project", "proj", "keys", *command],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert f"Run solwyn login, then retry keys {command[0]}." in result.stderr
    assert FakeClient.instances == []

    if command == ["show"]:
        suffix = "json" if output_format == "json" else "txt"
        assert_matches_golden(
            result.stderr,
            GOLDEN_DIR / f"keys-{source}-key-refusal-{output_format}.{suffix}",
        )


def test_keys_commands_require_auth_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", lambda **_: None)

    result = runner.invoke(app, ["--json", "--project", "proj", "keys", "show"])

    assert result.exit_code == 3
    assert result.stdout == ""
    assert (
        json.loads(result.stderr.splitlines()[-1])["code"] == "authentication_required"
    )
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    "args",
    [
        ["--project", "", "keys", "show"],
        ["--project", "   ", "keys", "rotate", "--yes"],
        ["--project", "proj", "keys", "rotate", "--yes", "--grace-seconds", "-1"],
        ["--project", "proj", "keys", "rotate", "--yes", "--grace-seconds", "604801"],
        ["--project", "proj", "keys", "revoke", "--yes", "--key-id", ""],
        ["--project", "proj", "keys", "revoke", "--yes", "--key-id", "   "],
    ],
)
def test_keys_commands_validate_identifiers_and_grace_before_auth(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    monkeypatch.setattr(
        keys_module,
        "resolve_credential",
        lambda **_: pytest.fail("must validate before auth"),
    )

    result = runner.invoke(app, ["--json", *args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.parametrize(
    ("command", "status", "code", "expected_exit", "hint_fragment"),
    [
        (["show"], 404, "not_found", 5, "project or key"),
        (["rotate", "--yes"], 409, "rotation_conflict", 1, "already"),
        (["rotate", "--yes"], 410, "key_inactive", 1, "inactive"),
        (["rotate", "--yes"], 429, "rate_limited", 1, "later"),
        (["revoke", "--yes", "--key-id", "key"], 404, "not_found", 5, "project or key"),
    ],
)
def test_keys_api_errors_are_actionable_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    status: int,
    code: str,
    expected_exit: int,
    hint_fragment: str,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    detail = {"code": code, "message": "Server detail remains structured."}
    FakeClient.responses = [
        ApiError(
            status_code=status,
            code=code,
            message="Server detail remains structured.",
            hint=None,
            detail=detail,
        ),
        RuntimeError("must not retry"),
    ]

    result = runner.invoke(
        app,
        ["--json", "--project", "proj", "keys", *command],
    )

    assert result.exit_code == expected_exit
    payload = json.loads(result.stderr.splitlines()[-1])
    assert payload["detail"] == detail
    assert hint_fragment in payload["hint"].lower()
    assert len(FakeClient.instances[0].requests) == 1


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_keys_show_not_found_matches_golden(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    detail = {"code": "not_found", "message": "Project or key was not found."}
    FakeClient.responses = [
        ApiError(
            status_code=404,
            code="not_found",
            message="Project or key was not found.",
            hint=None,
            detail=detail,
        )
    ]

    result = runner.invoke(
        app,
        [*_options(output_format), "--project", "proj", "keys", "show"],
    )

    assert result.exit_code == 5
    assert result.stdout == ""
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stderr, GOLDEN_DIR / f"keys-not-found-{output_format}.{suffix}"
    )


@pytest.mark.parametrize(
    ("command", "response"),
    [
        (
            ["show"],
            httpx.Response(
                200, json={**KEY_METADATA, "created_at": "2026-07-01T08:00:00"}
            ),
        ),
        (["show"], httpx.Response(200, json={**KEY_METADATA, "key": ROTATED_KEY})),
        (
            ["rotate", "--yes"],
            httpx.Response(200, json={**ROTATE_RESPONSE, "key": "not-a-key"}),
        ),
        (
            ["rotate", "--yes"],
            httpx.Response(
                200,
                json={**ROTATE_RESPONSE, "rotation_grace_until": "2026-07-10T13:30:00"},
            ),
        ),
        (["show"], httpx.Response(201, json=KEY_METADATA)),
        (["rotate", "--yes"], httpx.Response(201, json=ROTATE_RESPONSE)),
        # The spec requires scope on both responses: absent or unknown is invalid.
        (
            ["show"],
            httpx.Response(
                200,
                json={
                    field: value
                    for field, value in KEY_METADATA.items()
                    if field != "scope"
                },
            ),
        ),
        (["show"], httpx.Response(200, json={**KEY_METADATA, "scope": "admin"})),
        (
            ["rotate", "--yes"],
            httpx.Response(
                200,
                json={
                    field: value
                    for field, value in ROTATE_RESPONSE.items()
                    if field != "scope"
                },
            ),
        ),
        (
            ["rotate", "--yes"],
            httpx.Response(200, json={**ROTATE_RESPONSE, "scope": "admin"}),
        ),
        (
            ["rotate", "--yes"],
            httpx.Response(
                200,
                json={**ROTATE_RESPONSE, "key_prefix": "sk_proj_1234567890abcdef"},
            ),
        ),
        (["revoke", "--yes", "--key-id", "key"], httpx.Response(200, json={})),
        (["revoke", "--yes", "--key-id", "key"], httpx.Response(204, json={})),
    ],
)
def test_keys_commands_reject_malformed_or_wrong_status_responses_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [response]

    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app,
            ["--json", "--project", "proj", "keys", *command],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "invalid_keys_response"
    rendered = result.stderr + repr(result.exception) + caplog.text
    assert ROTATED_KEY not in rendered
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert len(FakeClient.instances[0].requests) == 1


def test_keys_use_global_project_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLWYN_PROJECT_ID", "env-project")
    monkeypatch.setattr(keys_module, "resolve_credential", _jwt)
    FakeClient.responses = [httpx.Response(200, json=KEY_METADATA)]

    result = runner.invoke(app, ["--json", "--project", "flag-project", "keys", "show"])

    assert result.exit_code == 0, result.stderr
    assert FakeClient.instances[0].requests[0][1] == (
        "/api/v1/projects/flag-project/key"
    )


def test_rotated_secret_occurs_only_once_in_authorized_goldens() -> None:
    key_goldens = sorted(GOLDEN_DIR.glob("keys-*"))
    rotate_goldens = [path for path in key_goldens if "keys-rotate-" in path.name]

    assert len(rotate_goldens) == 2
    assert all(
        path.read_text(encoding="utf-8").count(ROTATED_KEY) == 1
        for path in rotate_goldens
    )
    assert all(
        ROTATED_KEY not in path.read_text(encoding="utf-8")
        for path in key_goldens
        if path not in rotate_goldens
    )
