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
        "expected_url_source",
    ),
    [
        (
            "https://explicit.example",
            "proj_a45c2264b8cab0f1e0637f6c",
            "table",
            {
                "SOLWYN_API_URL": "https://environment.example",
                "SOLWYN_PROJECT_ID": "proj_c6e454d960df4845f9d69eb1",
                "SOLWYN_FORMAT": "json",
            },
            "https://explicit.example",
            "proj_a45c2264b8cab0f1e0637f6c",
            OutputFormat.TABLE,
            "cli",
        ),
        (
            None,
            None,
            None,
            {
                "SOLWYN_API_URL": "https://environment.example",
                "SOLWYN_PROJECT_ID": "proj_c6e454d960df4845f9d69eb1",
                "SOLWYN_FORMAT": "json",
            },
            "https://environment.example",
            "proj_c6e454d960df4845f9d69eb1",
            OutputFormat.JSON,
            "environment",
        ),
        (
            None,
            None,
            None,
            {},
            "https://configured.example",
            "proj_3be9f957f29f905f10f7b652",
            OutputFormat.TABLE,
            "config",
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
    expected_url_source: str,
) -> None:
    monkeypatch.setattr(
        runtime,
        "load_config",
        lambda: Config(
            api_url="https://configured.example",
            default_project="proj_3be9f957f29f905f10f7b652",
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
    assert resolved.api_url_source == expected_url_source
    assert resolved.config_load_failed is False


@pytest.mark.parametrize(
    ("api_url", "environment", "expected_url", "expected_url_source"),
    [
        (None, {}, "https://api.solwyn.ai", "fallback"),
        (
            "https://explicit.example",
            {},
            "https://explicit.example",
            "cli",
        ),
        (
            None,
            {"SOLWYN_API_URL": "https://environment.example"},
            "https://environment.example",
            "environment",
        ),
    ],
)
def test_allowed_config_error_preserves_api_url_provenance(
    monkeypatch: pytest.MonkeyPatch,
    api_url: str | None,
    environment: dict[str, str],
    expected_url: str,
    expected_url_source: str,
) -> None:
    def fail_config_load() -> Config:
        raise runtime.ConfigLoadError("sensitive malformed config detail")

    monkeypatch.setattr(runtime, "load_config", fail_config_load)

    resolved = runtime.resolve_command_context(
        api_url=api_url,
        project=None,
        output_format=None,
        json_flag=False,
        no_color=False,
        allow_config_error=True,
        environ=environment,
    )

    assert resolved.api_url == expected_url
    assert resolved.api_url_source == expected_url_source
    assert resolved.config_load_failed is True
