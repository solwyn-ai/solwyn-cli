"""Render deterministic agent-facing Solwyn capability guidance."""

import typer

from solwyn_cli.artifacts import ArtifactSurface, render_surface
from solwyn_cli.help import help_contract


AGENT_CONTEXT_HELP = help_contract(
    endpoint="local capability artifact",
    auth="local (none)",
    exits="0 success; 2 usage",
    schema="none (plain text capability document)",
)


def agent_context() -> None:
    """Print safe Solwyn budget guidance for coding agents."""
    typer.echo(render_surface(ArtifactSurface.AGENT_CONTEXT), nl=False)
