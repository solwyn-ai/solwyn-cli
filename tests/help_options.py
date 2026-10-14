"""Parser checks for semantic help tests; strict output goldens stay separate."""

from typer.core import TyperOption
from typer.main import get_command

from solwyn_cli.main import app


def assert_public_bool_flag(path: list[str], flag: str) -> None:
    command = get_command(app)
    for name in path:
        command = command.commands[name]
    options = [
        param
        for param in command.params
        if isinstance(param, TyperOption) and flag in param.opts
    ]
    assert len(options) == 1, f"Missing real parser option: {flag} on {path}"
    option = options[0]
    assert option.is_flag and option.is_bool_flag, f"Not a boolean flag: {flag}"
    assert not option.hidden, f"Hidden parser option: {flag}"
