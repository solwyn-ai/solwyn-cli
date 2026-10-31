"""Downloads remain lazy, authenticated, and close on completion or failure."""

from contextlib import contextmanager

from typer.testing import CliRunner
from solwyn_cli.commands import costs
from solwyn_cli.main import app

import httpx
import pytest
from solwyn_cli.errors import ApiError

from solwyn_cli.auth import ProjectKeyCredential
from solwyn_cli.client import SolwynClient


class Body(httpx.SyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    def __iter__(self):
        self.reads += 1
        yield b"name,cost\n"
        self.reads += 1
        yield b"'=SUM(A1),2\n"

    def close(self):
        self.closed = True


def test_stream_does_not_buffer_before_first_byte():
    body = Body()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    with SolwynClient(
        api_url="https://api.solwyn.ai",
        credential=ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
        transport=httpx.MockTransport(handle),
    ) as client:
        with client.stream("GET", "/api/v1/projects/p/costs/export") as response:
            assert body.reads == 0
            iterator = response.iter_bytes()
            assert next(iterator) == b"name,cost\n"
            assert body.reads == 1
            assert next(iterator) == b"'=SUM(A1),2\n"
    assert body.closed
    assert requests[0].headers["authorization"].startswith("Bearer sk_proj_")


def test_export_command_preserves_bytes_and_reports_headers(monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @contextmanager
        def stream(self, method, path, **kwargs):
            captured.append((method, path, kwargs))
            response = httpx.Response(
                200,
                stream=Body(),
                headers={
                    "X-Solwyn-Retention-Clamped": "true",
                    "X-Solwyn-Truncated": "false",
                    "X-Solwyn-Export-Snapshot": "statement",
                    "X-Solwyn-Effective-From": "2026-08-01T00:00:00Z",
                },
            )
            try:
                yield response
            finally:
                response.close()

    monkeypatch.setattr(costs, "SolwynClient", FakeClient)
    monkeypatch.setattr(costs, "get_capabilities", lambda **kwargs: None)
    monkeypatch.setattr(
        costs,
        "resolve_credential",
        lambda **_: ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
    )
    result = CliRunner().invoke(
        app, ["-p", "project", "costs", "export", "--format", "csv"]
    )
    assert result.exit_code == 0, result.exception
    assert result.stdout == "name,cost\n'=SUM(A1),2\n"
    assert "X-Solwyn-Retention-Clamped: true" in result.stderr
    assert "X-Solwyn-Export-Snapshot: statement" in result.stderr
    assert "X-Solwyn-Truncated" not in result.stderr
    assert captured == [
        (
            "GET",
            "/api/v1/projects/project/costs/export",
            {"params": {"range": "30d", "format": "csv"}},
        )
    ]


def test_stream_errors_are_structured_and_close_before_retry():
    first = Body()
    second = Body()
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(
            429 if len(attempts) == 1 else 200,
            stream=first if len(attempts) == 1 else second,
        )

    with SolwynClient(
        api_url="https://api.solwyn.ai",
        credential=ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
        transport=httpx.MockTransport(handle),
        sleeper=lambda _: None,
    ) as client:
        with client.stream("GET", "/api/v1/projects/p/costs/export") as response:
            assert first.closed
            assert second.reads == 0
            assert b"".join(response.iter_bytes()) == b"name,cost\n'=SUM(A1),2\n"
    assert len(attempts) == 2
    assert second.closed


def test_stream_interruption_does_not_replay_partial_body():
    class Interrupted(Body):
        def __iter__(self):
            yield b"first\n"
            raise httpx.ReadError("private transport detail")

    body = Interrupted()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=body)

    with SolwynClient(
        api_url="https://api.solwyn.ai",
        credential=ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
        transport=httpx.MockTransport(handle),
    ) as client:
        with pytest.raises(ApiError, match="Download interrupted"):
            with client.stream("GET", "/api/v1/projects/p/costs/export") as response:
                iterator = response.iter_bytes()
                assert next(iterator) == b"first\n"
                next(iterator)
    assert len(calls) == 1
    assert body.closed


def test_stream_busy_retries_before_yielding_any_export_bytes():
    first = Body()
    second = Body()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            503 if len(requests) == 1 else 200,
            headers={"Retry-After": "0"},
            stream=first if len(requests) == 1 else second,
        )

    with SolwynClient(
        api_url="https://example.test",
        credential=ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
        transport=httpx.MockTransport(handle),
        sleeper=lambda _: None,
    ) as client:
        with client.stream("GET", "/api/v1/projects/p/costs/export") as response:
            assert first.closed
            assert second.reads == 0
            assert b"".join(response.iter_bytes()) == b"name,cost\n'=SUM(A1),2\n"
    assert len(requests) == 2
    assert second.closed


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_export_body_stays_byte_exact_while_header_notices_are_redacted(
    monkeypatch, fmt
):
    token = "solwyn_cli_" + "9a" * 32
    device_code = "solwyn_dc_" + "4f" * 32
    body = f"name,cost\nname_{token}_x,{device_code}\n".encode()

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @contextmanager
        def stream(self, method, path, **kwargs):
            response = httpx.Response(
                200,
                content=body,
                headers={"X-Solwyn-Export-Snapshot": f"snap_{device_code}"},
            )
            try:
                yield response
            finally:
                response.close()

    monkeypatch.setattr(costs, "SolwynClient", FakeClient)
    monkeypatch.setattr(costs, "get_capabilities", lambda **kwargs: None)
    monkeypatch.setattr(
        costs,
        "resolve_credential",
        lambda **_: ProjectKeyCredential(
            api_key="sk_proj_" + "a1" * 32, source="environment"
        ),
    )

    result = CliRunner().invoke(
        app, ["-p", "project", "costs", "export", "--format", fmt]
    )

    assert result.exit_code == 0, result.exception
    # Raw exports are byte-for-byte copies of the server data, by design.
    assert result.stdout_bytes == body
    assert "X-Solwyn-Export-Snapshot: snap_[REDACTED]" in result.stderr
    assert device_code not in result.stderr
