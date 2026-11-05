"""Public CLI pagination using synthetic, offline HTTP responses."""

import json
from copy import deepcopy
from pathlib import Path

import click
import httpx
import pytest
from help_options import assert_public_bool_flag
from typer.testing import CliRunner

from solwyn_cli.auth import ApiKeyCredential
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import costs, runs, tags
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN = Path(__file__).parent / "golden"
CASES = [
    (costs, ["costs", "events"], "costs-events-json.json", "items"),
    (runs, ["runs", "list"], "runs-list-jwt-json.json", "runs"),
    (tags, ["tags", "values", "route /? # ="], "tags-values-json.json", "values"),
]


@pytest.fixture(params=CASES, ids=["costs", "runs", "tags"])
def paging(request, monkeypatch):
    module, command, golden, field = request.param
    template = json.loads((GOLDEN / golden).read_text())
    schema = template.pop("schema")
    calls, replies = [], []

    def handle(req):
        calls.append(req)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(**kwargs):
        return SolwynClient(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(module, "SolwynClient", client)
    monkeypatch.setattr(
        module,
        "resolve_credential",
        lambda **_: ApiKeyCredential(
            api_key="solwyn_sk_" + "a4" * 32, source="environment"
        ),
    )
    monkeypatch.setattr(costs, "get_capabilities", lambda **kwargs: None)

    def page(cursor=None, *, empty=False, label="first", **updates):
        data = deepcopy(template)
        data.update(next_cursor=cursor, has_more=cursor is not None)
        if empty:
            data[field] = []
        else:
            row = data[field][0]
            if field == "values":
                row["value"] = label
            elif field == "runs":
                row["name"] = label
                row["id"] = "run_" + ("1a" if label == "first" else "2b") * 16
            else:
                row["id"] = label
                row["model"] = label
        data.update(updates)
        return httpx.Response(200, json=data)

    def invoke(*args):
        return runner.invoke(
            app,
            ["--json", "--project", "proj_be76331b95dfc399cd776d2f", *command, *args],
        )

    return calls, replies, page, invoke, field, schema, command


@pytest.mark.parametrize("start", [None, "start"])
@pytest.mark.parametrize("empty_final", [False, True])
def test_all_pages_preserve_order_and_exhaustion(paging, start, empty_final):
    calls, replies, page, invoke, field, schema, _ = paging
    replies.extend(
        [
            page("second"),
            page("third", label="second"),
            page(empty=empty_final, label="last"),
        ]
    )
    args = ["--all", "--limit", "2"] + (["--cursor", start] if start else [])
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["schema"] == schema
    assert data["next_cursor"] is None and data["has_more"] is False
    label = {"items": "id", "runs": "name", "values": "value"}[field]
    assert [row[label] for row in data[field]] == ["first", "second"] + (
        [] if empty_final else ["last"]
    )
    assert [req.url.params.get("cursor") for req in calls] == [start, "second", "third"]
    assert all(req.url.params["limit"] == "2" for req in calls)
    assert all(req.method == "GET" for req in calls)


def test_default_remains_one_page_with_truthful_continuation(paging):
    calls, replies, page, invoke, field, _, _ = paging
    replies.append(page("next"))
    result = invoke()
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data[field]) == 1
    assert data["next_cursor"] == "next" and data["has_more"] is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "extra",
        "inconsistent",
        "malformed",
        "repeated",
        "cycle",
        "http",
        "transport",
        "wrong_identity",
    ],
)
def test_all_pages_fail_closed_without_partial_success(paging, failure):
    calls, replies, page, invoke, field, _, _ = paging
    replies.append(page("next"))
    if failure == "extra":
        replies.append(page(unadopted_field=True))
    elif failure == "inconsistent":
        replies.append(page(has_more=True))
    elif failure == "malformed":
        replies.append(httpx.Response(200, json={field: "invalid"}))
    elif failure == "repeated":
        replies.append(page("next"))
    elif failure == "cycle":
        replies.append(page("start"))
    elif failure == "http":
        replies.append(httpx.Response(403, json={"detail": "Forbidden"}))
    elif failure == "transport":
        replies.append(httpx.ConnectError("synthetic transport failure"))
    else:
        response = page()
        body = json.loads(response.content)
        if field == "values":
            body["key"] = "wrong"
        else:
            body[field][0]["project_id"] = "wrong"
        replies.append(httpx.Response(200, json=body))
    result = invoke("--all", "--cursor", "start")
    assert result.exit_code != 0, result.output
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schema"] == "solwyn.error.v1"
    assert field not in error
    assert len(calls) == 2


def test_all_pages_forward_filters_and_custom_windows(paging):
    calls, replies, page, invoke, field, _, _ = paging
    replies.extend([page("next"), page()])
    if field == "items":
        args = [
            "--provider",
            "openai",
            "--model",
            "gpt-5-mini",
            "--modality",
            "text",
            "--status",
            "success",
            "--service-tier",
            "default",
            "--provider-region",
            "us-east-1",
            "--agent-run",
            "agent",
            "--failover-reason",
            "primary_error",
            "--model-fallback",
            "--no-provider-fallback",
            "--tag",
            "customer=example",
        ]
        expected = {
            "provider": "openai",
            "model": "gpt-5-mini",
            "modality": "text",
            "status": "success",
            "service_tier": "default",
            "provider_region": "us-east-1",
            "agent_run": "agent",
            "failover_reason": "primary_error",
            "is_model_fallback": "true",
            "is_provider_fallback": "false",
            "tag.customer": "example",
        }
    elif field == "runs":
        args = ["--q", "agent", "--parent", "parent", "--sort", "total_cost"]
        expected = {"q": "agent", "parent": "parent", "sort": "total_cost"}
    else:
        args = ["--prefix", "p"]
        expected = {"key": "route /? # =", "q": "p"}
    if field != "values":
        args += ["--from", "2026-08-01T00:00:00Z", "--to", "2026-08-08T00:00:00Z"]
        expected |= {"from": "2026-08-01T00:00:00Z", "to": "2026-08-08T00:00:00Z"}
    result = invoke("--all", *args)
    assert result.exit_code == 0, result.output
    for req in calls:
        for key, value in expected.items():
            assert req.url.params[key] == value


def test_all_pages_preserve_first_window_and_any_retention_clamp(paging):
    _, replies, page, invoke, field, _, _ = paging
    if field == "values":
        # Tag values have no window contract.
        return
    first = page("next")
    window = json.loads(first.content)["window"]
    later_window = window | {
        "retention_clamped": True,
        "effective_from": "2026-08-03T00:00:00Z",
    }
    replies.extend([first, page(window=later_window)])
    result = invoke("--all")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["window"] == window | {"retention_clamped": True}


@pytest.mark.parametrize(
    "command", [["costs", "events"], ["runs", "list"], ["tags", "values"]]
)
@pytest.mark.parametrize("color", [False, True], ids=["no-color", "color"])
def test_public_help_advertises_all(command, color, monkeypatch):
    # Typer caches its force-terminal decision at import, before these cases.
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", color)
    monkeypatch.setenv("TERM", "xterm-256color" if color else "dumb")
    if color:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
    else:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(app, [*command, "--help"], terminal_width=160, color=color)
    assert result.exit_code == 0
    assert ("\x1b[" in result.output) is color
    rendered = click.unstyle(result.output)
    assert "--all" in rendered
    assert "Follow all remaining cursor pages" in " ".join(
        rendered.replace("│", " ").split()
    )
    assert_public_bool_flag(command, "--all")


def test_providers_group_has_useful_public_description():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Inspect provider failovers" in result.output


def test_all_pages_table_keeps_server_order_and_has_no_stale_continuation(paging):
    _, replies, page, _, _, _, command = paging
    replies.extend([page("next"), page(label="second")])
    result = runner.invoke(
        app, ["--project", "proj_be76331b95dfc399cd776d2f", *command, "--all"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.index("first") < result.stdout.index("second")
    assert "next cursor" not in result.stderr.lower()
    assert "More events" not in result.stderr


def test_all_pages_empty_first_page_is_successful_exhaustion(paging):
    calls, replies, page, invoke, field, _, _ = paging
    replies.append(page(empty=True))
    result = invoke("--all")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data[field] == []
    assert data["next_cursor"] is None and data["has_more"] is False
    assert len(calls) == 1


def test_all_pages_reject_nonadvancing_initial_cursor_before_refetch(paging):
    calls, replies, page, invoke, _, _, _ = paging
    replies.append(page("start"))
    result = invoke("--all", "--cursor", "start")
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"].startswith("invalid_")
    assert len(calls) == 1
