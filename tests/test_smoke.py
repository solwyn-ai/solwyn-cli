"""Smoke tests for the installed CLI package."""

from importlib.metadata import version

from typer import Typer

from solwyn_cli import __version__
from solwyn_cli.main import app


def test_package_exposes_versioned_typer_app() -> None:
    """The packaged CLI foundation is importable by CI."""
    assert __version__ == version("solwyn-cli") == "0.4.0"
    assert isinstance(app, Typer)
