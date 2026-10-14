"""Billing conflict adoption, conservative retries and the sole new MCP read."""

from copy import deepcopy
import json

import anyio
import httpx
from mcp import Client
import pytest

from solwyn_cli import auth
from solwyn_cli.errors import error_from_response
from solwyn_cli.mcp import server
from test_billing_lifecycle import (
    PREVIEW,
    TARGET,
    JWT,
    billing as billing_fixture,
    invoke,
)

from test_mcp_conformance import _install_http

billing = billing_fixture


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("billing_cancel_pending", "plan resume"),
        ("billing_plan_change_scheduled", "plan change --discard"),
        ("billing_plan_payment_failed", "plan portal"),
        ("billing_subscription_active", "plan change"),
        ("billing_subscription_past_due", "plan portal"),
        ("billing_subscription_unusable", "solwyn plan"),
        ("billing_plan_no_change", "plan list"),
    ],
)
def test_billing_conflict_status_is_typed_preserved_and_actionable(code, fragment):
    detail = {"code": code, "message": "Synthetic conflict", "status": "incomplete"}
    error = error_from_response(httpx.Response(409, json={"detail": detail}))
    assert error.detail == detail
    assert fragment in error.hint
    for value in (False, 17, {"private": "PRIVATE"}):
        invalid = error_from_response(
            httpx.Response(409, json={"detail": {**detail, "status": value}})
        )
        assert invalid.code == "invalid_billing_error_response"
        assert invalid.detail is None
    invalid = error_from_response(
        httpx.Response(409, json={"detail": {**detail, "private": "PRIVATE"}})
    )
    assert invalid.code == "invalid_billing_error_response"
    assert "PRIVATE" not in str(invalid)


@pytest.mark.parametrize(
    "path,code,hint",
    [
        ("cancel", "billing_cancel_unavailable", "solwyn plan"),
        ("plan-change", "billing_plan_change_unavailable", "solwyn plan"),
        ("checkout", "billing_checkout_unavailable", "checkout"),
        ("portal", "billing_portal_unavailable", "portal"),
        ("plan-preview", "billing_preview_unavailable", "preview"),
    ],
)
def test_503_billing_hints_no_blind_replay(path, code, hint, billing):
    queue, calls = billing
    if path == "cancel":
        queue.append(
            httpx.Response(200, json=__import__("test_plan_command").BILLING_STATUS)
        )
        args = ["cancel", "--yes"]
    elif path == "plan-change":
        queue.append(httpx.Response(200, json=PREVIEW))
        args = ["change", "--tier", "scale", "--interval", "year", "--yes"]
    elif path == "checkout":
        args = ["upgrade", "--tier", "scale", "--interval", "year", "--yes"]
    elif path == "portal":
        args = ["portal", "--yes"]
    else:
        args = ["change", "--tier", "scale", "--interval", "year", "--dry-run"]
    queue.append(
        httpx.Response(
            503,
            json={"detail": {"code": code, "message": "Synthetic unavailable"}},
            headers={"Retry-After": "0"},
        )
    )
    result = invoke(*args)
    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr.splitlines()[-1])
    assert hint in error["hint"]
    assert ("unknown" in error["hint"]) == (path != "plan-preview")
    assert len([c for c in calls if c[1] == f"/api/v1/billing/{path}"]) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_preview_exact_read_policy_jwt_no_project_and_units(monkeypatch, mode):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[httpx.Response(200, json=PREVIEW)],
        credentials=[JWT],
        project=None,
    )

    async def exercise():
        async with Client(
            server.create_server(
                launch_options=server.McpLaunchOptions(profile="read")
            ),
            mode=mode,
            cache=None,
        ) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 31
            tool = next(t for t in tools if t.name == "plan_preview")
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is True
            assert set(tool.input_schema["properties"]) == {"tier", "interval"}
            result = await client.call_tool(
                "plan_preview", {"tier": "scale", "interval": "year"}
            )
            assert result.structured_content["data"] == PREVIEW
            assert not result.is_error

    anyio.run(exercise)
    assert requests[0][:2] == ("POST", "/api/v1/billing/plan-preview")
    assert requests[0][2]["json"] == TARGET
    for profile, count in (("read", 31), ("guardrails", 35)):
        catalog = server.get_tool_catalog(profile=profile)
        assert len(catalog) == count
        assert (
            next(
                t for t in catalog if t.definition.name == "plan_preview"
            ).credential_requirement
            == "jwt"
        )
        assert {
            t.definition.name
            for t in catalog
            if not t.definition.annotations.read_only_hint
        } == (
            set()
            if profile == "read"
            else {"budget_check", "budget_check_batch", "budget_confirm", "record_call"}
        )


@pytest.mark.parametrize(
    "code,status",
    [
        ("permission_required", 403),
        ("billing_plan_no_change", 409),
        ("billing_preview_unavailable", 503),
    ],
)
def test_mcp_preview_fixed_errors_private_detail_never_reflected(
    monkeypatch, code, status
):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(
                status,
                json={
                    "detail": {
                        "code": code,
                        "message": "PRIVATE STRIPE",
                        "private": "PRIVATE",
                    }
                },
            )
        ],
        credentials=[JWT],
        project=None,
    )

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            result = await client.call_tool(
                "plan_preview", {"tier": "team", "interval": "month"}
            )
            assert result.is_error
            assert "PRIVATE" not in str(result)

    anyio.run(exercise)
    assert len(requests) == 1


@pytest.mark.parametrize("bad", ["extra", "missing", "line", "target"])
def test_preview_fails_closed_and_never_changes(billing, bad):
    queue, calls = billing
    preview = deepcopy(PREVIEW)
    if bad == "extra":
        preview["private"] = "PRIVATE"
    elif bad == "missing":
        del preview["effective_at"]
    elif bad == "line":
        preview["lines"][0]["private"] = "PRIVATE"
    else:
        preview["target_tier"] = "team"
    queue.append(httpx.Response(200, json=preview))
    result = invoke("change", "--tier", "scale", "--interval", "year", "--yes")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "PRIVATE" not in result.output
    assert len(calls) == 1


@pytest.mark.parametrize(
    "args",
    [
        ("upgrade", "--tier", "team", "--interval", "year", "--yes"),
        ("portal", "--yes"),
        ("change", "--tier", "scale", "--interval", "year", "--dry-run"),
    ],
)
def test_transport_failure_is_not_replayed_and_classifies_read_vs_write(billing, args):
    queue, calls = billing
    queue.append(httpx.ReadTimeout("PRIVATE transport"))
    result = invoke(*args)
    assert result.exit_code == 1
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "transport_error"
    assert ("unknown" in error["hint"]) == (args[0] != "change")
    assert "PRIVATE" not in result.output
    assert len(calls) == 1


@pytest.mark.parametrize("first_status", [401, 429])
def test_checkout_rejection_retry_preserves_request_identity_and_refresh_is_mocked(
    billing, monkeypatch, first_status
):
    queue, calls = billing
    queue.append(httpx.Response(first_status, headers={"Retry-After": "0"}))
    if first_status == 401:
        queue.append(
            httpx.Response(
                200,
                json={
                    "access_token": "synthetic-new",
                    "refresh_token": "synthetic-new-refresh",
                },
            )
        )
    queue.append(
        httpx.Response(
            200, json={"checkout_url": "https://billing.example.test/session"}
        )
    )
    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    result = invoke("upgrade", "--tier", "team", "--interval", "year", "--yes")
    assert result.exit_code == 0, result.output
    posts = [c for c in calls if c[1] == "/api/v1/billing/checkout"]
    assert len(posts) == 2 and posts[0][2] == posts[1][2]
    assert len(calls) == (3 if first_status == 401 else 2)


@pytest.mark.parametrize("role", ["admin", "member"])
def test_core_owner_refusal_propagates_without_privilege_workaround(billing, role):
    queue, calls = billing
    queue.append(
        httpx.Response(
            403,
            json={
                "detail": {
                    "code": "permission_required",
                    "message": "Owner required",
                    "role": role,
                }
            },
        )
    )
    result = invoke("upgrade", "--tier", "team", "--interval", "year", "--yes")
    assert result.exit_code == 3
    assert len(calls) == 1
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "permission_required"


@pytest.mark.parametrize(
    "body", [httpx.Response(200, json={}), httpx.Response(204, content=b"{}")]
)
def test_bodyless_discard_rejects_wrong_status_or_body(billing, body):
    queue, calls = billing
    from test_plan_command import BILLING_STATUS

    queue.extend([httpx.Response(200, json=BILLING_STATUS), body])
    result = invoke("change", "--discard", "--yes")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert len(calls) == 2


def test_mcp_selected_key_preview_refusal_before_http(monkeypatch):
    requests, _, _ = _install_http(
        monkeypatch,
        responses=[],
        credentials=[
            auth.ProjectKeyCredential(
                api_key="sk_proj_" + "a1" * 32, source="environment"
            )
        ],
        project=None,
    )

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            result = await client.call_tool(
                "plan_preview", {"tier": "team", "interval": "month"}
            )
            assert result.is_error
            assert "JWT" in result.structured_content["error"]["hint"]

    anyio.run(exercise)
    assert requests == []


def test_bare_plan_503_stays_read_guidance(billing):
    queue, calls = billing
    queue.append(httpx.Response(503, json={"detail": "busy"}))
    result = invoke()
    assert result.exit_code == 1
    assert json.loads(result.stderr)["hint"] == "Service is busy; retry in a moment."
    assert len(calls) == 1


def test_mcp_preview_transport_failure_has_fixed_read_guidance(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("PRIVATE transport")

    monkeypatch.setattr(auth, "resolve_credential", lambda: JWT)
    from solwyn_cli.client import SolwynClient

    monkeypatch.setattr(
        server,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            api_url=kwargs["api_url"],
            credential=kwargs["credential"],
            transport=httpx.MockTransport(handler),
        ),
    )

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            result = await client.call_tool(
                "plan_preview", {"tier": "team", "interval": "month"}
            )
            assert result.is_error
            assert "read-only" in result.structured_content["error"]["hint"]
            assert "PRIVATE" not in str(result)

    anyio.run(exercise)


@pytest.mark.parametrize("fence", ["http", "result", "deadline"])
def test_native_mcp_preview_uses_existing_response_result_and_deadline_fences(
    monkeypatch, fence
):
    from contextlib import asynccontextmanager
    from solwyn_cli import _mcp_operation as operation

    calls = []
    preview = deepcopy(PREVIEW)
    if fence != "deadline":
        preview["lines"][0]["description"] = (
            "x" * operation.MAX_RESPONSE_BYTES if fence == "http" else "🧪" * 200_000
        )
    response = httpx.Response(200, json=preview)
    if fence == "result":
        assert len(response.content) < operation.MAX_RESPONSE_BYTES

    async def handler(request):
        calls.append(request)
        if fence == "deadline":
            await anyio.sleep(10)
        return response

    original = operation.operation_scope

    @asynccontextmanager
    async def scope():
        async with original(transport=httpx.MockTransport(handler)) as control:
            yield control

    monkeypatch.setattr(operation, "operation_scope", scope)
    if fence == "deadline":
        monkeypatch.setattr(operation, "DEADLINE_SECONDS", 1)
    monkeypatch.setattr(auth, "resolve_credential", lambda: JWT)

    async def exercise():
        # Warm a worker before starting the timed operation; startup is not
        # the condition under test. All unchanged deadline tests stay intact.
        await anyio.to_thread.run_sync(lambda: None)
        async with Client(server.create_server(), cache=None) as client:
            return await client.call_tool(
                "plan_preview", {"tier": "scale", "interval": "year"}
            )

    result = anyio.run(exercise)
    assert result.is_error
    assert result.structured_content["data"] is None
    assert (
        result.structured_content["error"]["code"]
        == {
            "http": "response_size_limit",
            "result": "result_size_limit",
            "deadline": "operation_deadline",
        }[fence]
    )
    assert len(calls) == 1


def test_checkout_429_retry_budget_is_shared_across_jwt_refresh(billing, monkeypatch):
    queue, calls = billing
    queue.extend(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(401),
            httpx.Response(
                200,
                json={
                    "access_token": "synthetic-new",
                    "refresh_token": "synthetic-new-refresh",
                },
            ),
            httpx.Response(429, headers={"Retry-After": "0"}),
        ]
    )
    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    result = invoke("upgrade", "--tier", "team", "--interval", "year", "--yes")
    assert result.exit_code == 1
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "rate_limited"
    posts = [c for c in calls if c[1] == "/api/v1/billing/checkout"]
    assert len(calls) == 4 and len(posts) == 3
    assert posts[0][2] == posts[1][2] == posts[2][2]


@pytest.mark.parametrize("status_value", ["incomplete", None, "absent"])
def test_cli_conflict_status_preserves_presence_and_null(billing, status_value):
    queue, _ = billing
    detail = {"code": "billing_subscription_active", "message": "Active subscription"}
    if status_value != "absent":
        detail["status"] = status_value
    queue.append(httpx.Response(409, json={"detail": detail}))
    result = invoke("upgrade", "--tier", "team", "--interval", "month", "--yes")
    assert result.exit_code == 1
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["detail"] == detail
    assert "plan change" in error["hint"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "active"},
        {"status": "active", "cancel_at": None, "unexpected": "PRIVATE"},
        {"status": "active", "cancel_at": "invalid-date"},
    ],
)
def test_cancel_strict_response_no_fabricated_success(billing, payload):
    from test_plan_command import BILLING_STATUS

    queue, calls = billing
    queue.extend(
        [httpx.Response(200, json=BILLING_STATUS), httpx.Response(200, json=payload)]
    )
    result = invoke("cancel", "--yes")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "PRIVATE" not in result.output
    assert len(calls) == 2


def test_unknown_billing_code_fails_closed_instead_of_dumping_new_private_detail():
    error = error_from_response(
        httpx.Response(
            409,
            json={
                "detail": {
                    "code": "billing_future_conflict",
                    "message": "PRIVATE Stripe",
                    "private": {"payload": "PRIVATE"},
                }
            },
        )
    )
    assert error.code == "invalid_billing_error_response"
    assert error.detail is None
    assert "PRIVATE" not in str(error)


def test_existing_agent_run_authentication_error_remains_intact(monkeypatch):
    requests, _, _ = _install_http(monkeypatch, responses=[], credentials=[None])

    async def exercise():
        async with Client(server.create_server(), cache=None) as client:
            result = await client.call_tool("get_agent_run", {"run": "synthetic-run"})
            assert (
                result.structured_content["error"]["code"] == "authentication_required"
            )

    anyio.run(exercise)
    assert requests == []
