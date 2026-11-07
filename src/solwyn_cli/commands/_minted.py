"""Keep a secret the server just minted: store it, show it once, or revoke it.

Core returns a new API key exactly once. From then on the key must end up
stored on this computer, shown once to the person who asked for it, or
revoked; never live and held by nobody. ``keys rotate`` shows its replacement
when the store fails, and device login revokes a login it cannot store; this
is the same rule for every command that creates a key.

Showing is safe only on a terminal: a run whose stdout is a pipe, a log or an
agent asked for the key to be stored, not printed. Ctrl+C (or any other
interrupt) is never a moment to print a secret, so an interrupted run revokes.
"""

import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any
from urllib.parse import quote

import typer
from pydantic import SecretStr

from solwyn_cli.approvals import approval_settings
from solwyn_cli.auth import ApiKeyCredential, AuthError
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OneTimeSecretPanel, OutputError, _terminal_safe


def _stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def _note(text: str) -> None:
    typer.echo(_terminal_safe(text), err=True)


class MintedKey:
    """A key Core just returned; it leaves this run stored, shown or revoked.

    Use it as a context manager around everything from the store to the
    output that shows the key: if the block exits early while the key is
    neither stored nor shown (an error exit, Ctrl+C), the key is revoked.
    """

    def __init__(
        self,
        *,
        key: SecretStr,
        key_id: str,
        project: str,
        api_url: str,
        credential: object,
        client: Callable[..., Any],
    ) -> None:
        self.key = key
        self.key_id = key_id
        self._project = project
        self._api_url = api_url
        self._credential = credential
        self._client = client
        self.stored: ApiKeyCredential | None = None
        # Neither stored nor shown yet: an exit now must revoke.
        self._pending = True

    @property
    def needs_showing(self) -> bool:
        """The store failed: the key must be shown once by the caller."""
        return self._pending and self.stored is None

    def __enter__(self) -> "MintedKey":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if kind is None or not self._pending:
            return
        self._pending = False
        revoked = self.revoke()
        reason = "Interrupted" if not isinstance(error, Exception) else "Stopped early"
        _note(
            f"{reason} before the new key {self.key_id} was stored or shown; "
            + (
                "it was revoked."
                if revoked
                else f"it could not be revoked. Run solwyn keys revoke {self.key_id} --yes."
            )
        )

    def store(
        self,
        write: Callable[[], ApiKeyCredential],
        context: typer.Context,
        *,
        hint: str,
    ) -> None:
        """Store the key; when the store fails, show it once or revoke it.

        On a terminal a failed store leaves the key for the caller to show
        (``needs_showing``). Anywhere else the key is revoked and the run exits
        3 ``credential_storage_failed`` with ``hint`` (what to run next).
        """
        try:
            self.stored = write()
        except Exception as error:  # noqa: BLE001 - the new key must not be lost.
            reason = (
                str(error)
                if isinstance(error, AuthError)
                else "The credential store failed."
            )
            if _stdout_is_terminal():
                _note(
                    f"Could not store the new key: {reason} It is shown once below; "
                    f"store it with solwyn --project {self._project} login --with-key."
                )
                return
            self._pending = False
            revoked = self.revoke()
            exit_with_error(
                context,
                OutputError(
                    code="credential_storage_failed",
                    message=f"{reason} "
                    + (
                        f"The new key {self.key_id} was revoked, because output "
                        "that is not a terminal never shows a key."
                        if revoked
                        else f"The new key {self.key_id} could not be stored or "
                        f"revoked; revoke it with solwyn keys revoke {self.key_id} --yes."
                    ),
                    hint=(
                        f"{error.hint} {hint}"
                        if isinstance(error, AuthError) and error.hint
                        else hint
                    ),
                ),
                semantic=ErrorSemantic.AUTH,
            )
        else:
            self._pending = False

    def panel(self, title: str, footer: str | None = None) -> OneTimeSecretPanel | None:
        """The one-time panel for a key whose store failed, else None."""
        if not self.needs_showing:
            return None
        return OneTimeSecretPanel(
            title=title,
            value=self.key,
            footer=None if footer is None else SecretStr(footer),
        )

    def reveal(self) -> dict[str, SecretStr] | None:
        """The JSON ``key`` field for a key whose store failed, else None."""
        return {"key": self.key} if self.needs_showing else None

    def shown(self) -> None:
        """The output carrying the key was written."""
        self._pending = False

    def revoke(self) -> bool:
        """Revoke the key once, best effort, without waiting for an approval."""
        try:
            with (
                approval_settings(None),
                self._client(
                    api_url=self._api_url, credential=self._credential
                ) as client,
            ):
                response = client.request(
                    "DELETE",
                    f"/api/v1/keys/{quote(self.key_id, safe='')}",
                    retry_rate_limit=False,
                )
        except Exception:  # noqa: BLE001 - best effort; the caller reports it.
            return False
        return response.status_code == 204
