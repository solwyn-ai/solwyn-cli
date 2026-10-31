"""Login and logout commands."""

import socket
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli import auth
from solwyn_cli.approvals import default_wait_seconds
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    LoginCredential,
    LoginMetadata,
    PendingDevice,
    ProjectKeyCredential,
    clear_credentials,
    store_project_key,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.oauth import (
    OAuthError,
    TokenResponse,
    poll_token,
    revoke_token,
    start_device_authorization,
)
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


LOGIN_HELP = help_contract(
    endpoint=(
        "POST /oauth/device_authorization and POST /oauth/token, or local keychain "
        "write with --with-key"
    ),
    auth="none",
    exits=(
        "0 success; 1 generic/API; 2 usage; 3 auth, denied, expired or credential "
        "storage; 6 still waiting for browser approval"
    ),
    schema="solwyn.login.v2",
    guidance=(
        "Prints a URL and a code on stderr; approve in a browser, where you can also sign up.",
        "The CLI never opens a browser, so SSH and agent sandboxes work the same way.",
        "Default --wait is 600 s with a terminal, else 90 s; --wait 0 starts and returns.",
        "Exit 6 is error code login_pending; its detail (solwyn.login.pending.v1) has "
        "user_code, verification_uri, verification_uri_complete and expires_at.",
        "Keys are never accepted as command-line arguments or echoed.",
    ),
)

LOGOUT_HELP = help_contract(
    endpoint="POST /oauth/revoke (best effort) and local keychain clear",
    auth="local (none)",
    exits="0 success; 2 usage; 3 credential storage",
    schema="solwyn.logout.v1",
)

DEFAULT_SCOPE = "read contain"
_SCOPES = {
    "read": "read",
    "read,contain": "read contain",
    "contain,read": "read contain",
}
APPROVAL_NOTE = (
    "Raising a cap, changing enforcement mode, pausing rules, keys, billing and team "
    "changes need your approval in the browser each time."
)
REVOKE_FAILED_NOTE = (
    "The login could not be revoked on the server. Revoke it in the dashboard under "
    "Settings, CLI logins."
)

# Injectable for tests; production waits on the wall clock.
sleep: Callable[[float], object] = time.sleep


def now() -> datetime:
    return datetime.now(UTC)


class _LoginData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_type: Literal["login", "project_key"]
    credential_id: str | None
    label: str | None
    scope: list[str] | None
    expires_at: datetime | None
    account_id: str | None
    account_name: str | None
    email: str | None
    role: Literal["owner", "admin", "member"] | None


def _note(text: str) -> None:
    typer.echo(text, err=True)


def default_label() -> str:
    """Return the short hostname, as printable ASCII of at most 64 characters."""
    try:
        hostname = socket.gethostname()
    except OSError:
        return ""
    return _sanitize_label(hostname.split(".", 1)[0])


def _sanitize_label(value: str) -> str:
    cleaned = "".join(
        character for character in value if 0x20 <= ord(character) < 0x7F
    ).strip()
    return cleaned[:64]


def authenticate_with_project_key(project_key: str) -> ProjectKeyCredential:
    """Validate and store one project key through the shared auth boundary."""
    return store_project_key(project_key)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _fail(
    context: typer.Context,
    code: str,
    message: str,
    hint: str | None = None,
    *,
    detail: dict[str, object] | None = None,
    semantic: ErrorSemantic = ErrorSemantic.AUTH,
) -> None:
    exit_with_error(
        context,
        OutputError(code=code, message=message, hint=hint, detail=detail),
        semantic=semantic,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _print_instructions(pending: PendingDevice, *, waiting: bool) -> None:
    minutes = max(1, round((pending.expires_at - now()).total_seconds() / 60))
    lines = [
        "To log in, open this page in a browser:",
        "",
        f"  {pending.verification_uri}",
        "",
        "and enter this code:",
        "",
        f"  {pending.user_code}",
        "",
        f"Or open {pending.verification_uri_complete} and check that it shows "
        "the same code.",
        f"The code expires in {minutes} minute{'s' if minutes != 1 else ''}. "
        "You can sign up and verify your email on that page.",
    ]
    if waiting:
        lines.append("Waiting for approval... (Ctrl-C to stop)")
    _note("\n".join(lines))


def _start(
    context: typer.Context, client: SolwynClient, *, scope: str, label: str
) -> PendingDevice:
    try:
        authorization = start_device_authorization(
            client, scope=scope, device_label=label or None
        )
    except ApiError as error:
        exit_with_error(context, error)
    except OAuthError as error:
        _fail(
            context,
            "login_failed",
            f"The login could not start ({error.error}).",
            "Check --api-url and retry solwyn login.",
        )
    except (ValidationError, ValueError, TypeError):
        _fail(
            context,
            "invalid_auth_response",
            "The device authorization response was invalid.",
        )
    pending = PendingDevice(
        device_code=authorization.device_code,
        user_code=authorization.user_code,
        verification_uri=authorization.verification_uri,
        verification_uri_complete=authorization.verification_uri_complete,
        expires_at=now() + timedelta(seconds=authorization.expires_in),
        interval=authorization.interval,
    )
    try:
        auth.store_pending_device(pending)
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error))
    return pending


def _pending_exit(
    context: typer.Context, pending: PendingDevice, continue_command: str
) -> None:
    _fail(
        context,
        "login_pending",
        f"Still waiting for approval. After approving, run: {continue_command}",
        detail={
            "schema": "solwyn.login.pending.v1",
            "user_code": pending.user_code,
            "verification_uri": pending.verification_uri,
            "verification_uri_complete": pending.verification_uri_complete,
            "expires_at": _iso(pending.expires_at),
        },
        semantic=ErrorSemantic.APPROVAL_REQUIRED,
    )


def _expired_exit(context: typer.Context) -> None:
    auth.clear_pending_device()
    _fail(
        context,
        "login_expired",
        "The code expired. Run solwyn login again.",
    )


def _poll(
    context: typer.Context,
    client: SolwynClient,
    pending: PendingDevice,
    *,
    wait_seconds: float,
    poll_first: bool,
    continue_command: str,
) -> TokenResponse:
    """Poll the token endpoint at the server's interval until a result or the wait ends."""
    started = now()
    interval = pending.interval
    first = poll_first
    while True:
        current = now()
        if current >= pending.expires_at:
            _expired_exit(context)
        if not first:
            waited = (current - started).total_seconds()
            if waited + interval > wait_seconds:
                _pending_exit(context, pending, continue_command)
            sleep(interval)
        first = False
        try:
            return poll_token(
                client, device_code=pending.device_code.get_secret_value()
            )
        except OAuthError as error:
            if error.error == "authorization_pending":
                continue
            if error.error == "slow_down":
                interval = min(interval + 5, 60)
                pending = pending.model_copy(update={"interval": interval})
                try:
                    auth.store_pending_device(pending)
                except CredentialStorageError:
                    pass
                continue
            if error.error == "access_denied":
                auth.clear_pending_device()
                _fail(context, "login_denied", "The login was denied in the browser.")
            if error.error == "expired_token":
                _expired_exit(context)
            if error.error == "invalid_grant":
                auth.clear_pending_device()
                _fail(
                    context,
                    "login_invalid",
                    "The login code is no longer valid. Run solwyn login again.",
                )
            _fail(
                context,
                "login_failed",
                f"The login failed ({error.error}).",
                "Run solwyn login again.",
            )
        except ApiError as error:
            exit_with_error(context, error)
        except (ValidationError, ValueError, TypeError):
            _fail(
                context,
                "invalid_auth_response",
                "The token response was invalid.",
            )


def device_login(
    context: typer.Context,
    *,
    scope: str = DEFAULT_SCOPE,
    label: str | None = None,
    wait_seconds: float | None = None,
    resume: bool = False,
    continue_command: str = "solwyn login --continue",
) -> LoginCredential:
    """Run the device flow and store the resulting CLI login in the keychain."""
    wait = default_wait_seconds() if wait_seconds is None else wait_seconds
    api_url = command_context(context).api_url
    with SolwynClient(api_url=api_url, credential=None) as client:
        if resume:
            stored = auth.load_pending_device()
            if stored is None:
                _fail(
                    context,
                    "login_not_pending",
                    "No login is waiting for approval.",
                    "Run solwyn login.",
                )
            pending = stored
            if pending.expires_at <= now():
                _expired_exit(context)
        else:
            pending = _start(
                context,
                client,
                scope=scope,
                label=default_label() if label is None else _sanitize_label(label),
            )
        _print_instructions(pending, waiting=wait > 0 or resume)
        token = _poll(
            context,
            client,
            pending,
            wait_seconds=wait,
            poll_first=resume,
            continue_command=continue_command,
        )
    issued = token.solwyn_credential
    meta = LoginMetadata(
        id=issued.id,
        label=issued.label,
        scope=token.scope,
        expires_at=issued.expires_at,
        account_id=issued.account_id,
        account_name=issued.account_name,
        email=issued.email,
        role=issued.role,
    )
    try:
        return auth.store_login(token.access_token.get_secret_value(), meta)
    except CredentialStorageError as error:
        # The server issued a credential we cannot keep; do not leave it live.
        revoke_token(api_url, token.access_token.get_secret_value())
        _fail(context, "credential_storage_failed", str(error))


def _login_data(credential: LoginCredential | None) -> _LoginData:
    if credential is None:
        return _LoginData(
            auth_type="project_key",
            credential_id=None,
            label=None,
            scope=None,
            expires_at=None,
            account_id=None,
            account_name=None,
            email=None,
            role=None,
        )
    meta = credential.meta
    return _LoginData(
        auth_type="login",
        credential_id=meta.id,
        label=meta.label,
        scope=meta.scopes,
        expires_at=meta.expires_at,
        account_id=meta.account_id,
        account_name=meta.account_name,
        email=meta.email,
        role=meta.role,
    )


def _login_success(context: typer.Context, credential: LoginCredential | None) -> None:
    data = _login_data(credential)
    if credential is None:
        rows: tuple[tuple[str, str], ...] = (("Authentication", "project_key"),)
    else:
        meta = credential.meta
        rows = (
            ("Authentication", "login"),
            ("Account", f"{meta.account_name} ({meta.role})"),
            ("Email", meta.email),
            ("Label", meta.label),
            ("Scope", ", ".join(meta.scopes)),
            ("Expires", meta.expires_at.date().isoformat()),
        )
        _note(
            f"Logged in as {meta.email} to account {meta.account_name} "
            f"({meta.role}) on {meta.label}.\n{APPROVAL_NOTE}"
        )
    output_engine(context).write_success(
        schema="solwyn.login.v2",
        data=data,
        table=TableData(
            columns=("Field", "Value"),
            rows=rows,
            right_align=("Value",),
        ),
    )


def _login_with_key(context: typer.Context) -> None:
    project_key = (
        typer.prompt("Project key", hide_input=True, err=True)
        if _stdin_is_tty()
        else sys.stdin.read().strip()
    )
    try:
        authenticate_with_project_key(project_key)
        auth.delete_legacy_entries()
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error))
    except AuthError:
        _fail(
            context,
            "invalid_project_key",
            "The project key is empty or malformed.",
        )
    _login_success(context, None)


def login(
    context: typer.Context,
    with_key: Annotated[
        bool,
        typer.Option(
            "--with-key",
            help="Read and store a project API key from a hidden prompt or stdin.",
        ),
    ] = False,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Scopes to request: read, or read,contain.",
        ),
    ] = "read,contain",
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Name for this login in the dashboard. Defaults to the short hostname.",
        ),
    ] = None,
    wait: Annotated[
        int | None,
        typer.Option(
            "--wait",
            min=0,
            help="Seconds to wait for browser approval. 0 starts and returns.",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--continue",
            help="Resume waiting for the login started earlier.",
        ),
    ] = False,
) -> None:
    """Log in through the browser, or store a project key with --with-key."""
    if with_key:
        if resume or label is not None or wait is not None or scope != "read,contain":
            _fail(
                context,
                "invalid_login_options",
                "--with-key cannot be combined with --scope, --label, --wait or --continue.",
                semantic=ErrorSemantic.USAGE,
            )
        _login_with_key(context)
        return
    requested = _SCOPES.get(scope.replace(" ", ""))
    if requested is None:
        _fail(
            context,
            "invalid_login_scope",
            "--scope must be read or read,contain.",
            semantic=ErrorSemantic.USAGE,
        )
    credential = device_login(
        context,
        scope=requested,
        label=label,
        wait_seconds=wait,
        resume=resume,
    )
    _login_success(context, credential)


def logout(context: typer.Context) -> None:
    """Revoke the stored login and clear all stored Solwyn credentials."""
    stored = auth.stored_login(include_expired=True)
    revoked: bool | None = None
    if stored is not None:
        revoked = revoke_token(command_context(context).api_url, stored.bearer_value())
    try:
        clear_credentials()
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error))
    if revoked is False:
        _note(REVOKE_FAILED_NOTE)
    output_engine(context).write_success(
        schema="solwyn.logout.v1",
        data={"cleared": True, "revoked": revoked},
        table=TableData(
            columns=("Credentials", "Status"),
            rows=(
                ("all", "cleared"),
                *(
                    (("login", "revoked" if revoked else "not revoked"),)
                    if revoked is not None
                    else ()
                ),
            ),
            right_align=("Status",),
        ),
    )
