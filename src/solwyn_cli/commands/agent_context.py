"""Render deterministic agent-facing Solwyn capability guidance."""

import typer

from solwyn_cli.artifacts import ArtifactSurface, render_surface


def agent_context() -> None:
    """Print safe Solwyn budget guidance for coding agents."""
    typer.echo(render_surface(ArtifactSurface.AGENT_CONTEXT), nl=False)
