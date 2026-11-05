"""Login and logout commands."""

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli import auth, credential_store
from solwyn_cli.approvals import default_wait_seconds
from solwyn_cli.auth import (
    AuthError,
    CredentialStorageError,
    LoginCredential,
    LoginMetadata,
    PendingDevice,
    ApiKeyCredential,
    ResolvedCredential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import (
    credential_saved_note,
    exit_for_resolution,
    exit_with_error,
    legacy_keys_note,
)
from solwyn_cli.project_resolver import ProjectResolutionError
from solwyn_cli.runtime import resolve_project
from solwyn_cli.credential_store import STORE_ENV, STORE_MODES, StoreMode
from solwyn_cli.errors import ApiError, redact_secrets
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
        "POST /oauth/device_authorization and POST /oauth/token, or with "
        "--with-key GET /api/v1/auth/key and a local credential store write"
    ),
    auth="none",
    exits=(
        "0 success; 1 generic/API; 2 usage; 3 auth, denied, expired or credential "
        "storage; 6 still waiting for browser approval"
    ),
    schema="solwyn.login.v2; with --no-store --json: solwyn.login.token.v1",
    guidance=(
        "Prints a URL and a code on stderr; approve in a browser, where you can also sign up.",
        "The CLI never opens a browser, so SSH and agent sandboxes work the same way.",
        "Default --wait is 600 s with a terminal, else 90 s; --wait 0 starts and returns.",
        "Exit 6 is error code login_pending; its detail (solwyn.login.pending.v1) has "
        "user_code, verification_uri, verification_uri_complete and expires_at.",
        "Keys are never accepted as command-line arguments or echoed.",
        "--with-key needs a project (--project, SOLWYN_PROJECT or .solwyn.env): "
        "the key is checked with GET /api/v1/auth/key and stored for that project.",
        "Stores in the OS keychain on macOS and Windows and in a private 0600 "
        "credentials file on Linux; --store auto|keyring|file chooses and is saved "
        "to config. SOLWYN_CREDENTIAL_STORE overrides config.",
        "A failed keychain write stores nothing and exits 3; there is no fallback "
        "to the file, and nothing is copied between stores.",
        "--no-store finishes the login and prints the token once on stdout for "
        "SOLWYN_TOKEN (CI, cloud agents); nothing is stored.",
    ),
)

LOGOUT_HELP = help_contract(
    endpoint="POST /oauth/revoke (best effort) and a local credential store clear",
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
CLI_LOGINS_URL = "https://app.solwyn.ai/settings/cli"
UNSTORED_LOGIN_HINT = (
    "The new login could not be revoked and is still active. "
    f"Revoke it at {CLI_LOGINS_URL}."
)
REVOKE_FAILED_NOTE = (
    "The login could not be revoked on the server. Revoke it in the dashboard under "
    f"Settings, CLI logins ({CLI_LOGINS_URL})."
)

# Injectable for tests; production waits on the wall clock.
sleep: Callable[[float], object] = time.sleep


UNCHECKED_KEYRING_NOTE = (
    "No system keyring is reachable from this session, so it was not checked. "
    "If you used --store keyring in a desktop session, run solwyn logout there too."
)


def now() -> datetime:
    return datetime.now(UTC)


class _LoginData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_type: Literal["login", "project_key"]
    store: Literal["keyring", "file"]
    location: str | None
    credential_id: str | None
    label: str | None
    scope: list[str] | None
    expires_at: datetime | None
    account_id: str | None
    account_name: str | None
    email: str | None
    role: Literal["owner", "admin", "member"] | None
    project_id: str | None = None
    project_name: str | None = None


def _note(text: str) -> None:
    typer.echo(redact_secrets(text), err=True)


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


def _selected_store(context: typer.Context, requested: str | None) -> StoreMode | None:
    """Validate --store and save it to config; nothing moves between stores."""
    if requested is None:
        return None
    if requested not in STORE_MODES:
        _fail(
            context,
            "invalid_login_options",
            "--store must be auto, keyring or file.",
            semantic=ErrorSemantic.USAGE,
        )
    environment_mode = os.environ.get(STORE_ENV, "")
    if environment_mode and environment_mode != requested:
        _fail(
            context,
            "invalid_login_options",
            f"{STORE_ENV}={environment_mode} conflicts with --store {requested}.",
            f"Unset {STORE_ENV}, or pass the same mode.",
            semantic=ErrorSemantic.USAGE,
        )
    # Only the mode is saved (auto removes it); nothing is copied between
    # stores. Saved first, so later reads look in the store this login writes.
    try:
        auth.save_store_mode(requested)  # type: ignore[arg-type]
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error), error.hint)
    return requested  # type: ignore[return-value]


def _print_unstored_token(
    context: typer.Context, token: str, meta: LoginMetadata
) -> None:
    """Write the --no-store token once: the deliberate, unredacted output."""
    if command_context(context).output_format.value == "json":
        # Metadata is redacted like any other output; only the newly issued
        # token is exempt, because printing it is the point of --no-store.
        metadata = redact_secrets(
            {
                "credential_id": meta.id,
                "label": meta.label,
                "scope": meta.scopes,
                "expires_at": _iso(meta.expires_at),
                "account_id": meta.account_id,
                "account_name": meta.account_name,
                "email": meta.email,
                "role": meta.role,
            }
        )
        document = {"schema": "solwyn.login.token.v1", "token": token, **metadata}
        line = json.dumps(document, separators=(",", ":"))
    else:
        line = token
    # Bypasses the output engine on purpose: the engine redacts CLI tokens.
    typer.echo(line)
    _note(
        f"Created CLI login {meta.label} ({', '.join(meta.scopes)}), expires "
        f"{meta.expires_at.date().isoformat()}. Nothing was stored. Keep it in a "
        "secret manager and pass it as SOLWYN_TOKEN. Revoke it with solwyn "
        f"credentials revoke or at {CLI_LOGINS_URL}."
    )


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
    context: typer.Context,
    client: SolwynClient,
    *,
    scope: str,
    label: str,
    persist: bool = True,
    store: StoreMode | None = None,
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
    if persist:
        try:
            auth.store_pending_device(pending, expect_mode=store)
        except CredentialStorageError as error:
            _fail(context, "credential_storage_failed", str(error), error.hint)
    return pending


def _pending_exit(
    context: typer.Context, pending: PendingDevice, continue_command: str | None
) -> None:
    if continue_command is None:
        # --no-store keeps nothing to resume from.
        _fail(
            context,
            "login_not_approved",
            "The login was not approved within --wait; nothing was stored.",
            "Run solwyn login --no-store again and approve it in the browser.",
        )
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
    continue_command: str | None,
    store: StoreMode | None = None,
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
                # RFC 8628 section 3.5: every slow_down adds 5 seconds.
                interval += 5
                pending = pending.model_copy(update={"interval": interval})
                if continue_command is not None:
                    try:
                        auth.store_pending_device(pending, expect_mode=store)
                    except CredentialStorageError as storage_error:
                        # A timed-out keyring write may still land: stop now
                        # rather than keep the lock through the browser wait.
                        if credential_store.stranded():
                            _fail(
                                context,
                                "credential_storage_failed",
                                str(storage_error),
                                storage_error.hint,
                            )
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


def _device_token(
    context: typer.Context,
    *,
    scope: str,
    label: str | None,
    wait_seconds: float | None,
    resume: bool,
    continue_command: str | None,
    store: StoreMode | None = None,
) -> tuple[str, LoginMetadata]:
    """Run the device flow; return the issued token and its metadata."""
    wait = default_wait_seconds() if wait_seconds is None else wait_seconds
    api_url = command_context(context).api_url
    with SolwynClient(api_url=api_url, credential=None) as client:
        if resume:
            try:
                stored = auth.load_pending_device()
            except CredentialStorageError as error:
                _fail(context, "credential_storage_failed", str(error), error.hint)
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
                persist=continue_command is not None,
                store=store,
            )
        _print_instructions(pending, waiting=wait > 0 or resume)
        token = _poll(
            context,
            client,
            pending,
            wait_seconds=wait,
            poll_first=resume,
            continue_command=continue_command,
            store=store,
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
    return token.access_token.get_secret_value(), meta


def device_login(
    context: typer.Context,
    *,
    scope: str = DEFAULT_SCOPE,
    label: str | None = None,
    wait_seconds: float | None = None,
    resume: bool = False,
    continue_command: str = "solwyn login --continue",
    store: StoreMode | None = None,
) -> LoginCredential:
    """Run the device flow and store the resulting CLI login.

    The store is resolved once, here, and pinned: the pending code and the
    final login go to that store, and if the selector changes during the
    browser wait the final write fails and the issued login is revoked.
    """
    try:
        pinned = store or auth.store_mode()
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error), error.hint)
    token, meta = _device_token(
        context,
        scope=scope,
        label=label,
        wait_seconds=wait_seconds,
        resume=resume,
        continue_command=continue_command,
        store=pinned,
    )
    try:
        credential = auth.store_login(token, meta, expect_mode=pinned)
    except Exception as error:  # noqa: BLE001 - never leave an issued login live.
        # The server issued a credential we cannot keep; do not leave it live.
        revoked = revoke_token(command_context(context).api_url, token)
        storage = isinstance(error, CredentialStorageError)
        _fail(
            context,
            "credential_storage_failed",
            str(error) if storage else "The new login could not be stored.",
            (error.hint if storage else None) if revoked else UNSTORED_LOGIN_HINT,
        )
    if note := credential_saved_note(credential):
        _note(note)
    if note := auth.legacy_entries_note(pinned):
        _note(note)
    return credential


def _login_data(credential: ResolvedCredential) -> _LoginData:
    store: Literal["keyring", "file"] = (
        "keyring" if credential.source == "keyring" else "file"
    )
    location = credential.location
    if isinstance(credential, ApiKeyCredential):
        return _LoginData(
            auth_type="project_key",
            store=store,
            location=location,
            credential_id=credential.key_id,
            label=None,
            scope=None,
            expires_at=None,
            account_id=credential.account_id,
            account_name=None,
            email=None,
            role=None,
            project_id=credential.project,
            project_name=credential.project_name,
        )
    if credential.meta is None:
        return _LoginData(
            auth_type="login",
            store=store,
            location=location,
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
        store=store,
        location=location,
        credential_id=meta.id,
        label=meta.label,
        scope=meta.scopes,
        expires_at=meta.expires_at,
        account_id=meta.account_id,
        account_name=meta.account_name,
        email=meta.email,
        role=meta.role,
    )


def _login_success(context: typer.Context, credential: ResolvedCredential) -> None:
    data = _login_data(credential)
    stored_in = (
        "Stored in",
        f"{data.store} ({data.location})" if data.location else data.store,
    )
    if isinstance(credential, ApiKeyCredential):
        rows: tuple[tuple[str, str], ...] = (
            ("Authentication", "project_key"),
            ("Project", f"{credential.project_name} ({credential.project})"),
            ("Key ID", credential.key_id or "-"),
            stored_in,
        )
    elif credential.meta is None:
        rows = (("Authentication", "login"), stored_in)
    else:
        meta = credential.meta
        rows = (
            ("Authentication", "login"),
            ("Account", f"{meta.account_name} ({meta.role})"),
            ("Email", meta.email),
            ("Label", meta.label),
            ("Scope", ", ".join(meta.scopes)),
            ("Expires", meta.expires_at.date().isoformat()),
            stored_in,
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


def store_supplied_key(
    context: typer.Context, raw_key: str, store: StoreMode | None
) -> ApiKeyCredential:
    """Check a supplied key with GET /api/v1/auth/key, then store it.

    The key is never a stored or SOLWYN_API_KEY key. It is checked against the
    selected project first, so a mismatch with the repo file or an unknown
    project stores nothing; it is stored under (API, project id) with the
    account the API says owns it.
    """
    try:
        supplied = ApiKeyCredential(
            api_key=raw_key.strip(), source="environment", location=None
        )
    except ValidationError:
        _fail(
            context,
            "invalid_api_key",
            "The API key is empty or malformed.",
            "Solwyn API keys start with solwyn_sk_. Keys from Solwyn CLI 0.4 and "
            "earlier (sk_proj_) stopped working; create one with solwyn keys create.",
            semantic=ErrorSemantic.USAGE,
        )
    legacy_keys_note(context)
    try:
        selection = resolve_project(context, purpose="with_key", supplied_key=supplied)
    except (ProjectResolutionError, ApiError, AuthError) as error:
        exit_for_resolution(context, error)
    bound = selection.credential
    if (
        not isinstance(bound, ApiKeyCredential)
        or bound.project is None
        or bound.key_id is None
        or bound.account_id is None
        or bound.project_name is None
    ):
        _fail(context, "invalid_auth_key_response", "The API key response was invalid.")
        raise AssertionError("unreachable")
    try:
        credential = auth.store_api_key(
            project_id=bound.project,
            key=bound.bearer_value(),
            key_id=bound.key_id,
            account_id=bound.account_id,
            project_name=bound.project_name,
            mode=store,
            api_url=command_context(context).api_url,
        )
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error), error.hint)
    except Exception:  # noqa: BLE001 - a structured error, never a traceback.
        _fail(context, "credential_storage_failed", "The API key could not be stored.")
    if note := credential_saved_note(credential):
        _note(note)
    if note := auth.legacy_entries_note(store):
        _note(note)
    return credential


def _login_with_key(context: typer.Context, store: StoreMode | None) -> None:
    """Read a key from a hidden prompt or stdin, never an argument; store it."""
    raw_key = (
        typer.prompt("API key", hide_input=True, err=True)
        if _stdin_is_tty()
        else sys.stdin.read().strip()
    )
    _login_success(context, store_supplied_key(context, raw_key, store))


def login(
    context: typer.Context,
    with_key: Annotated[
        bool,
        typer.Option(
            "--with-key",
            help="Check an API key from a hidden prompt or stdin and store it for the project.",
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
    store: Annotated[
        str | None,
        typer.Option(
            "--store",
            help=(
                "Where to store credentials: auto, keyring or file. Saved to config; "
                "nothing is copied between stores."
            ),
        ),
    ] = None,
    no_store: Annotated[
        bool,
        typer.Option(
            "--no-store",
            help="Print the new login token once for SOLWYN_TOKEN; store nothing.",
        ),
    ] = False,
) -> None:
    """Log in through the browser, or store an API key with --with-key."""
    if no_store and (with_key or resume or store is not None or wait == 0):
        _fail(
            context,
            "invalid_login_options",
            "--no-store cannot be combined with --with-key, --continue, --store or --wait 0.",
            semantic=ErrorSemantic.USAGE,
        )
    if with_key:
        if resume or label is not None or wait is not None or scope != "read,contain":
            _fail(
                context,
                "invalid_login_options",
                "--with-key cannot be combined with --scope, --label, --wait or --continue.",
                semantic=ErrorSemantic.USAGE,
            )
        _login_with_key(context, _selected_store(context, store))
        return
    requested = _SCOPES.get(scope.replace(" ", ""))
    if requested is None:
        _fail(
            context,
            "invalid_login_scope",
            "--scope must be read or read,contain.",
            semantic=ErrorSemantic.USAGE,
        )
    if no_store:
        token, meta = _device_token(
            context,
            scope=requested,
            label=label,
            wait_seconds=wait,
            resume=False,
            continue_command=None,
        )
        _print_unstored_token(context, token, meta)
        return
    legacy_keys_note(context, strict=False)
    credential = device_login(
        context,
        scope=requested,
        label=label,
        wait_seconds=wait,
        resume=resume,
        store=_selected_store(context, store),
    )
    _login_success(context, credential)


_STATUS_TEXT = {
    "cleared": "cleared",
    "unavailable": "not checked",
    "unreachable": "kept (not reachable)",
    "unread": "kept (not readable)",
}


def logout(context: typer.Context) -> None:
    """Revoke every stored login and clear this API's stored Solwyn credentials.

    Logout is the one exception to store isolation: whatever store is
    selected, it looks in both the credentials file and the keychain.
    """
    api_url = command_context(context).api_url
    legacy_keys_note(context, strict=False)
    try:
        report = auth.logout_everywhere(lambda token: revoke_token(api_url, token))
    except CredentialStorageError as error:
        _fail(context, "credential_storage_failed", str(error), error.hint)
    if report.revoked is False:
        _note(REVOKE_FAILED_NOTE)
    if any(item.status == "unavailable" for item in report.stores):
        _note(UNCHECKED_KEYRING_NOTE)
    for item in report.stores:
        if item.legacy_kept:
            _note(auth.legacy_kept_note(item.location, item.legacy_kept))
    if auth.TOKEN_ENV in os.environ:
        _note(
            "SOLWYN_TOKEN is set in this environment. Logout does not revoke it; "
            "use solwyn credentials revoke."
        )
    data = {
        # Only what was checked: an unavailable keyring is reported, not claimed.
        "cleared": all(item.cleared for item in report.stores if item.checked),
        "revoked": report.revoked,
        "logins_found": report.logins_found,
        "stores": [
            {
                "store": item.kind,
                "location": item.location,
                "status": item.status,
                "cleared": item.cleared,
                "kept": list(item.kept),
                "legacy_cleared": list(item.legacy_cleared),
                "legacy_kept": list(item.legacy_kept),
            }
            for item in report.stores
        ],
    }
    retained = [item for item in report.stores if item.retained]
    if retained:
        places = ", ".join(f"{item.location} ({item.status})" for item in retained)
        _fail(
            context,
            "logout_incomplete",
            f"Logout kept credentials it could not reach or read: {places}.",
            "Unlock or repair that store and run solwyn logout again. Nothing that "
            "could not be read was deleted or revoked.",
            detail={"schema": "solwyn.logout.v1", **data},
            semantic=ErrorSemantic.AUTH,
        )
    output_engine(context).write_success(
        schema="solwyn.logout.v1",
        data=data,
        table=TableData(
            columns=("Credentials", "Status"),
            rows=(
                *(
                    (f"{item.kind} ({item.location})", _STATUS_TEXT[item.status])
                    for item in report.stores
                ),
                *(
                    (
                        f"Solwyn CLI 0.3 credentials ({item.location})",
                        f"cleared ({', '.join(item.legacy_cleared)})",
                    )
                    for item in report.stores
                    if item.legacy_cleared
                ),
                *(
                    (
                        f"Solwyn CLI 0.3 credentials ({item.location})",
                        f"kept ({', '.join(item.legacy_kept)})",
                    )
                    for item in report.stores
                    if item.legacy_kept
                ),
                *(
                    (("login", "revoked" if report.revoked else "not revoked"),)
                    if report.revoked is not None
                    else ()
                ),
            ),
            right_align=("Status",),
        ),
    )
