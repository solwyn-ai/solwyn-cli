"""Browser approval hand-off inside the shared client."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from login_fixtures import login_credential
from solwyn_cli.approvals import ApprovalSettings, approval_settings
from solwyn_cli.client import SolwynClient
from solwyn_cli.errors import APPROVAL_USED_HINT, ApiError
from solwyn_cli.exit_codes import ExitCode, exit_code_for_error


API_URL = "https://api.solwyn.ai"
APPROVAL_ID = "0b7d2c4e-0000-4000-8000-000000000001"
APPROVAL_URL = f"https://app.solwyn.ai/approve/{APPROVAL_ID}"
START = datetime(2026, 10, 30, 14, 20, tzinfo=UTC)
BUDGET_PATH = "/api/v1/projects/p_1/budget"
# Raw query bytes the binding hashes: order, repeats and %20 must survive.
RAW_QUERY = "q=a%20b&key=a&key=b"


class FakeClock:
    def __init__(self) -> None:
        self.current = START
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


def _approval(status: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": APPROVAL_ID,
        "status": status,
        "action": "budget.update",
        "title": "Change the budget for project checkout",
        "reasons": ["raises_cap", "weakens_enforcement"],
        "changes": [
            {
                "field": "limit_usd",
                "label": "Budget limit",
                "before": "1000.00",
                "after": "1000000.00",
                "effect": "raises_cap",
            },
            {
                "field": "mode",
                "label": "Enforcement",
                "before": "hard_deny",
                "after": "alert_only",
                "effect": "weakens_enforcement",
            },
        ],
        "request": {"method": "PUT", "path": BUDGET_PATH, "query": RAW_QUERY},
        "credential": {"id": "c", "label": "dev-laptop"},
        "project": {"id": "p_1", "name": "checkout"},
        "request_ip": "203.0.113.7",
        "created_at": START.isoformat(),
        "expires_at": (START + timedelta(minutes=10)).isoformat(),
    }
    body.update(overrides)
    return body


def _approval_required(url: str = APPROVAL_URL) -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "detail": {
                "code": "approval_required",
                "message": "This change needs your approval in the browser.",
                "approval_id": APPROVAL_ID,
                "approval_url": url,
                "expires_at": (START + timedelta(minutes=10)).isoformat(),
                "action": "budget.update",
                "reasons": ["raises_cap", "weakens_enforcement"],
            }
        },
    )


class Server:
    """Answers writes from a queue and approval reads from a status list."""

    def __init__(
        self,
        writes: list[httpx.Response],
        statuses: list[str] | None = None,
    ) -> None:
        self.writes = writes
        self.statuses = statuses or []
        self.requests: list[httpx.Request] = []
        self.approval_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == f"/api/v1/cli/approvals/{APPROVAL_ID}":
            index = min(self.approval_reads, len(self.statuses) - 1)
            self.approval_reads += 1
            return httpx.Response(200, json=_approval(self.statuses[index]))
        return self.writes.pop(0)

    @property
    def sent_writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/cli/approvals/" not in r.url.path]


def _run(
    server: Server,
    *,
    wait: float = 600,
    approval_id: str | None = None,
    clock: FakeClock | None = None,
    call: Callable[[SolwynClient], httpx.Response] | None = None,
) -> tuple[httpx.Response, list[str]]:
    clock = clock or FakeClock()
    written: list[str] = []
    settings = ApprovalSettings(
        wait_seconds=wait, approval_id=approval_id, write=written.append
    )
    with (
        approval_settings(settings),
        SolwynClient(
            api_url=API_URL,
            credential=login_credential(),
            transport=httpx.MockTransport(server),
            sleeper=clock.sleep,
            clock=clock.now,
        ) as client,
    ):
        response = (
            call(client)
            if call is not None
            else client.request(
                "PUT",
                f"{BUDGET_PATH}?{RAW_QUERY}",
                content=b'{"mode": "alert_only",  "limit_usd": 1000000.0}',
                headers={"Content-Type": "application/json"},
            )
        )
    return response, written


def test_approved_change_is_resent_once_with_the_exact_prepared_bytes() -> None:
    server = Server(
        writes=[_approval_required(), httpx.Response(200, json={"ok": True})],
        statuses=["pending", "pending", "approved"],
    )
    clock = FakeClock()

    response, written = _run(server, clock=clock)

    first, retry = server.sent_writes
    assert response.json() == {"ok": True}
    assert retry.method == first.method == "PUT"
    assert retry.url.raw_path == first.url.raw_path
    assert retry.url.raw_path == f"{BUDGET_PATH}?{RAW_QUERY}".encode()
    assert retry.content == first.content
    assert retry.headers["Content-Type"] == "application/json"
    assert retry.headers["Authorization"] == first.headers["Authorization"]
    assert "Solwyn-Approval" not in first.headers
    assert retry.headers["Solwyn-Approval"] == APPROVAL_ID
    assert server.approval_reads == 3
    assert clock.sleeps == [3.0, 3.0]
    text = "".join(written)
    assert "This change needs your approval in the browser:" in text
    assert APPROVAL_URL in text
    assert "Change the budget for project checkout" in text
    assert "Budget limit: 1000.00 -> 1000000.00 (raises the cap)" in text
    assert "Enforcement: hard_deny -> alert_only (weakens enforcement)" in text
    assert "expires in 10 minutes, Ctrl-C to stop" in text
    assert text.endswith("Approved. Applying the change.\n")


def test_get_requests_never_hand_off() -> None:
    server = Server(writes=[_approval_required()], statuses=["approved"])

    with pytest.raises(ApiError) as exc_info:
        _run(server, call=lambda client: client.request("GET", BUDGET_PATH))

    assert exc_info.value.code == "approval_required"
    assert server.approval_reads == 0
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [("denied", "approval_denied"), ("expired", "approval_expired")],
)
def test_denied_or_expired_approval_sends_nothing_else(status: str, code: str) -> None:
    server = Server(writes=[_approval_required()], statuses=["pending", status])

    with pytest.raises(ApiError) as exc_info:
        _run(server)

    assert exc_info.value.code == code
    assert exit_code_for_error(exc_info.value) is ExitCode.APPROVAL_REQUIRED
    assert len(server.sent_writes) == 1


def test_pending_past_its_deadline_counts_as_expired() -> None:
    expired = _approval(
        "pending", expires_at=(START - timedelta(seconds=1)).isoformat()
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if "/cli/approvals/" in request.url.path:
            return httpx.Response(200, json=expired)
        return _approval_required()

    with pytest.raises(ApiError) as exc_info:
        _run(handle)  # type: ignore[arg-type]

    assert exc_info.value.code == "approval_expired"


@pytest.mark.parametrize(("wait", "reads"), [(0, 1), (10, 4)])
def test_wait_timeout_exits_pending_with_the_resume_instructions(
    wait: float, reads: int
) -> None:
    server = Server(writes=[_approval_required()], statuses=["pending"])

    with pytest.raises(ApiError) as exc_info:
        _run(server, wait=wait)

    error = exc_info.value
    assert error.code == "approval_pending"
    assert exit_code_for_error(error) is ExitCode.APPROVAL_REQUIRED
    assert error.message == (
        f"Still waiting for approval {APPROVAL_ID}. After approving, run the same "
        f"command again with --approval {APPROVAL_ID}."
    )
    assert error.detail == {
        "approval_id": APPROVAL_ID,
        "approval_url": APPROVAL_URL,
        "expires_at": "2026-10-30T14:30:00Z",
    }
    assert server.approval_reads == reads
    assert len(server.sent_writes) == 1


def test_mismatch_after_approval_explains_and_does_not_retry_again() -> None:
    server = Server(
        writes=[
            _approval_required(),
            httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "approval_invalid",
                        "message": "Approval does not match.",
                        "reason": "mismatch",
                    }
                },
            ),
        ],
        statuses=["approved"],
    )

    with pytest.raises(ApiError) as exc_info:
        _run(server)

    assert exc_info.value.code == "approval_invalid"
    assert exc_info.value.message == "The request changed since it was approved."
    assert exc_info.value.hint == "Run the command again to request a new approval."
    assert exit_code_for_error(exc_info.value) is ExitCode.APPROVAL_REQUIRED
    assert len(server.sent_writes) == 2


def test_handler_failure_after_consumption_says_the_approval_was_used() -> None:
    server = Server(
        writes=[
            _approval_required(),
            httpx.Response(
                422, json={"detail": {"code": "validation_error", "message": "Bad."}}
            ),
        ],
        statuses=["approved"],
    )

    with pytest.raises(ApiError) as exc_info:
        _run(server)

    assert exc_info.value.status_code == 422
    assert exc_info.value.hint is not None
    assert exc_info.value.hint.endswith(APPROVAL_USED_HINT)
    assert len(server.sent_writes) == 2


def test_approval_option_attaches_to_the_first_non_get_only() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    def call(client: SolwynClient) -> httpx.Response:
        client.request("GET", BUDGET_PATH)
        client.request("PUT", BUDGET_PATH, json={"a": 1})
        return client.request("POST", "/api/v1/projects", json={"b": 2})

    _run(handle, approval_id=APPROVAL_ID, call=call)  # type: ignore[arg-type]

    assert [r.headers.get("Solwyn-Approval") for r in requests] == [
        None,
        APPROVAL_ID,
        None,
    ]


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("pending", "The approval is still pending."),
        ("denied", "The approval was denied in the browser."),
        ("expired", "The approval expired."),
        ("consumed", "The approval was already used."),
        ("not_found", "The approval was not found for this login."),
    ],
)
def test_attached_approval_rejections_are_explained(reason: str, message: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "approval_invalid",
                    "message": "Approval rejected.",
                    "reason": reason,
                }
            },
        )

    with pytest.raises(ApiError) as exc_info:
        _run(handle, approval_id=APPROVAL_ID)  # type: ignore[arg-type]

    assert exc_info.value.message.startswith(message)
    assert exit_code_for_error(exc_info.value) is ExitCode.APPROVAL_REQUIRED


def test_without_settings_the_client_never_waits() -> None:
    server = Server(writes=[_approval_required()], statuses=["approved"])

    with SolwynClient(
        api_url=API_URL,
        credential=login_credential(),
        transport=httpx.MockTransport(server),
    ) as client:
        with pytest.raises(ApiError) as exc_info:
            client.request("PUT", BUDGET_PATH, json={})

    assert exc_info.value.code == "approval_required"
    assert exc_info.value.hint == f"Approve the change in the browser: {APPROVAL_URL}"
    assert exit_code_for_error(exc_info.value) is ExitCode.APPROVAL_REQUIRED
    assert server.approval_reads == 0


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pw@app.solwyn.ai/approve/x",
        "https://app.solwyn.ai/approve/\x1b[31mx",
    ],
)
def test_unsafe_approval_urls_are_never_printed(url: str) -> None:
    server = Server(writes=[_approval_required(url)], statuses=["pending"])

    with pytest.raises(ApiError) as exc_info:
        _run(server, wait=0)

    assert url not in json.dumps(exc_info.value.detail)
    assert exc_info.value.hint is None
