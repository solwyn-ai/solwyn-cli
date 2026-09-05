"""Account-scoped discovery and retirement."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli.commands import tags
from solwyn_cli.main import app
from solwyn_cli.errors import error_from_response
from golden.harness import assert_matches_golden
from solwyn_cli import auth

GOLDEN_DIR = Path(__file__).parent / "golden"
runner = CliRunner()
STAMP = "2026-09-05T12:00:00Z"
DISCOVERY = {
    "keys": [
        {
            "key": "env",
            "values_count": 1,
            "first_seen_at": STAMP,
            "last_seen_at": STAMP,
            "similar": [" Env"],
        }
    ],
    "usage": {"keys_used": 1, "keys_cap": 100, "values_cap_per_key": 1000},
    "scope": "account",
}
VALUES = {
    "key": "route /? # =",
    "values": [{"value": "prod", "first_seen_at": STAMP, "last_seen_at": STAMP}],
    "next_cursor": None,
    "has_more": False,
}


@pytest.fixture
def transport(monkeypatch):
    requests, replies = [], []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path, **kwargs):
            requests.append((method, path, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setenv("SOLWYN_API_KEY", "sk_proj_" + "f6" * 32)
    monkeypatch.setattr(tags, "SolwynClient", Client)
    return requests, replies


def test_list_account_scope(transport):
    requests, replies = transport
    replies.append(httpx.Response(200, json=DISCOVERY))
    result = runner.invoke(app, ["--json", "--project", "proj_1", "tags", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["scope"] == "account"
    assert requests[0][1] == "/api/v1/projects/proj_1/tags"


def test_values_reserved_characters_are_query_parameter(transport):
    requests, replies = transport
    replies.append(httpx.Response(200, json=VALUES))
    result = runner.invoke(
        app,
        [
            "--json",
            "--project",
            "proj_1",
            "tags",
            "values",
            "route /? # =",
            "--prefix",
            "p",
            "--limit",
            "5",
            "--cursor",
            "cursor",
        ],
    )
    assert result.exit_code == 0, result.output
    assert requests[0][1] == "/api/v1/projects/proj_1/tags/values"
    assert requests[0][2]["params"] == {
        "key": "route /? # =",
        "q": "p",
        "limit": 5,
        "cursor": "cursor",
    }


def test_retirement_requires_jwt_before_request(transport):
    requests, _ = transport
    result = runner.invoke(app, ["--json", "tags", "retire", "env"])
    assert result.exit_code == 3
    assert "JWT" in result.stderr
    assert requests == []


@pytest.mark.parametrize("mode", ["table", "json"])
@pytest.mark.parametrize("case", ["list", "values", "retire"])
def test_tag_goldens(transport, monkeypatch, mode, case):
    _, replies = transport
    if case == "retire":
        monkeypatch.setattr(
            tags,
            "resolve_credential",
            lambda: auth.JwtCredential(access_token="jwt", refresh_token="refresh"),
        )
        replies.append(httpx.Response(204))
    else:
        replies.append(
            httpx.Response(200, json=DISCOVERY if case == "list" else VALUES)
        )
    args = ["tags", case]
    if case != "list":
        args.append("env" if case == "retire" else "route /? # =")
    result = runner.invoke(
        app,
        [
            *(["--json"] if mode == "json" else ["--no-color"]),
            "--project",
            "proj_1",
            *args,
        ],
    )
    assert result.exit_code == 0, result.output
    suffix = "json" if mode == "json" else "txt"
    assert_matches_golden(result.stdout, GOLDEN_DIR / f"tags-{case}-{mode}.{suffix}")


@pytest.mark.parametrize(
    "code,detail,expected",
    [
        (
            402,
            {
                "code": "upgrade_required",
                "message": "Tag analytics require Team",
                "feature": "analytics.tags",
                "cta_path": "/billing",
                "allowed_values": None,
            },
            1,
        ),
        (403, "admin_required", 3),
        (404, "Tag key not found", 5),
    ],
)
def test_tags_error_contract(transport, code, detail, expected):
    _, replies = transport
    replies.append(error_from_response(httpx.Response(code, json={"detail": detail})))
    result = runner.invoke(app, ["--json", "--project", "proj_1", "tags", "list"])
    assert result.exit_code == expected
    if code == 402:
        assert "Tag analytics need Team" in result.stderr
        assert "/billing" in result.stderr


def test_tag_values_limit_matches_core(transport):
    requests, _ = transport
    result = runner.invoke(
        app,
        ["--json", "--project", "proj_1", "tags", "values", "env", "--limit", "101"],
    )
    assert result.exit_code == 2 and requests == []


def test_retire_never_registered_is_not_found(transport, monkeypatch):
    requests, replies = transport
    monkeypatch.setattr(
        tags,
        "resolve_credential",
        lambda: auth.JwtCredential(access_token="jwt", refresh_token="refresh"),
    )
    replies.append(
        error_from_response(httpx.Response(404, json={"detail": "Tag key not found"}))
    )
    result = runner.invoke(app, ["--json", "tags", "retire", "missing"])
    assert result.exit_code == 5
    assert requests[0] == (
        "DELETE",
        "/api/v1/account/tags",
        {"params": {"key": "missing"}},
    )
