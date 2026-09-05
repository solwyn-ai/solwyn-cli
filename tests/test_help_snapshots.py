"""Semantic snapshots for every registered CLI help surface."""

from collections.abc import Iterator

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from solwyn_cli.main import app


runner = CliRunner()


HELP_CONTRACTS = {
    ("costs", "events"): {
        "endpoint": "GET /api/v1/projects/{project}/costs/events",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.events.v1",
    },
    ("costs", "total"): {
        "endpoint": "GET /api/v1/projects/{project}/costs/total",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.total.v1",
    },
    ("costs", "export"): {
        "endpoint": "GET /api/v1/projects/{project}/costs/export",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "stream (CSV or NDJSON)",
    },
    ("budget", "rules"): {
        "endpoint": "GET /api/v1/projects/{project_id}/budget",
        "auth": "key or JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rules", "list"): {
        "endpoint": "GET /api/v1/projects/{project_id}/budget",
        "auth": "key or JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rule"): {
        "endpoint": "composite budget rule router",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold"): {
        "endpoint": "composite budget threshold router",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "add"): {
        "endpoint": "GET then PUT /api/v1/projects/{project_id}/budget",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "restore-defaults"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/restore-defaults; GET /api/v1/projects/{project_id}/budget",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rule", "preview"): {
        "endpoint": "POST /api/v1/projects/{project_id}/alerts/preview",
        "auth": "key or JWT",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_preview.v1",
    },
    ("budget", "rule", "remove"): {
        "endpoint": "DELETE /api/v1/projects/{project_id}/budget/rule/{rule_key}",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "pause"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/pause",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "resume"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/resume",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "snooze"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/snooze",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "unsnooze"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/unsnooze",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold", "snooze"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/threshold/{percent}/snooze",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold", "unsnooze"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/threshold/{percent}/unsnooze",
        "auth": "JWT owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("account",): {
        "endpoint": "composite router for account subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("account", "alert-email"): {
        "endpoint": "composite router for account alert-email subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("account", "alert-email", "show"): {
        "endpoint": "GET /api/v1/account/alert-email",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.account.alert-email.v1",
    },
    ("account", "alert-email", "set"): {
        "endpoint": "PUT /api/v1/account/alert-email",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.account.alert-email.v1",
    },
    ("account", "alert-email", "clear"): {
        "endpoint": "DELETE /api/v1/account/alert-email",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.account.alert-email.v1",
    },
    ("channels",): {
        "endpoint": "composite router for channels subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("channels", "list"): {
        "endpoint": "GET /api/v1/account/notification-channels",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.list.v1",
    },
    ("channels", "show"): {
        "endpoint": "GET /api/v1/account/notification-channels/{channel_id}",
        "auth": "JWT",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "create"): {
        "endpoint": "POST /api/v1/account/notification-channels",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "update"): {
        "endpoint": "GET then PUT /api/v1/account/notification-channels/{channel_id}",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "test"): {
        "endpoint": "POST /api/v1/account/notification-channels/{channel_id}/test",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.test.v1",
    },
    ("channels", "delete"): {
        "endpoint": "DELETE /api/v1/account/notification-channels/{channel_id}",
        "auth": "JWT; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.delete.v1",
    },
    ("notifications",): {
        "endpoint": "composite router for notifications subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("notifications", "show"): {
        "endpoint": "GET /api/v1/projects/{project}/notifications",
        "auth": "any",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.notifications.v1",
    },
    ("notifications", "set"): {
        "endpoint": "PUT /api/v1/projects/{project}/notifications",
        "auth": "any; full-scope key for writes",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.notifications.v1",
    },
    ("notifications", "test"): {
        "endpoint": "POST /api/v1/projects/{project}/notifications/test-{channel}",
        "auth": "any; email requires JWT",
        "exits": "0 success; 1 generic/API or delivery failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.notifications.test.v1",
    },
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
        "auth": "local (none)",
        "exits": "0 success; 2 usage; 3 credential storage",
        "schema": "solwyn.logout.v1",
    },
    ("mcp",): {
        "endpoint": "local stdio MCP server",
        "auth": "resolved per tool call",
        "exits": "0 clean shutdown; 1 startup or protocol failure; 2 usage",
        "schema": "MCP protocol (stdio JSON-RPC)",
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

_INIT_REMOTE_OPERATIONS = (
    "conditional POST /api/v1/auth/login",
    "GET /api/v1/projects",
    "optional POST /api/v1/projects",
    "final GET /api/v1/projects/{project}/budget",
    "--run-cap adds a rule with GET/GET/PUT /api/v1/projects/{project}/budget after creation",
)
_INIT_LOCAL_OPERATIONS = (
    "write local configuration",
    "scan Python imports",
    "render SDK snippets",
    "optionally write agent artifacts with --agents",
)
_INIT_TRUST_STATEMENT = "Provider credentials are never collected"


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


def _stages_between(rendered: str, start: str, end: str) -> tuple[str, ...]:
    _, start_found, remainder = rendered.partition(start)
    stages, end_found, _ = remainder.partition(end)
    assert start_found and end_found
    return tuple(stages.split("; "))


def _assert_init_help_contract(rendered: str) -> None:
    assert (
        _stages_between(
            rendered,
            "Remote operations: ",
            ". Local operations: ",
        )
        == _INIT_REMOTE_OPERATIONS
    )
    assert (
        _stages_between(
            rendered,
            "Local operations: ",
            f". {_INIT_TRUST_STATEMENT}",
        )
        == _INIT_LOCAL_OPERATIONS
    )
    assert f"{_INIT_TRUST_STATEMENT};" in rendered


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


def test_mcp_help_names_the_exact_six_tool_surface() -> None:
    rendered = _normalized_help(("mcp",))

    assert (
        "Tools: budget_status, budget_check, budget_confirm, get_project_status, "
        "get_costs, list_projects, get_cost_total, list_budget_rules."
    ) in rendered


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

    _assert_init_help_contract(init_help)


@pytest.mark.parametrize(
    "stage",
    (*_INIT_REMOTE_OPERATIONS, *_INIT_LOCAL_OPERATIONS, _INIT_TRUST_STATEMENT),
)
def test_init_help_contract_rejects_a_missing_or_changed_stage(stage: str) -> None:
    init_help = _normalized_help(("init",))
    mutated_help = init_help.replace(stage, "changed stage", 1)

    assert mutated_help != init_help
    with pytest.raises(AssertionError):
        _assert_init_help_contract(mutated_help)
