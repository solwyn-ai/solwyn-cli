"""Offline regressions for capacity shedding and membership recovery."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import account, login
from solwyn_cli.commands._record import build_event, record_best_effort
from solwyn_cli.commands._cost_advice_models import SimulationCaveats
from solwyn_cli.errors import ApiError
from solwyn_cli.main import app
from test_mcp_conformance import _call_tool, _install_http

KEY = auth.ProjectKeyCredential(api_key="sk_proj_" + "a1" * 32, source="environment")
JWT = auth.JwtCredential(
    access_token="synthetic-access", refresh_token="synthetic-refresh"
)
BUSY = "Budget admission is busy; retry shortly"
NOW = datetime(2026, 10, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/projects/p/costs"),
        ("GET", "/api/v1/account/costs"),
        ("POST", "/api/v1/metadata/ingest"),
    ],
)
@pytest.mark.parametrize(
    "retry_after,delay",
    [
        ("1", 1.0),
        ("100", 10.0),
        ("-1", 0.0),
        ("bad", 0.0),
        (format_datetime(NOW + timedelta(seconds=30)), 10.0),
    ],
)
def test_busy_retry_is_bounded_and_reuses_request(method, path, retry_after, delay):
    requests, waits = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            503, headers={"Retry-After": retry_after}, json={"detail": "busy"}
        )

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=waits.append,
        clock=lambda: NOW,
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request(method, path, json={"call_id": "synthetic"})
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert waits == [delay]
    assert failure.value.code == "service_busy"
    assert "retry" in failure.value.hint.lower()


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/budgets/check"),
        ("POST", "/api/v1/budgets/check/batch"),
        ("POST", "/api/v1/budgets/confirm"),
        ("POST", "/api/v1/billing/plan-change"),
        ("DELETE", "/api/v1/accounts/me/members/member"),
        ("POST", "/api/v1/projects/p/costs/simulate"),
    ],
)
def test_busy_does_not_retry_other_writes(method, path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            503, headers={"Retry-After": "1"}, json={"detail": "busy"}
        )

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: pytest.fail("write must not sleep"),
    ) as client:
        with pytest.raises(ApiError):
            client.request(method, path)
    assert len(requests) == 1


def test_busy_without_retry_after_is_not_automatically_replayed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"detail": "busy"})

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ApiError):
            client.request("GET", "/api/v1/overview")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "detail",
    [BUSY, "Budget backend temporarily unavailable; retry"],
)
def test_legacy_budget_busy_never_guarantees_no_reservation(detail):
    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                503, headers={"Retry-After": "1"}, json={"detail": detail}
            )
        ),
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("POST", "/api/v1/budgets/check/batch")
    assert failure.value.hint is not None
    assert "Reservation state within the failed chunk is unknown" in failure.value.hint
    assert "safe to retry" not in failure.value.hint


@pytest.mark.parametrize("busy", [True, False])
def test_best_effort_recording_distinguishes_unaccepted_from_transport(busy):
    def handler(request):
        if busy:
            return httpx.Response(
                503, headers={"Retry-After": "1"}, json={"detail": BUSY}
            )
        raise httpx.ReadError("private failure")

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        result = record_best_effort(
            client=client, event=build_event(model="local", provider="ollama")
        )
    assert result.status == ("not_recorded" if busy else "failed")


def test_membership_revoked_does_not_refresh_or_suggest_relogin():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"detail": "account_membership_revoked"})

    with SolwynClient(
        api_url="https://example.test",
        credential=JWT,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("GET", "/api/v1/account/me")
    assert len(requests) == 1
    assert failure.value.code == "account_membership_revoked"
    assert "solwyn account recover" in failure.value.hint


@pytest.mark.parametrize("succeed", [True, False])
def test_login_retries_capacity_once_and_uses_generic_exit(monkeypatch, succeed):
    requests, waits, stored = [], [], []

    def handler(request):
        requests.append(request)
        if succeed and len(requests) == 2:
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                },
            )
        return httpx.Response(
            503,
            headers={"Retry-After": "100"},
            json={
                "detail": {
                    "code": "auth_capacity_exceeded",
                    "message": "Authentication is busy.",
                }
            },
        )

    monkeypatch.setattr(
        login,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=waits.append
        ),
    )
    monkeypatch.setattr(login, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(
        login.typer,
        "prompt",
        lambda label, **kwargs: (
            "person@example.com" if label == "Email" else "synthetic-password"
        ),
    )
    monkeypatch.setattr(login, "store_jwt_pair", lambda *pair: stored.append(pair))
    result = CliRunner().invoke(app, ["--json", "login"])
    assert result.exit_code == (0 if succeed else 1), result.exception
    assert len(requests) == 2
    assert waits == [10.0]
    assert len(stored) == int(succeed)
    assert requests[0].content == requests[1].content
    assert "authorization" not in requests[0].headers
    if not succeed:
        error = json.loads(result.stderr)
        assert error["code"] == "auth_capacity_exceeded"
        assert "retry" in error["hint"].lower()
    assert "synthetic-password" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "payload,exit_code",
    [
        ({"account_id": "account_recovered"}, 0),
        ({"account_id": "account_recovered", "unexpected": True}, 1),
        ({}, 1),
    ],
)
def test_account_recover_posts_jwt_and_parses_fail_closed(
    monkeypatch, payload, exit_code
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(account, "resolve_credential", lambda: JWT)
    monkeypatch.setattr(
        account,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(app, ["--json", "account", "recover"])
    assert result.exit_code == exit_code, result.exception
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/accounts/me/personal"
    assert requests[0].content == b""
    assert requests[0].headers["authorization"] == "Bearer synthetic-access"
    if exit_code == 0:
        assert json.loads(result.stdout) == {
            "schema": "solwyn.account.recover.v1",
            **payload,
        }
        assert_matches_golden(result.stdout, Path("tests/golden/account-recover.json"))


def test_account_recover_rejects_project_key_before_request(monkeypatch):
    monkeypatch.setattr(account, "resolve_credential", lambda: KEY)
    monkeypatch.setattr(
        account, "SolwynClient", lambda **kwargs: pytest.fail("no request")
    )
    result = CliRunner().invoke(app, ["--json", "account", "recover"])
    assert result.exit_code == 3
    assert "Unset SOLWYN_API_KEY" in json.loads(result.stderr)["hint"]


def test_simulation_fallback_matches_core_pricing():
    assert SimulationCaveats().pricing_version == "2026-09-23"


@pytest.mark.parametrize(
    "tool,args,detail,guarantee",
    [
        ("get_cost_total", {"project_id": "p"}, "busy", None),
        (
            "budget_check",
            {"model": "gpt-4o", "provider": "openai", "estimated_input_tokens": 1},
            BUSY,
            "No reservation",
        ),
        (
            "record_call",
            {"model": "local", "provider": "ollama"},
            BUSY,
            "Nothing recorded",
        ),
    ],
)
def test_mcp_busy_is_fixed_safe_code_with_retry_hint(
    monkeypatch, tool, args, detail, guarantee
):
    requests = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(503, headers={"Retry-After": "1"}, json={"detail": detail})
            for _ in range(1 if tool == "budget_check" else 2)
        ],
        credentials=[KEY],
    )[0]
    result = anyio.run(_call_tool, tool, args)
    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "service_busy"
    assert "retry" in error["hint"].lower()
    if guarantee:
        assert guarantee in error["hint"]
    else:
        assert "Nothing recorded" not in error["hint"]
        assert "Nothing reserved" not in error["hint"]
    assert len(requests) == (1 if tool == "budget_check" else 2)
    if tool == "record_call":
        assert result.structured_content["recovery"]["recording"] == "not_recorded"


@pytest.mark.parametrize("status,recording", [(503, "unknown"), (None, "unknown")])
def test_mcp_recording_preserves_transport_uncertainty(monkeypatch, status, recording):
    if status is None:
        from solwyn_cli.mcp import server
        from contextlib import contextmanager

        def disconnect(_request):
            raise httpx.ReadError("private")

        @contextmanager
        def client_factory(_options):
            with SolwynClient(
                api_url="https://example.test",
                credential=KEY,
                transport=httpx.MockTransport(disconnect),
            ) as client:
                yield None, client

        monkeypatch.setattr(server, "tool_call_client", client_factory)
    else:
        _install_http(
            monkeypatch,
            responses=[httpx.Response(503, json={"detail": "busy"})],
            credentials=[KEY],
        )
    result = anyio.run(
        _call_tool, "record_call", {"model": "local", "provider": "ollama"}
    )
    assert result.is_error
    assert result.structured_content["recovery"]["recording"] == recording


@pytest.mark.parametrize(
    "detail,state,code",
    [
        (BUSY, "not_submitted", "service_busy"),
        (
            {"code": "outcome_unavailable", "operation_id": "synthetic"},
            "unknown",
            "outcome_unavailable",
        ),
    ],
)
def test_mcp_batch_shedding_does_not_erase_ambiguous_journal_state(
    monkeypatch, detail, state, code
):
    from mcp_batch_fixtures import ID

    requests, _, _ = _install_http(
        monkeypatch,
        responses=[
            httpx.Response(503, headers={"Retry-After": "0"}, json={"detail": detail})
        ],
        credentials=[KEY],
    )
    result = anyio.run(
        _call_tool,
        "budget_check_batch",
        {
            "operation_id": ID,
            "checks": [
                {"model": "gpt-4o", "provider": "openai", "estimated_input_tokens": 1}
            ],
        },
    )
    assert result.is_error
    assert result.structured_content["recovery"]["state"] == state
    assert result.structured_content["error"]["code"] == code
    assert len(requests) == 1


def test_membership_revocation_after_refresh_keeps_recovery_hint(monkeypatch):
    responses = [
        httpx.Response(401, json={"detail": "expired"}),
        httpx.Response(
            200,
            json={
                "access_token": "synthetic-new",
                "refresh_token": "synthetic-refresh-new",
            },
        ),
        httpx.Response(401, json={"detail": "account_membership_revoked"}),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    with SolwynClient(
        api_url="https://example.test",
        credential=JWT,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("GET", "/api/v1/account/me")
    assert failure.value.code == "account_membership_revoked"
    assert "solwyn account recover" in failure.value.hint
    assert len(requests) == 3


def test_account_recovery_refreshes_expired_jwt_without_membership_read(monkeypatch):
    responses = [
        httpx.Response(401, json={"detail": "expired"}),
        httpx.Response(
            200,
            json={
                "access_token": "synthetic-new",
                "refresh_token": "synthetic-refresh-new",
            },
        ),
        httpx.Response(200, json={"account_id": "recovered"}),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    monkeypatch.setattr(account, "resolve_credential", lambda: JWT)
    monkeypatch.setattr(
        account,
        "SolwynClient",
        lambda **kwargs: SolwynClient(**kwargs, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(app, ["--json", "account", "recover"])
    assert result.exit_code == 0, result.exception
    assert [request.url.path for request in requests] == [
        "/api/v1/accounts/me/personal",
        "/api/v1/auth/refresh",
        "/api/v1/accounts/me/personal",
    ]
    assert requests[-1].headers["authorization"] == "Bearer synthetic-new"


def test_get_busy_retry_budget_survives_jwt_refresh(monkeypatch):
    responses = [
        httpx.Response(503, headers={"Retry-After": "0"}),
        httpx.Response(401),
        httpx.Response(
            200,
            json={
                "access_token": "synthetic-new",
                "refresh_token": "synthetic-refresh-new",
            },
        ),
        httpx.Response(503, headers={"Retry-After": "0"}),
    ]
    requests, waits = [], []

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        auth,
        "store_jwt_pair",
        lambda access, refresh: auth.JwtCredential(
            access_token=access, refresh_token=refresh
        ),
    )
    with SolwynClient(
        api_url="https://example.test",
        credential=JWT,
        transport=httpx.MockTransport(handler),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError):
            client.request("GET", "/api/v1/overview")
    assert len(requests) == 4
    assert waits == [0.0]


def test_rate_limit_and_busy_share_one_retry_budget():
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(503, headers={"Retry-After": "1"}),
    ]
    waits = []
    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(lambda _: responses.pop(0)),
        sleeper=waits.append,
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("GET", "/api/v1/overview")
    assert failure.value.code == "service_busy"
    assert responses == []
    assert waits == [0.0]


def test_ingest_busy_retry_success_keeps_complete_event():
    from solwyn_cli.commands._record import request_ingest

    requests = []
    event = build_event(model="local", provider="ollama")

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(202, json={"ingested": 1})

    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        result = request_ingest(client=client, events=[event])
    assert result.status == "ingested"
    assert result.call_ids == [event.call_id]
    assert len(requests) == 2
    assert requests[0].content == requests[1].content


@pytest.mark.parametrize("detail", ["busy", {"code": "other_busy"}])
def test_login_does_not_retry_unrelated_503(detail):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            503, headers={"Retry-After": "0"}, json={"detail": detail}
        )

    with SolwynClient(
        api_url="https://example.test",
        credential=None,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ApiError):
            client.request_unauthenticated("POST", "/api/v1/auth/login")
    assert len(requests) == 1


def test_tally_busy_has_no_absence_guarantee():
    with SolwynClient(
        api_url="https://example.test",
        credential=KEY,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                503, headers={"Retry-After": "0"}, json={"detail": BUSY}
            )
        ),
        sleeper=lambda _: None,
    ) as client:
        with pytest.raises(ApiError) as failure:
            client.request("GET", "/api/v1/projects/p/budget/uncounted")
    assert "Nothing reserved" not in failure.value.hint
    assert "Nothing recorded" not in failure.value.hint


@pytest.mark.parametrize(
    "command",
    [
        ["costs", "export", "--format", "json"],
        ["costs", "simulate", "--provider", "openai", "--model", "gpt-5"],
    ],
)
def test_oversized_export_and_simulation_stay_usage_errors_without_partial_output(
    monkeypatch, command
):
    from solwyn_cli.commands import costs, cost_pricing

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            422,
            json={
                "detail": "Operation exceeds its event limit; choose a smaller time window. No partial financial result was returned."
            },
        )

    def factory(**kwargs):
        return SolwynClient(**kwargs, transport=httpx.MockTransport(handler))

    for module in (costs, cost_pricing):
        monkeypatch.setattr(module, "SolwynClient", factory)
        monkeypatch.setattr(module, "resolve_credential", lambda: KEY)
    monkeypatch.setattr(costs, "get_capabilities", lambda **kwargs: None)
    result = CliRunner().invoke(app, ["--json", "--project", "p", *command])
    assert result.exit_code == 2, result.exception
    assert result.stdout == ""
    assert "No partial financial result" in json.loads(result.stderr)["message"]
    assert len(requests) == 1


def test_record_cli_busy_is_known_unaccepted_and_preserves_identity(monkeypatch):
    from solwyn_cli.commands import record

    requests = []
    call_id = "12345678-1234-1234-1234-123456789abc"

    def handler(request):
        requests.append(request)
        return httpx.Response(503, headers={"Retry-After": "1"}, json={"detail": BUSY})

    monkeypatch.setattr(
        record,
        "SolwynClient",
        lambda **kwargs: SolwynClient(
            **kwargs, transport=httpx.MockTransport(handler), sleeper=lambda _: None
        ),
    )
    monkeypatch.setattr(record, "resolve_credential", lambda: KEY)
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "record",
            "--model",
            "local",
            "--provider",
            "ollama",
            "--call-id",
            call_id,
        ],
    )
    assert result.exit_code == 1, result.exception
    assert json.loads(result.stdout)["status"] == "not_recorded"
    assert json.loads(result.stdout)["call_ids"] == [call_id]
    assert json.loads(result.stderr)["code"] == "service_busy"
    assert "Nothing recorded" in json.loads(result.stderr)["hint"]
    assert len(requests) == 2


def test_legacy_batch_completed_chunks_keep_failed_chunk_uncertain():
    from solwyn_cli.commands.budget import BudgetCheckBatchData, BudgetCheckBatchFailure
    from solwyn_cli.errors import error_from_response

    busy = error_from_response(
        httpx.Response(503, headers={"Retry-After": "1"}, json={"detail": BUSY})
    )
    ambiguous = error_from_response(
        httpx.Response(
            503, json={"detail": "Budget backend temporarily unavailable; retry"}
        )
    )
    data = BudgetCheckBatchData(results=[], failed_chunk_start=32, total_checks=33)
    assert "No reservation" not in BudgetCheckBatchFailure(busy, data).hint
    assert "unknown" in BudgetCheckBatchFailure(busy, data).hint
    assert "unknown" in BudgetCheckBatchFailure(ambiguous, data).hint
