"""Stable, machine-legible CLI help contract formatting."""


def help_contract(
    *,
    endpoint: str,
    auth: str,
    exits: str,
    schema: str,
    guidance: tuple[str, ...] = (),
) -> str:
    """Render one semantic help contract without terminal-specific formatting."""
    contract = (
        f"Endpoint: {endpoint}",
        f"Auth: {auth}",
        f"Exit codes: {exits}",
        f"JSON schema: {schema}",
    )
    return "\n".join((*contract, *guidance))
