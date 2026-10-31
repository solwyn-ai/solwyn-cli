"""Per-invocation settings for the browser approval hand-off."""

import re
import shlex
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

try:
    from typer._click.globals import get_current_context
except ImportError:  # Typer releases before the bundled Click boundary.
    from click import get_current_context

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


def may_need_approval(method: str, path: str) -> bool:
    """Return whether a request can be a change that needs a browser approval."""
    route = path.split("?", 1)[0].strip("/")
    # The approval gate guards only /api/v1; OAuth and health routes never ask.
    return (
        method.upper() != "GET"
        and route.startswith("api/v1/")
        and not _NO_APPROVAL_ROUTE.fullmatch(route)
    )


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
    # Generated values a re-run must repeat, keyed by parameter name, and notes
    # for inputs the CLI never repeats itself (secrets read from stdin).
    pins: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # A command that finishes this run's remaining work, as (project, argv),
    # when re-running the original command can no longer do it.
    continuation: tuple[str, list[str]] | None = None

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


def pin_retry_value(name: str, value: str) -> None:
    """Make the printed retry command repeat a value this run generated."""
    settings = current_settings()
    if settings is not None:
        settings.pins[name] = value


def add_retry_note(note: str) -> None:
    """Tell the user how to supply an input the retry command cannot carry."""
    settings = current_settings()
    if settings is not None and note not in settings.notes:
        settings.notes.append(note)


def continue_with(command: list[str], *, project: str, note: str) -> None:
    """Make the printed retry finish this run's remaining step instead."""
    settings = current_settings()
    if settings is not None:
        settings.continuation = (project, command)
        settings.notes = [note]


def _option_text(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _render_params(
    context: object,
    pins: Mapping[str, str],
    skip: set[str],
    *,
    leaf: bool = False,
) -> list[str]:
    options: list[str] = []
    arguments: list[str] = []
    for param in context.command.params:  # type: ignore[attr-defined]
        name = param.name
        if name in skip:
            continue
        if name in pins:
            value: object = pins[name]
        else:
            source = context.get_parameter_source(name)  # type: ignore[attr-defined]
            if source is None or source.name != "COMMANDLINE":
                continue
            value = context.params.get(name)  # type: ignore[attr-defined]
        if getattr(param, "param_type_name", "") != "option":
            values = value if isinstance(value, (list, tuple)) else [value]
            arguments += [_option_text(item) for item in values]
            continue
        flag = max(
            (opt for opt in param.opts if opt.startswith("--")),
            key=len,
            default=param.opts[0],
        )
        if getattr(param, "is_flag", False):
            if value:
                options.append(flag)
            elif param.secondary_opts:
                options.append(param.secondary_opts[0])
            continue
        values = value if param.multiple else [value]
        for item in values or ():
            options += [flag, _option_text(item)]
    if leaf and arguments:
        # Positionals can look like options ("--help", "-x"); "--" keeps them data.
        return [*options, "--", *arguments]
    return options + arguments


def render_leaf(context: object, pins: Mapping[str, str]) -> list[str]:
    """Render a leaf command's own options and positionals for a re-run."""
    return _render_params(context, pins, set(), leaf=True)


def retry_argv(approval_id: str) -> list[str] | None:
    """Rebuild this invocation as an exact re-run that carries the approval.

    Only values the user typed, plus pinned generated values, are repeated, so
    the re-run rebuilds byte-identical requests. Nothing is stored.
    """
    settings = current_settings()
    try:
        context = get_current_context()
    except RuntimeError:
        return None
    chain = []
    while context is not None:
        chain.append(context)
        context = context.parent
    chain.reverse()
    pins = settings.pins if settings is not None else {}
    root_skip = {"approval", "approval_wait", "version"}
    argv = ["solwyn", "--approval", approval_id]
    if settings is not None and settings.continuation is not None:
        project, command = settings.continuation
        argv += _render_params(chain[0], {}, root_skip | {"project"})
        return [*argv, "--project", project, *command]
    argv += _render_params(chain[0], {}, root_skip)
    for index, context in enumerate(chain[1:], start=1):
        argv.append(context.info_name)
        argv += _render_params(context, pins, set(), leaf=index == len(chain) - 1)
    return argv


def retry_command(argv: list[str]) -> str:
    """Return the POSIX shell form of a retry command."""
    return shlex.join(argv)
