"""Stable, machine-legible CLI help contract formatting."""

import re

from solwyn_cli.approvals import may_need_approval

APPROVAL_EXIT = "6 browser approval required, pending, denied or expired"
_WRITE = re.compile(r"\b(POST|PUT|PATCH|DELETE) (/[^\s;,)]+)")


def help_contract(
    *,
    endpoint: str,
    auth: str,
    exits: str,
    schema: str,
    guidance: tuple[str, ...] = (),
) -> str:
    """Render one semantic help contract without terminal-specific formatting.

    A command whose endpoint names a change that can need a browser approval
    also lists exit 6.
    """
    if not re.search(r"\b6 ", exits) and any(
        may_need_approval(method, route) for method, route in _WRITE.findall(endpoint)
    ):
        exits = f"{exits}; {APPROVAL_EXIT}"
    contract = (
        f"Endpoint: {endpoint}",
        f"Auth: {auth}",
        f"Exit codes: {exits}",
        f"JSON schema: {schema}",
    )
    return "\n".join((*contract, *guidance))
