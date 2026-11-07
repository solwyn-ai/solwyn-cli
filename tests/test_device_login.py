"""Device-flow login, logout revocation, keys env and credential selection."""

import functools
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from golden.harness import assert_matches_golden
from key_fixtures import PROJECT_ID, keys_entry
from login_fixtures import (
    LOGIN_TOKEN,
    expired_login_entries,
    fake_keychain,
    login_entries,
)
from typer.testing import CliRunner

from solwyn_cli import approvals, auth, oauth
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands import login as login_module
from solwyn_cli.main import app

runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
START = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
DEVICE_CODE = "solwyn_dc_" + "4f" * 32
ISSUED_TOKEN = "solwyn_cli_" + "9a" * 32
VALID_KEY = "solwyn_sk_" + "e5" * 32
USER_CODE = "BCDF-GHJK"
DEVICE_AUTHORIZATION = {
    "device_code": DEVICE_CODE,
    "user_code": USER_CODE,
    "verification_uri": "https://app.solwyn.ai/device",
    "verification_uri_complete": f"https://app.solwyn.ai/device?user_code={USER_CODE}",
    "expires_in": 600,
    "interval": 5,
}
TOKEN = {
    "access_token": ISSUED_TOKEN,
    "token_type": "Bearer",
    "expires_in": 2592000,
    "scope": "read contain",
    "solwyn_credential": {
        "id": "6c1f2a90-0000-4000-8000-000000000001",
        "label": "dev-laptop",
        "expires_at": "2026-11-29T14:03:11Z",
        "account_id": "a1",
        "account_name": "Acme",
        "email": "ada@example.com",
        "role": "owner",
    },
}


class FakeClock:
    def __init__(self) -> None:
        self.current = START
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = fake_keychain(monkeypatch)
    return values


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(login_module, "now", fake.now)
    monkeypatch.setattr(login_module, "sleep", fake.sleep)
    monkeypatch.setattr(auth, "_now", fake.now)
    return fake


class OAuthServer:
    def __init__(self, token_answers: list[httpx.Response]) -> None:
        self.token_answers = token_answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/device_authorization":
            return httpx.Response(200, json=DEVICE_AUTHORIZATION)
        if request.url.path == "/oauth/token":
            return self.token_answers.pop(0)
        raise AssertionError(f"unexpected {request.url}")

    def forms(self, path: str) -> list[dict[str, list[str]]]:
        return [
            parse_qs(request.content.decode())
            for request in self.requests
            if request.url.path == path
        ]


def _oauth_error(error: str) -> httpx.Response:
    return httpx.Response(400, json={"error": error, "error_description": error})


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., OAuthServer]]:
    def install(*answers: httpx.Response) -> OAuthServer:
        fake = OAuthServer(list(answers))
        monkeypatch.setattr(
            login_module,
            "SolwynClient",
            functools.partial(SolwynClient, transport=httpx.MockTransport(fake)),
        )
        return fake

    yield install


def test_login_polls_with_interval_and_slow_down_then_stores_the_credential(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    fake = server(
        _oauth_error("authorization_pending"),
        _oauth_error("slow_down"),
        httpx.Response(200, json=TOKEN),
    )

    result = runner.invoke(app, ["--json", "login", "--label", "dev-laptop"])

    assert result.exit_code == 0, result.stderr
    assert clock.sleeps == [5, 5, 10]
    assert fake.forms("/oauth/device_authorization") == [
        {
            "client_id": ["solwyn-cli"],
            "scope": ["read contain"],
            "device_label": ["dev-laptop"],
        }
    ]
    assert fake.forms("/oauth/token")[0] == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
        "device_code": [DEVICE_CODE],
        "client_id": ["solwyn-cli"],
    }
    assert fake.requests[0].headers["User-Agent"].startswith("solwyn-cli/")
    assert "Authorization" not in fake.requests[0].headers
    payload = json.loads(result.stdout)
    assert_matches_golden(result.stdout, GOLDEN_DIR / "login-device.json")
    assert payload == {
        "schema": "solwyn.login.v2",
        "auth_type": "login",
        "store": "file",
        "location": "/test/credentials.json",
        "credential_id": "6c1f2a90-0000-4000-8000-000000000001",
        "label": "dev-laptop",
        "scope": ["read", "contain"],
        "expires_at": "2026-11-29T14:03:11Z",
        "account_id": "a1",
        "account_name": "Acme",
        "email": "ada@example.com",
        "role": "owner",
        "project_id": None,
        "project_name": None,
    }
    login = json.loads(keychain[auth.LOGIN_ENTRY])
    assert login["token"] == ISSUED_TOKEN
    assert login["meta"]["account_name"] == "Acme"
    assert set(keychain) == {auth.LOGIN_ENTRY}
    assert "https://app.solwyn.ai/device" in result.stderr
    assert USER_CODE in result.stderr
    assert "Waiting for approval... (Ctrl-C to stop)" in result.stderr
    assert "You can sign up and verify your email on that page." in result.stderr
    for secret in (DEVICE_CODE, ISSUED_TOKEN):
        assert secret not in result.stdout + result.stderr


def test_login_human_output_names_the_account_and_the_approval_rule(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server(httpx.Response(200, json=TOKEN))

    result = runner.invoke(app, ["--no-color", "login"])

    assert result.exit_code == 0
    assert "Acme (owner)" in result.stdout
    assert "read, contain" in result.stdout
    assert "2026-11-29" in result.stdout
    assert (
        "Logged in as ada@example.com to account Acme (owner) on dev-laptop."
        in result.stderr
    )
    assert login_module.APPROVAL_NOTE in result.stderr


def test_read_scope_is_requested_as_read_only(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    fake = server(httpx.Response(200, json={**TOKEN, "scope": "read"}))

    result = runner.invoke(app, ["--json", "login", "--scope", "read"])

    assert result.exit_code == 0
    assert fake.forms("/oauth/device_authorization")[0]["scope"] == ["read"]
    assert json.loads(result.stdout)["scope"] == ["read"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["login", "--scope", "contain"],
        ["login", "--scope", "admin"],
        ["login", "--with-key", "--wait", "5"],
        ["login", "--with-key", "--continue"],
        ["login", "--wait", "-1"],
    ],
)
def test_invalid_login_options_are_usage_errors(
    keychain: dict[str, str], arguments: list[str]
) -> None:
    result = runner.invoke(app, ["--json", *arguments])

    assert result.exit_code == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("access_denied", "login_denied"),
        ("expired_token", "login_expired"),
        ("invalid_grant", "login_invalid"),
    ],
)
def test_terminal_poll_answers_exit_three_and_forget_the_pending_login(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    error: str,
    code: str,
) -> None:
    server(_oauth_error(error))

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert json.loads(result.stderr.splitlines()[-1])["code"] == code
    assert keychain == {}


def test_denied_message(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server(_oauth_error("access_denied"))

    result = runner.invoke(app, ["--json", "login"])

    assert json.loads(result.stderr.splitlines()[-1])["message"] == (
        "The login was denied in the browser."
    )


def test_wait_zero_returns_the_code_for_an_agent_and_exits_six(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    fake = server()

    result = runner.invoke(app, ["--json", "login", "--wait", "0"])

    assert result.exit_code == 6
    assert result.stdout == ""
    assert "Waiting for approval" not in result.stderr
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["schema"] == "solwyn.error.v1"
    assert error["code"] == "login_pending"
    assert error["message"] == (
        "Still waiting for approval. After approving, run: solwyn login --continue"
    )
    assert error["detail"] == {
        "schema": "solwyn.login.pending.v1",
        "user_code": USER_CODE,
        "verification_uri": "https://app.solwyn.ai/device",
        "verification_uri_complete": f"https://app.solwyn.ai/device?user_code={USER_CODE}",
        "expires_at": "2026-10-30T14:10:00Z",
    }
    assert fake.forms("/oauth/token") == []
    assert clock.sleeps == []
    pending = json.loads(keychain[auth.PENDING_DEVICE_ENTRY])
    assert pending["device_code"] == DEVICE_CODE
    assert DEVICE_CODE not in result.stderr


def test_wait_ends_at_the_bound_and_keeps_the_pending_login(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    fake = server(*[_oauth_error("authorization_pending")] * 10)

    result = runner.invoke(app, ["--json", "login", "--wait", "12"])

    assert result.exit_code == 6
    assert len(fake.forms("/oauth/token")) == 2
    assert clock.sleeps == [5, 5]
    assert auth.PENDING_DEVICE_ENTRY in keychain


def test_continue_polls_the_stored_code_at_once_and_finishes(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server()
    runner.invoke(app, ["--json", "login", "--wait", "0"])
    fake = server(httpx.Response(200, json=TOKEN))

    result = runner.invoke(app, ["--json", "login", "--continue", "--wait", "0"])

    assert result.exit_code == 0, result.stderr
    assert fake.forms("/oauth/device_authorization") == []
    assert fake.forms("/oauth/token")[0]["device_code"] == [DEVICE_CODE]
    assert clock.sleeps == []
    assert auth.PENDING_DEVICE_ENTRY not in keychain
    assert json.loads(keychain[auth.LOGIN_ENTRY])["token"] == ISSUED_TOKEN


def test_continue_after_approval_does_not_show_the_code_again(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    # Run-through finding 10: login --continue reprinted the page and code
    # after the login was already approved.
    server()
    runner.invoke(app, ["--json", "login", "--wait", "0"])
    server(httpx.Response(200, json=TOKEN))

    result = runner.invoke(app, ["--json", "login", "--continue"])

    assert result.exit_code == 0, result.stderr
    assert USER_CODE not in result.stderr
    assert "open this page" not in result.stderr


def test_continue_while_still_pending_shows_the_code_once(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server()
    runner.invoke(app, ["--json", "login", "--wait", "0"])
    server(
        _oauth_error("authorization_pending"),
        _oauth_error("authorization_pending"),
        httpx.Response(200, json=TOKEN),
    )

    result = runner.invoke(app, ["--json", "login", "--continue"])

    assert result.exit_code == 0, result.stderr
    assert result.stderr.count("and enter this code:") == 1
    assert result.stderr.count("To log in, open this page in a browser:") == 1


def test_continue_without_a_pending_login_exits_three(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server()

    result = runner.invoke(app, ["--json", "login", "--continue"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "login_not_pending"


def test_continue_after_the_code_expired_exits_three_with_login_expired(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server()
    runner.invoke(app, ["--json", "login", "--wait", "0"])
    clock.current += timedelta(minutes=11)

    result = runner.invoke(app, ["--json", "login", "--continue"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "login_expired"
    assert error["message"] == "The code expired. Run solwyn login again."
    assert auth.PENDING_DEVICE_ENTRY not in keychain


def test_ctrl_c_keeps_the_pending_login_for_continue(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server()

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(login_module, "sleep", interrupt)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code != 0
    assert auth.PENDING_DEVICE_ENTRY in keychain


def test_keychain_failure_after_issue_revokes_and_exits_three(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server(httpx.Response(200, json=TOKEN))
    revoked: list[str] = []
    monkeypatch.setattr(
        login_module, "revoke_token", lambda api_url, token: revoked.append(token)
    )

    def store_login(
        token: str, meta: auth.LoginMetadata, **_: object
    ) -> auth.LoginCredential:
        raise auth.CredentialStorageError(
            "Could not update credentials in the operating-system keychain."
        )

    monkeypatch.setattr(auth, "store_login", store_login)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert json.loads(result.stderr.splitlines()[-1])["code"] == (
        "credential_storage_failed"
    )
    assert revoked == [ISSUED_TOKEN]
    assert ISSUED_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(143)])
def test_an_interrupt_while_storing_the_issued_login_revokes_it(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    monkeypatch: pytest.MonkeyPatch,
    interrupt: BaseException,
) -> None:
    """Audit F10: Ctrl+C after Core issued the login must not leave it live."""
    server(httpx.Response(200, json=TOKEN))
    revoked: list[str] = []
    monkeypatch.setattr(
        login_module,
        "revoke_token",
        lambda api_url, token: revoked.append(token) or True,
    )

    def store_login(
        token: str, meta: auth.LoginMetadata, **_: object
    ) -> auth.LoginCredential:
        raise interrupt

    monkeypatch.setattr(auth, "store_login", store_login)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code != 0
    assert revoked == [ISSUED_TOKEN]
    assert ISSUED_TOKEN not in result.stdout + result.stderr


def test_pending_store_failure_stops_before_polling(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = server()

    def fail(pending: auth.PendingDevice, **kwargs: object) -> None:
        raise auth.CredentialStorageError("Could not update credentials.")

    monkeypatch.setattr(auth, "store_pending_device", fail)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert fake.forms("/oauth/token") == []


@pytest.mark.parametrize(
    "token",
    [
        {**TOKEN, "access_token": "eyJhbGciOiJIUzI1NiJ9.session.jwt"},
        {**TOKEN, "token_type": "mac"},
        {key: value for key, value in TOKEN.items() if key != "solwyn_credential"},
    ],
)
def test_invalid_token_answers_are_not_stored(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    token: dict[str, Any],
) -> None:
    server(httpx.Response(200, json=token))

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "invalid_auth_response"
    assert auth.LOGIN_ENTRY not in keychain


@pytest.mark.parametrize(
    "authorization",
    [
        {**DEVICE_AUTHORIZATION, "verification_uri": "javascript:alert(1)"},
        {**DEVICE_AUTHORIZATION, "user_code": "bcdf-ghjk\x1b[2J"},
        {**DEVICE_AUTHORIZATION, "device_code": "not-a-device-code"},
    ],
)
def test_unsafe_device_authorizations_are_refused(
    keychain: dict[str, str],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    authorization: dict[str, Any],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=authorization)

    monkeypatch.setattr(
        login_module,
        "SolwynClient",
        functools.partial(SolwynClient, transport=httpx.MockTransport(handle)),
    )

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert "javascript" not in result.stderr
    assert "\x1b" not in result.stderr
    assert keychain == {}


@pytest.mark.parametrize(
    ("stdin_tty", "stderr_tty", "expected"),
    [(True, True, 600), (True, False, 90), (False, True, 90), (False, False, 90)],
)
def test_default_wait_depends_on_both_terminals(
    monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stderr_tty: bool, expected: int
) -> None:
    class Stream(io.StringIO):
        def __init__(self, tty: bool) -> None:
            super().__init__()
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.setattr(approvals.sys, "stdin", Stream(stdin_tty))
    monkeypatch.setattr(approvals.sys, "stderr", Stream(stderr_tty))

    assert approvals.default_wait_seconds() == expected


def _revocation(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    original = oauth._revocation_client

    def client(api_url: str) -> httpx.Client:
        built = original(api_url)
        built._transport = httpx.MockTransport(record)
        return built

    monkeypatch.setattr(oauth, "revocation_client", client)
    return requests


def test_logout_revokes_the_stored_login_with_the_exact_form_then_clears(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.update(login_entries())
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)
    requests = _revocation(monkeypatch, lambda request: httpx.Response(200))

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema": "solwyn.logout.v1",
        "cleared": True,
        "revoked": True,
        "logins_found": 1,
        "stores": [
            {
                "store": "file",
                "location": "/test/credentials.json",
                "status": "cleared",
                "cleared": True,
                "kept": [],
                "legacy_cleared": [],
                "legacy_kept": [],
            },
            {
                "store": "keyring",
                "location": auth.credential_store.keyring_name(),
                "status": "cleared",
                "cleared": True,
                "kept": [],
                "legacy_cleared": [],
                "legacy_kept": [],
            },
        ],
    }
    assert result.stderr == ""
    assert keychain == {}
    (request,) = requests
    assert request.method == "POST"
    assert request.url == httpx.URL("https://api.solwyn.ai/oauth/revoke")
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert "Authorization" not in request.headers
    assert parse_qs(request.content.decode()) == {
        "token": [LOGIN_TOKEN],
        "token_type_hint": ["access_token"],
        "client_id": ["solwyn-cli"],
    }
    assert request.extensions["timeout"]["read"] == 5.0


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503),
        lambda request: httpx.Response(400, json={"error": "unauthorized_client"}),
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectTimeout("slow", request=request)
        ),
    ],
)
def test_logout_revoke_failure_still_clears_and_exits_zero(
    keychain: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    keychain.update(login_entries())
    requests = _revocation(monkeypatch, handler)

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0
    assert len(requests) == 1
    assert json.loads(result.stdout)["revoked"] is False
    assert login_module.REVOKE_FAILED_NOTE in result.stderr
    assert keychain == {}


def test_logout_revokes_an_expired_login_too(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain.update(expired_login_entries())
    requests = _revocation(monkeypatch, lambda request: httpx.Response(200))

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0
    assert len(requests) == 1


def test_logout_without_a_login_makes_no_request(
    keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)
    requests = _revocation(monkeypatch, lambda request: httpx.Response(200))

    result = runner.invoke(app, ["--json", "logout"])

    assert result.exit_code == 0
    assert requests == []
    assert json.loads(result.stdout)["revoked"] is None
    assert keychain == {}


def test_keys_env_prints_the_export_lines_and_nothing_else(
    keychain: dict[str, str],
) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)

    result = runner.invoke(app, ["-p", PROJECT_ID, "keys", "env"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout == (
        f"export SOLWYN_API_KEY='{VALID_KEY}'\nexport SOLWYN_PROJECT='{PROJECT_ID}'\n"
    )
    assert result.stderr == ""


def test_keys_env_reads_only_the_stored_key(keychain: dict[str, str]) -> None:
    keychain.update(login_entries())

    result = runner.invoke(
        app,
        ["--json", "-p", PROJECT_ID, "keys", "env"],
        env={"SOLWYN_API_KEY": VALID_KEY},
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["code"] == "api_key_not_stored"
    assert VALID_KEY not in result.stderr


def test_keys_env_json_mode(keychain: dict[str, str]) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)

    result = runner.invoke(app, ["--json", "-p", PROJECT_ID, "keys", "env"])

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "solwyn.keys-env.v2",
        "names": ["SOLWYN_API_KEY", "SOLWYN_PROJECT"],
        "export": (
            f"export SOLWYN_API_KEY='{VALID_KEY}'\nexport SOLWYN_PROJECT='{PROJECT_ID}'"
        ),
        "api_url": None,
        "project": {"selector": PROJECT_ID, "id": PROJECT_ID},
    }


def test_keys_env_never_writes_files(
    keychain: dict[str, str], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["-p", PROJECT_ID, "keys", "env"])

    assert result.exit_code == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", ["jwt", "Login", "both"])
def test_unknown_solwyn_auth_is_a_usage_error(
    keychain: dict[str, str], value: str
) -> None:
    result = runner.invoke(app, ["--json", "whoami"], env={"SOLWYN_AUTH": value})

    assert result.exit_code == 2
    assert "SOLWYN_AUTH must be login or project_key" in result.stderr


def test_expired_login_with_no_fallback_says_when_it_expired(
    keychain: dict[str, str],
) -> None:
    keychain.update(expired_login_entries())

    result = runner.invoke(app, ["--json", "projects", "list"], env={})

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert error["message"].startswith("Your Solwyn login expired on ")
    assert error["hint"] == "Run solwyn login."


def test_solwyn_auth_login_without_a_login_is_explained(
    keychain: dict[str, str],
) -> None:
    keychain[auth.API_KEYS_ENTRY] = keys_entry(key=VALID_KEY)

    result = runner.invoke(
        app, ["--json", "projects", "list"], env={"SOLWYN_AUTH": "login"}
    )

    assert result.exit_code == 3
    assert json.loads(result.stderr)["message"] == (
        "SOLWYN_AUTH=login requires a Solwyn login."
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "budget",
            "check",
            "--estimated-input-tokens",
            "10",
            "--model",
            "gpt-5",
            "--provider",
            "openai",
        ],
        ["record", "--model", "gpt-4o", "--provider", "openai"],
    ],
)
def test_metering_with_only_a_login_names_the_project_key(
    keychain: dict[str, str], arguments: list[str]
) -> None:
    keychain.update(login_entries())

    result = runner.invoke(app, ["--json", *arguments], env={})

    assert result.exit_code == 3
    error = json.loads(result.stderr)
    assert error["code"] == "authentication_required"
    assert error["hint"] == (
        auth.METERING_KEY_HINT + " Looked in: file, /test/credentials.json."
    )


def test_invite_accept_is_retired(keychain: dict[str, str]) -> None:
    result = runner.invoke(app, ["--json", "invite", "accept", "--yes"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["code"] == "invite_accept_retired"
    assert error["message"] == (
        "Accept the invitation from the link in your email, then run solwyn login."
    )


def test_failed_cleanup_revoke_tells_the_user_to_revoke_in_the_dashboard(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review item 7: a live, unstored credential must not be silent.
    server(httpx.Response(200, json=TOKEN))
    requests = _revocation(monkeypatch, lambda request: httpx.Response(503))

    def store_login(
        token: str, meta: auth.LoginMetadata, **_: object
    ) -> auth.LoginCredential:
        raise auth.CredentialStorageError(
            "Could not update credentials in the operating-system keychain."
        )

    monkeypatch.setattr(auth, "store_login", store_login)

    result = runner.invoke(app, ["--json", "login"])

    assert result.exit_code == 3
    assert len(requests) == 1
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "credential_storage_failed"
    assert "https://app.solwyn.ai/settings/cli" in (error["hint"] or "")
    assert ISSUED_TOKEN not in result.stdout + result.stderr


def test_slow_down_keeps_adding_five_seconds_past_sixty(
    keychain: dict[str, str],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review item 8: RFC 8628 section 3.5 has no ceiling.
    answers = [_oauth_error("slow_down"), httpx.Response(200, json=TOKEN)]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/device_authorization":
            return httpx.Response(200, json={**DEVICE_AUTHORIZATION, "interval": 60})
        return answers.pop(0)

    monkeypatch.setattr(
        login_module,
        "SolwynClient",
        functools.partial(SolwynClient, transport=httpx.MockTransport(handle)),
    )

    result = runner.invoke(app, ["--json", "login", "--wait", "600"])

    assert result.exit_code == 0, result.stderr
    assert clock.sleeps == [60, 65]


# --- --no-store: SOLWYN_TOKEN for CI and cloud agents -----------------------


@pytest.mark.parametrize("output", ["human", "json"])
def test_no_store_prints_the_token_once_and_stores_nothing(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    memory_keyring: Any,
    output: str,
) -> None:
    server(_oauth_error("authorization_pending"), httpx.Response(200, json=TOKEN))
    flags = ["--json"] if output == "json" else []

    result = runner.invoke(
        app, [*flags, "login", "--no-store", "--label", "ci", "--scope", "read"]
    )

    assert result.exit_code == 0, result.stderr
    # Nothing stored anywhere: no login, no pending device, no keyring entry.
    assert keychain == {}
    assert memory_keyring.values == {}
    # The token appears exactly once, in the deliberate stdout output.
    assert ISSUED_TOKEN not in result.stderr
    assert result.stdout.count(ISSUED_TOKEN) == 1
    if output == "json":
        assert_matches_golden(result.stdout, GOLDEN_DIR / "login-token.json")
        document = json.loads(result.stdout)
        assert document["schema"] == "solwyn.login.token.v1"
        assert document["token"] == ISSUED_TOKEN
        assert document["credential_id"] == TOKEN["solwyn_credential"]["id"]
    else:
        assert result.stdout == ISSUED_TOKEN + "\n"
    assert "Nothing was stored" in result.stderr
    assert "SOLWYN_TOKEN" in result.stderr


@pytest.mark.parametrize(
    "extra",
    [["--with-key"], ["--continue"], ["--store", "file"], ["--wait", "0"]],
)
def test_no_store_rejects_options_that_store_or_resume(
    keychain: dict[str, str], extra: list[str]
) -> None:
    result = runner.invoke(app, ["--json", "login", "--no-store", *extra])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["code"] == "invalid_login_options"
    assert keychain == {}


def test_no_store_timeout_keeps_nothing_to_resume(
    keychain: dict[str, str], clock: FakeClock, server: Callable[..., OAuthServer]
) -> None:
    server(*[_oauth_error("authorization_pending") for _ in range(10)])

    result = runner.invoke(app, ["--json", "login", "--no-store", "--wait", "6"])

    assert result.exit_code == 3
    error = json.loads(result.stderr.splitlines()[-1])
    assert error["code"] == "login_not_approved"
    assert keychain == {}


@pytest.mark.parametrize("output", ["human", "json"])
def test_no_store_redacts_secrets_embedded_in_metadata(
    keychain: dict[str, str],
    clock: FakeClock,
    server: Callable[..., OAuthServer],
    output: str,
) -> None:
    other_token = "solwyn_cli_" + "7b" * 32
    device_code = "solwyn_dc_" + "6c" * 32
    poisoned = {
        **TOKEN,
        "solwyn_credential": {
            **TOKEN["solwyn_credential"],
            "account_name": f"Acme {other_token}",
            "label": f"ci {device_code}",
            "email": f"x{other_token}@example.com",
        },
    }
    server(httpx.Response(200, json=poisoned))
    flags = ["--json"] if output == "json" else []

    result = runner.invoke(app, [*flags, "login", "--no-store"])

    assert result.exit_code == 0, result.stderr
    rendered = result.stdout + result.stderr
    # Only the newly issued token is exempt from redaction, and only once.
    assert rendered.count(ISSUED_TOKEN) == 1
    assert other_token not in rendered
    assert device_code not in rendered
    if output == "json":
        document = json.loads(result.stdout)
        assert document["token"] == ISSUED_TOKEN
        assert document["account_name"] == "Acme [REDACTED]"
        assert document["label"] == "ci [REDACTED]"
