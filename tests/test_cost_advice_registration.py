"""Registration and help contracts for cost analysis and pricing commands."""

from typer.testing import CliRunner

from solwyn_cli.main import app


runner = CliRunner()


def test_cost_advice_commands_are_registered_without_priced_models_window_flags() -> (
    None
):
    costs_help = runner.invoke(app, ["costs", "--help"])

    assert costs_help.exit_code == 0
    for command in ("baseline", "unit-economics", "drift", "priced-models", "simulate"):
        assert command in costs_help.stdout

    priced_help = runner.invoke(app, ["costs", "priced-models", "--help"])

    assert priced_help.exit_code == 0
    assert "--range" not in priced_help.stdout
    assert "--from" not in priced_help.stdout
    assert "--to" not in priced_help.stdout
