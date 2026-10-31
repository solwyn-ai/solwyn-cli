"""Deliberate billing lifecycle commands against synthetic Core responses only."""

from copy import deepcopy
import json
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import plan
from solwyn_cli.main import app
from test_plan_command import BILLING_STATUS
from login_fixtures import (
    APPROVAL_ID,
    fake_keychain,
    login_credential,
)

runner = CliRunner()
JWT = login_credential("synthetic-access")
TARGET = {"target_tier": "scale", "target_billing_interval": "year"}
PREVIEW = {
    **TARGET,
    "mode": "immediate",
    "effective_at": None,
    "proration_date": 1791936000,
    "amount_due_cents": 1275,
    "total_cents": 1275,
    "currency": "usd",
    "lines": [
        {
            "description": "Unused term credit",
            "amount_cents": -725,
            "currency": "usd",
            "period_start": "2026-10-14T00:00:00Z",
            "period_end": None,
            "proration": True,
        },
        {
            "description": "New plan",
            "amount_cents": 2000,
            "currency": "usd",
            "period_start": None,
            "period_end": "2027-10-14T00:00:00Z",
            "proration": True,
        },
    ],
}


@pytest.fixture
def billing(monkeypatch):
    queue = []
    calls = []

    def handler(request):
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(plan, "resolve_credential", lambda **_: JWT)
    monkeypatch.setattr(plan, "_is_interactive", lambda: False, raising=False)
    monkeypatch.setattr(
        plan,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=lambda _: None
        ),
    )
    return queue, calls


def invoke(*args, input=None):
    return runner.invoke(app, ["--json", "plan", *args], input=input)


@pytest.mark.parametrize(
    "command,method,cancel_at",
    [("cancel", "POST", "2027-10-01T00:00:00Z"), ("resume", "DELETE", None)],
)
def test_cancel_resume_actual_nullable_result_and_term_effects(
    billing, command, method, cancel_at
):
    queue, calls = billing
    queue.extend(
        [
            httpx.Response(200, json=BILLING_STATUS),
            httpx.Response(200, json={"status": "active", "cancel_at": cancel_at}),
        ]
    )
    result = invoke(command, "--yes")
    assert result.exit_code == 0, result.output
    assert calls == [
        ("GET", "/api/v1/billing/status", None),
        (method, "/api/v1/billing/cancel", None),
    ]
    assert json.loads(result.stdout)["cancel_at"] == cancel_at
    assert "year" in result.stderr
    assert (
        "no proration" if command == "cancel" else "renewal"
    ) in result.stderr.lower()


@pytest.mark.parametrize("mode", ["immediate", "scheduled"])
@pytest.mark.parametrize("dry", [False, True])
def test_change_preserves_real_preview_and_sends_only_targets(billing, mode, dry):
    queue, calls = billing
    preview = deepcopy(PREVIEW)
    preview.update(
        mode=mode, effective_at="2027-10-01T00:00:00Z" if mode == "scheduled" else None
    )
    if mode == "scheduled":
        preview.update(amount_due_cents=0, total_cents=0, lines=[])
    queue.append(httpx.Response(200, json=preview))
    applied = {
        "mode": mode,
        "status": "active",
        "proration_date": None,
        "effective_at": preview["effective_at"],
    }
    if not dry:
        queue.append(httpx.Response(200, json=applied))
    result = invoke(
        "change",
        "--tier",
        "scale",
        "--interval",
        "year",
        "--dry-run" if dry else "--yes",
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["preview"] == preview
    assert data["dry_run"] is dry
    assert data["result"] == (None if dry else applied)
    assert calls == [("POST", "/api/v1/billing/plan-preview", TARGET)] + (
        [] if dry else [("POST", "/api/v1/billing/plan-change", TARGET)]
    )
    if not dry:
        assert "estimate" in result.stderr.lower()
        assert (
            "1275" in result.stderr
            if mode == "immediate"
            else "2027-10-01" in result.stderr
        )
        if mode == "immediate":
            assert "-725" in result.stderr and "Unused term credit" in result.stderr


@pytest.mark.parametrize(
    "args,path,field",
    [
        (("change", "--discard"), "plan-change", "discarded"),
        (("pending", "dismiss"), "plan-intent", "dismissed"),
    ],
)
def test_idempotent_204_desired_state_without_invented_previous_change(
    billing, args, path, field
):
    queue, calls = billing
    queue.extend([httpx.Response(200, json=BILLING_STATUS), httpx.Response(204)])
    result = invoke(*args, "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[field] is True
    assert calls[-1] == ("DELETE", f"/api/v1/billing/{path}", None)
    assert "none" in result.stderr


WRITE_ARGS = [
    ("cancel",),
    ("resume",),
    ("change", "--discard"),
    ("change", "--tier", "scale", "--interval", "year"),
    ("upgrade", "--tier", "team", "--interval", "month"),
    ("portal",),
    ("pending", "dismiss"),
]


@pytest.mark.parametrize("args", WRITE_ARGS)
def test_non_tty_without_yes_refuses_locally_before_any_http(billing, args):
    _, calls = billing
    result = invoke(*args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "confirmation_required"
    assert calls == []


@pytest.mark.parametrize("args", WRITE_ARGS)
@pytest.mark.parametrize("answer", ["n\n", "", "y\n"])
def test_interactive_accept_decline_eof_never_mutates_before_confirmation(
    billing, monkeypatch, args, answer
):
    queue, calls = billing
    monkeypatch.setattr(plan, "_is_interactive", lambda: True)
    is_change = args[0] == "change" and "--discard" not in args
    queue.append(httpx.Response(200, json=PREVIEW if is_change else BILLING_STATUS))
    # Hosted URL operations use a static effects preview, not status.
    if args[0] in {"portal", "upgrade"}:
        queue.clear()
    response = (
        httpx.Response(204)
        if "--discard" in args or args[0] == "pending"
        else httpx.Response(200, json={"status": "active", "cancel_at": None})
    )
    if is_change:
        response = httpx.Response(200, json={"mode": "immediate", "status": "active"})
    if args[0] in {"portal", "upgrade"}:
        response = httpx.Response(
            200,
            json={
                "portal_url"
                if args[0] == "portal"
                else "checkout_url": "https://billing.example.test/session"
            },
        )
    queue.append(response)
    monkeypatch.setattr(plan.webbrowser, "open", lambda _: True, raising=False)
    result = invoke(*args, input=answer)
    mutations = [
        c for c in calls if c[0] != "GET" and c[1] != "/api/v1/billing/plan-preview"
    ]
    assert len(mutations) == (1 if answer == "y\n" else 0)
    assert result.exit_code == (0 if answer == "y\n" else 2), result.output
    if answer == "y\n":
        json.loads(result.stdout)  # Exactly one JSON value; prompt belongs to stderr.
    else:
        assert result.stdout == ""


@pytest.mark.parametrize(
    "args",
    [
        ("change",),
        ("change", "--tier", "free", "--interval", "month"),
        ("change", "--tier", "team"),
        ("change", "--discard", "--tier", "team"),
        ("change", "--discard", "--interval", "year"),
        ("change", "--discard", "--dry-run"),
        ("upgrade", "--tier", "free", "--interval", "month"),
        ("upgrade", "--tier", "team", "--interval", "week"),
    ],
)
def test_invalid_or_mixed_args_do_not_request(billing, args):
    _, calls = billing
    assert invoke(*args, "--yes").exit_code == 2
    assert calls == []


@pytest.mark.parametrize(
    "command,url_field", [("upgrade", "checkout_url"), ("portal", "portal_url")]
)
@pytest.mark.parametrize(
    "tty,no_browser", [(False, False), (False, True), (True, False), (True, True)]
)
def test_hosted_links_print_and_browser_policy_with_checkout_identity(
    billing, monkeypatch, command, url_field, tty, no_browser
):
    queue, calls = billing
    opened = []
    monkeypatch.setattr(plan, "_is_interactive", lambda: tty)
    monkeypatch.setattr(plan.webbrowser, "open", lambda url: opened.append(url) or True)
    url = "https://billing.brand.example.test/session"
    queue.append(httpx.Response(200, json={url_field: url}))
    args = [command, "--yes", *(["--no-browser"] if no_browser else [])]
    if command == "upgrade":
        args += ["--tier", "team", "--interval", "month"]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[url_field] == url
    assert opened == ([url] if tty and not no_browser else [])
    assert calls[0][:2] == (
        "POST",
        "/api/v1/billing/checkout"
        if command == "upgrade"
        else "/api/v1/billing/portal",
    )
    if command == "upgrade":
        body = calls[0][2]
        UUID(body["client_request_id"])
        assert body.keys() == {"tier", "billing_interval", "client_request_id"}


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/test",
        "https://user:pass@example.test",
        "https://@example.test",
        "https://example.test/\nprivate",
        "https:///missing",
        "https://example.test:bad",
        "https://example.test/\\evil",
    ],
)
def test_invalid_hosted_link_is_never_opened(billing, monkeypatch, url):
    queue, _ = billing
    opened = []
    monkeypatch.setattr(plan, "_is_interactive", lambda: True)
    monkeypatch.setattr(plan.webbrowser, "open", lambda url: opened.append(url))
    queue.append(httpx.Response(200, json={"portal_url": url}))
    result = invoke("portal", "--yes")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert opened == []


def test_browser_failure_keeps_created_url_success(billing, monkeypatch):
    queue, _ = billing
    monkeypatch.setattr(plan, "_is_interactive", lambda: True)

    def failure(url):
        raise OSError("PRIVATE browser detail")

    monkeypatch.setattr(plan.webbrowser, "open", failure)
    queue.append(
        httpx.Response(200, json={"portal_url": "https://billing.example.test/session"})
    )
    result = invoke("portal", "--yes")
    assert result.exit_code == 0
    assert (
        json.loads(result.stdout)["portal_url"]
        == "https://billing.example.test/session"
    )
    assert "PRIVATE" not in result.output
    assert "manually" in result.stderr


@pytest.mark.parametrize(
    "args",
    [*WRITE_ARGS, ("change", "--tier", "scale", "--interval", "year", "--dry-run")],
)
def test_selected_key_is_masked_and_never_falls_back_to_jwt(billing, monkeypatch, args):
    _, calls = billing
    key = "sk_proj_" + "a1" * 32
    monkeypatch.setattr(
        plan,
        "resolve_credential",
        lambda **_: auth.ProjectKeyCredential(api_key=key, source="environment"),
    )
    result = invoke(*args, "--yes")
    assert result.exit_code == 3
    assert key not in result.output
    assert calls == []


@pytest.mark.parametrize("args", WRITE_ARGS)
def test_help_never_resolves_credentials_or_calls_api(billing, monkeypatch, args):
    _, calls = billing
    monkeypatch.setattr(
        plan, "resolve_credential", lambda **_: pytest.fail("help resolved auth")
    )
    command = list(args[:2] if args[0] == "pending" else args[:1])
    assert invoke(*command, "--help").exit_code == 0
    assert calls == []


@pytest.mark.parametrize(
    "args,payload,name,needs_status",
    [
        (
            ("cancel",),
            {"status": "active", "cancel_at": "2027-10-01T00:00:00Z"},
            "cancel",
            True,
        ),
        (("resume",), {"status": "active", "cancel_at": None}, "resume", True),
        (("change", "--discard"), None, "discard", True),
        (("pending", "dismiss"), None, "pending-dismiss", True),
        (
            ("portal",),
            {"portal_url": "https://billing.example.test/session"},
            "portal",
            False,
        ),
        (
            ("upgrade", "--tier", "team", "--interval", "year"),
            {"checkout_url": "https://billing.example.test/session"},
            "upgrade",
            False,
        ),
        (
            ("change", "--tier", "scale", "--interval", "year", "--dry-run"),
            PREVIEW,
            "preview",
            False,
        ),
    ],
)
def test_new_billing_json_golden_contract(billing, args, payload, name, needs_status):
    from pathlib import Path
    from golden.harness import assert_matches_golden

    queue, _ = billing
    if needs_status:
        queue.append(httpx.Response(200, json=BILLING_STATUS))
    queue.append(
        httpx.Response(204) if payload is None else httpx.Response(200, json=payload)
    )
    result = invoke(*args, "--yes")
    assert result.exit_code == 0, result.output
    assert_matches_golden(
        result.stdout, Path(__file__).parent / "golden" / f"plan-{name}.json"
    )


# Review items 4 and 5: approvals bind the real mutation and survive a retry.


@pytest.fixture
def recorded(monkeypatch):
    queue: list[httpx.Response] = []
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        return queue.pop(0)

    monkeypatch.setattr(plan, "resolve_credential", lambda **_: JWT)
    monkeypatch.setattr(plan, "_is_interactive", lambda: False, raising=False)
    monkeypatch.setattr(
        plan,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=lambda _: None
        ),
    )
    fake_keychain(monkeypatch)
    return queue, sent


def test_approval_option_attaches_to_the_plan_change_not_the_preview(recorded):
    queue, sent = recorded
    queue.extend(
        [
            httpx.Response(200, json=PREVIEW),
            httpx.Response(
                200,
                json={
                    "mode": "immediate",
                    "status": "active",
                    "proration_date": None,
                    "effective_at": None,
                },
            ),
        ]
    )

    result = runner.invoke(
        app,
        ["--json", "--approval", APPROVAL_ID, "plan", "change"]
        + ["--tier", "scale", "--interval", "year", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert [(r.url.path, r.headers.get("Solwyn-Approval")) for r in sent] == [
        ("/api/v1/billing/plan-preview", None),
        ("/api/v1/billing/plan-change", APPROVAL_ID),
    ]
