"""Per-invocation settings for the browser approval hand-off."""

import base64
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from solwyn_cli import auth

APPROVAL_HEADER = "Solwyn-Approval"
APPROVAL_POLL_SECONDS = 3.0
INTERACTIVE_WAIT_SECONDS = 600
NON_INTERACTIVE_WAIT_SECONDS = 90


# Non-GET routes that never need an approval (spec section 8): read-only POSTs,
# metering, and a login managing itself. --approval skips them and attaches to
# the actual change.
_NO_APPROVAL_ROUTE = re.compile(
    r"api/v1/("
    r"account/alerts/(latest-events|preview)"
    r"|projects/[^/]+/(alerts/preview|costs/simulate"
    r"|providers/breaker-reports|untracked-surfaces)"
    r"|billing/plan-preview|budgets/.+|metadata/ingest|untracked-surfaces"
    r"|cli/credentials(/[^/]+)?"
    r")"
)
_PREPARED_PREFIX = "approval_request:"


def may_need_approval(method: str, path: str) -> bool:
    """Return whether a request can be a change that needs a browser approval."""
    route = path.split("?", 1)[0].strip("/")
    # The approval gate guards only /api/v1; OAuth and health routes never ask.
    return (
        method.upper() != "GET"
        and route.startswith("api/v1/")
        and not _NO_APPROVAL_ROUTE.fullmatch(route)
    )


class _PreparedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    raw_path: str
    content_type: str | None
    body: str


def remember_change(approval_id: str, request: httpx.Request) -> None:
    """Keep the exact request an approval was bound to, so a retry re-sends it.

    Bodies can hold secrets and generated values (request IDs, relative times),
    so they go to the keychain. Best effort: without it a retry rebuilds.
    """
    prepared = _PreparedChange(
        method=request.method,
        path=request.url.path,
        raw_path=request.url.raw_path.decode("latin-1"),
        content_type=request.headers.get("Content-Type"),
        body=base64.b64encode(request.read()).decode("ascii"),
    )
    try:
        auth._set_password(_PREPARED_PREFIX + approval_id, prepared.model_dump_json())
    except auth.CredentialStorageError:
        pass


def recall_change(approval_id: str, request: httpx.Request) -> httpx.Request | None:
    """Return the remembered request for this approval if it is the same change."""
    available, raw = auth._get_password(_PREPARED_PREFIX + approval_id)
    if not available or raw is None:
        return None
    try:
        prepared = _PreparedChange.model_validate_json(raw)
        body = base64.b64decode(prepared.body, validate=True)
    except (ValidationError, ValueError):
        return None
    if prepared.method != request.method or prepared.path != request.url.path:
        return None
    headers = request.headers.copy()
    if prepared.content_type is None:
        headers.pop("Content-Type", None)
    else:
        headers["Content-Type"] = prepared.content_type
    headers.pop("Content-Length", None)
    return httpx.Request(
        request.method,
        request.url.copy_with(raw_path=prepared.raw_path.encode("latin-1")),
        headers=headers,
        content=body,
        extensions=request.extensions,
    )


def forget_change(approval_id: str) -> None:
    """Drop a remembered request. Best effort."""
    auth._delete_password(_PREPARED_PREFIX + approval_id)


def default_wait_seconds() -> int:
    """Wait 600 s when stdin and stderr are terminals, else 90 s."""
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    return INTERACTIVE_WAIT_SECONDS if interactive else NON_INTERACTIVE_WAIT_SECONDS


def _write_stderr(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


@dataclass
class ApprovalSettings:
    """How one CLI invocation waits for, and attaches, browser approvals.

    Library and MCP callers have no settings: an approval_required answer is
    returned as an error with its approval URL and nothing waits.
    """

    wait_seconds: float
    approval_id: str | None = None
    write: Callable[[str], None] = field(default=_write_stderr)
    approval_used: bool = False
    # Keep a timed-out change in the keychain for a later --approval retry.
    remember: bool = False

    def take_approval_id(self) -> str | None:
        """Return the --approval ID once, for the first non-GET request."""
        if self.approval_used:
            return None
        self.approval_used = True
        return self.approval_id


_SETTINGS: ContextVar[ApprovalSettings | None] = ContextVar(
    "solwyn_approval_settings", default=None
)


def current_settings() -> ApprovalSettings | None:
    """Return the active invocation's approval settings, if any."""
    return _SETTINGS.get()


def activate(settings: ApprovalSettings) -> Callable[[], None]:
    """Install settings for this invocation and return the matching reset."""
    token = _SETTINGS.set(settings)
    return lambda: _SETTINGS.reset(token)


@contextmanager
def approval_settings(settings: ApprovalSettings | None) -> Iterator[None]:
    """Scope approval settings to a block."""
    token = _SETTINGS.set(settings)
    try:
        yield
    finally:
        _SETTINGS.reset(token)
