"""Per-invocation settings for the browser approval hand-off."""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

APPROVAL_HEADER = "Solwyn-Approval"
APPROVAL_POLL_SECONDS = 3.0
INTERACTIVE_WAIT_SECONDS = 600
NON_INTERACTIVE_WAIT_SECONDS = 90


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
