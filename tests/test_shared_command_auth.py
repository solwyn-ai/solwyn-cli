"""Structural guard for one shared command credential resolver."""

from solwyn_cli.commands import _shared, budget, costs, projects, status


def test_read_only_commands_use_one_shared_credential_resolver() -> None:
    assert hasattr(_shared, "required_credential")
    for command_module in (budget, costs, projects, status):
        assert command_module.required_credential is _shared.required_credential
        assert not hasattr(command_module, "_credential")
