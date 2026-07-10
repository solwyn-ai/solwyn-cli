"""Resolved global command-context precedence tests."""

import pytest

from solwyn_cli import runtime
from solwyn_cli.config import Config
from solwyn_cli.output import OutputFormat


@pytest.mark.parametrize(
    (
        "api_url",
        "project",
        "output_format",
        "environment",
        "expected_url",
        "expected_project",
        "expected_format",
    ),
    [
        (
            "https://explicit.example",
            "proj_explicit",
            "table",
            {
                "SOLWYN_API_URL": "https://environment.example",
                "SOLWYN_PROJECT_ID": "proj_environment",
                "SOLWYN_FORMAT": "json",
            },
            "https://explicit.example",
            "proj_explicit",
            OutputFormat.TABLE,
        ),
        (
            None,
            None,
            None,
            {
                "SOLWYN_API_URL": "https://environment.example",
                "SOLWYN_PROJECT_ID": "proj_environment",
                "SOLWYN_FORMAT": "json",
            },
            "https://environment.example",
            "proj_environment",
            OutputFormat.JSON,
        ),
        (
            None,
            None,
            None,
            {},
            "https://configured.example",
            "proj_configured",
            OutputFormat.TABLE,
        ),
    ],
)
def test_global_context_uses_explicit_environment_config_precedence(
    monkeypatch: pytest.MonkeyPatch,
    api_url: str | None,
    project: str | None,
    output_format: str | None,
    environment: dict[str, str],
    expected_url: str,
    expected_project: str,
    expected_format: OutputFormat,
) -> None:
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(
            api_url="https://configured.example",
            default_project="proj_configured",
            format="table",
        ),
    )

    resolved = runtime.resolve_command_context(
        api_url=api_url,
        project=project,
        output_format=output_format,
        json_flag=False,
        no_color=False,
        environ=environment,
    )

    assert resolved.api_url == expected_url
    assert resolved.project == expected_project
    assert resolved.output_format is expected_format
