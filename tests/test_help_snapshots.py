"""Semantic snapshots for every registered CLI help surface."""

from collections.abc import Iterator

import click
from typer.main import get_command
from typer.testing import CliRunner

from solwyn_cli.main import app


runner = CliRunner()


HELP_CONTRACTS = {
    (): {
        "endpoint": "composite command router",
        "auth": "varies by command",
        "exits": "0 success; 2 usage",
        "schema": "varies by command",
    },
    ("agent-context",): {
        "endpoint": "local capability artifact",
        "auth": "local (none)",
        "exits": "0 success; 2 usage",
        "schema": "none (plain text capability document)",
    },
    ("budget",): {
        "endpoint": "composite router for budget subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("budget", "check"): {
        "endpoint": "POST /api/v1/budgets/check",
        "auth": "key",
        "exits": "0 allowed or fail-open; 1 generic/API; 2 usage; 3 auth; 4 budget denial",
        "schema": "solwyn.budget-check.v1",
    },
    ("budget", "confirm"): {
        "endpoint": "POST /api/v1/budgets/confirm",
        "auth": "key",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 reservation not found",
        "schema": "solwyn.budget-confirm.v1",
    },
    ("budget", "set"): {
        "endpoint": "GET then PUT /api/v1/projects/{project}/budget",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget-set.v1",
    },
    ("budget", "status"): {
        "endpoint": "GET /api/v1/projects/{project}/budget",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 4 utilization gate; 5 not found",
        "schema": "solwyn.budget-status.v1",
    },
    ("config",): {
        "endpoint": "local configuration router",
        "auth": "local (none)",
        "exits": "0 success; 2 usage",
        "schema": "none (subcommands emit plain text)",
    },
    ("config", "get"): {
        "endpoint": "local configuration read",
        "auth": "local (none)",
        "exits": "0 success; 2 usage",
        "schema": "none (plain text value)",
    },
    ("config", "list"): {
        "endpoint": "local configuration read",
        "auth": "local (none)",
        "exits": "0 success; 2 usage",
        "schema": "none (plain text key/value list)",
    },
    ("config", "set"): {
        "endpoint": "local configuration write",
        "auth": "local (none)",
        "exits": "0 success; 2 usage",
        "schema": "none (human confirmation on stderr)",
    },
    ("costs",): {
        "endpoint": "GET /api/v1/projects/{project}/costs",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.v1",
    },
    ("init",): {
        "endpoint": "composite onboarding workflow",
        "auth": "any or interactive bootstrap",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.init.v1",
    },
    ("keys",): {
        "endpoint": "composite router for project-key subcommands",
        "auth": "JWT",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("keys", "revoke"): {
        "endpoint": "DELETE /api/v1/projects/{project}/key/{key_id}",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.keys-revoke.v1 (CLI output after bodyless 204)",
    },
    ("keys", "rotate"): {
        "endpoint": "POST /api/v1/projects/{project}/key/rotate",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.keys-rotate.v1",
    },
    ("keys", "show"): {
        "endpoint": "GET /api/v1/projects/{project}/key",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.keys-show.v1",
    },
    ("login",): {
        "endpoint": "POST /api/v1/auth/login or local keychain write with --with-key",
        "auth": "none",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth or credential storage",
        "schema": "solwyn.login.v1",
    },
    ("logout",): {
        "endpoint": "local keychain clear",
        "auth": "local stored credentials",
        "exits": "0 success; 2 usage; 3 credential storage",
        "schema": "solwyn.logout.v1",
    },
    ("projects",): {
        "endpoint": "composite router for project subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("projects", "create"): {
        "endpoint": "POST /api/v1/projects",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.projects-create.v1",
    },
    ("projects", "list"): {
        "endpoint": "GET /api/v1/projects",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.projects-list.v1",
    },
    ("projects", "show"): {
        "endpoint": "GET /api/v1/projects/{project}",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.projects-show.v1",
    },
    ("status",): {
        "endpoint": "GET /api/v1/projects/{project}/budget and GET /api/v1/projects/{project}/providers",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.status.v1",
    },
    ("whoami",): {
        "endpoint": "local key identity or GET /api/v1/account/me and GET /api/v1/projects",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.whoami.v1",
    },
}


def _commands(
    command: object, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    yield path, command
    children = getattr(command, "commands", {})
    for name, child in sorted(children.items()):
        yield from _commands(child, (*path, name))


def _normalized_help(path: tuple[str, ...]) -> str:
    result = runner.invoke(app, [*path, "--help"], color=False)
    assert result.exit_code == 0, result.output
    return " ".join(click.unstyle(result.output).split())


def test_semantic_help_snapshots_cover_the_full_registered_tree() -> None:
    discovered = {path for path, _ in _commands(get_command(app))}

    assert discovered == set(HELP_CONTRACTS)
    for path, contract in HELP_CONTRACTS.items():
        rendered = _normalized_help(path)
        assert f"Endpoint: {contract['endpoint']}" in rendered
        assert f"Auth: {contract['auth']}" in rendered
        assert f"Exit codes: {contract['exits']}" in rendered
        assert f"JSON schema: {contract['schema']}" in rendered


def test_top_level_help_routes_agents_to_the_capability_document() -> None:
    assert "Agents: run solwyn agent-context" in _normalized_help(())


def test_budget_help_preserves_guardrail_semantics() -> None:
    check_help = _normalized_help(("budget", "check"))
    confirm_help = _normalized_help(("budget", "confirm"))
    status_help = _normalized_help(("budget", "status"))

    assert "immediately before caller-made un-instrumented provider spend" in check_help
    assert "Do not use with Solwyn SDK-instrumented calls" in check_help
    assert "Do not poll with check" in check_help
    assert "only when check returned a non-null reservation" in confirm_help
    assert "Skip confirm when the reservation is null" in confirm_help
    assert "Pure read/poll; never reserves or authorizes spend" in status_help
    assert "Poll with status, never check" in status_help


def test_auth_and_key_help_is_secret_safe() -> None:
    login_help = _normalized_help(("login",))
    keys_help = " ".join(
        _normalized_help(path)
        for path in (("keys",), ("keys", "rotate"), ("keys", "revoke"))
    )

    assert (
        "Keys and passwords are never accepted as command-line arguments or echoed"
        in login_help
    )
    assert "Read project keys only from a hidden prompt or stdin" in login_help
    assert "never reveals existing key material" in keys_help
    assert "replacement key is shown exactly once" in keys_help


def test_init_help_names_each_remote_and_local_operation() -> None:
    init_help = _normalized_help(("init",))

    assert "conditional POST /api/v1/auth/login" in init_help
    assert "GET /api/v1/projects" in init_help
    assert "optional POST /api/v1/projects" in init_help
    assert "final GET /api/v1/projects/{project}/budget" in init_help
    assert "write local configuration" in init_help
    assert "scan Python imports" in init_help
    assert "render SDK snippets" in init_help
    assert "optionally write agent artifacts with --agents" in init_help
