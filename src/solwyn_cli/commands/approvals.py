"""Inspect browser approvals for CLI changes."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import typer
from pydantic import ValidationError

from solwyn_cli.auth import AuthError, ApiKeyCredential, resolve_credential
from solwyn_cli.client import (
    ApprovalView,
    SolwynClient,
    approval_error,
    wait_for_approval,
)
from solwyn_cli.commands._shared import exit_with_error, required_credential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.help import help_contract
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine

app = typer.Typer(
    help="Inspect browser approvals requested by this CLI login.",
    epilog=help_contract(
        endpoint="composite router for approval subcommands",
        auth="login",
        exits="0 success; 2 usage",
        schema="varies by subcommand",
    ),
)

# Injectable for tests; production waits on the wall clock.
sleep: Callable[[float], object] = time.sleep


def now() -> datetime:
    return datetime.now(UTC)


def _effective_status(view: ApprovalView) -> str:
    if view.status in {"pending", "approved"} and view.expires_at <= now():
        return "expired"
    return view.status


def _table(view: ApprovalView, status: str) -> TableData:
    rows: list[tuple[str, str]] = [
        ("ID", view.id),
        ("Status", status),
        ("Title", view.title),
        ("Action", view.action),
        ("Reasons", ", ".join(view.reasons) or "none"),
    ]
    rows.extend(
        (change.label, f"{change.before or 'none'} -> {change.after or 'none'}")
        for change in view.changes
    )
    rows.append(("Expires", view.expires_at.isoformat()))
    return TableData(columns=("Field", "Value"), rows=tuple(rows), fold_cells=True)


@app.command(
    "status",
    epilog=help_contract(
        endpoint="GET /api/v1/cli/approvals/{approval_id}",
        auth="login (only your own approvals)",
        exits=(
            "0 shown; 1 generic/API; 2 usage; 3 auth; 5 not found; "
            "6 denied, expired, or still pending after --wait"
        ),
        schema="solwyn.approval.v1",
        guidance=("--wait polls every 3 s until a decision or the wait ends.",),
    ),
)
def status(
    context: typer.Context,
    approval_id: Annotated[str, typer.Argument(help="Approval ID.")],
    wait: Annotated[
        int,
        typer.Option(
            "--wait", min=0, help="Seconds to wait for a decision. 0 shows it now."
        ),
    ] = 0,
) -> None:
    """Show an approval and optionally wait for its decision."""
    credential = required_credential(context, resolve=resolve_credential)
    if isinstance(credential, ApiKeyCredential):
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Approvals belong to a Solwyn login.",
                hint="Run solwyn login, then retry.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    try:
        with SolwynClient(
            api_url=command_context(context).api_url, credential=credential
        ) as client:
            view = client.read_approval(approval_id)
            if wait > 0:
                latest = view

                def read() -> ApprovalView:
                    nonlocal latest
                    latest = client.read_approval(approval_id)
                    return latest

                wait_for_approval(
                    read=read,
                    initial=view,
                    wait_seconds=wait,
                    sleeper=sleep,
                    clock=now,
                )
                view = latest
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_approval_response",
                message="The approval response was invalid.",
            ),
        )
    current = _effective_status(view)
    if current == "denied":
        exit_with_error(
            context,
            approval_error(
                "approval_denied",
                "The change was denied in the browser.",
                hint=None,
                approval_id=view.id,
            ),
        )
    if current == "expired":
        exit_with_error(
            context,
            approval_error(
                "approval_expired",
                "The approval expired.",
                hint="Run the command again to request a new approval.",
                approval_id=view.id,
            ),
        )
    if current == "pending" and wait > 0:
        exit_with_error(
            context,
            approval_error(
                "approval_pending",
                f"Still waiting for approval {view.id}.",
                hint=(
                    f"After approving, run the same command again with --approval {view.id}."
                ),
                approval_id=view.id,
            ),
        )
    output_engine(context).write_success(
        schema="solwyn.approval.v1",
        data={**view.model_dump(mode="json"), "status": current},
        table=_table(view, current),
    )
