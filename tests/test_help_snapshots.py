"""Semantic snapshots for every registered CLI help surface."""

import re
from collections.abc import Iterator

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from solwyn_cli.main import app

runner = CliRunner()


HELP_CONTRACTS = {
    ("plan", "list"): {
        "endpoint": "GET /api/v1/billing/catalog",
        "auth": "login or API key with user context",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.plan.catalog.v1",
    },
    ("account", "phone"): {
        "endpoint": "GET /api/v1/account/phone",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.account.phone.v1",
    },
    ("account", "phone", "show"): {
        "endpoint": "GET /api/v1/account/phone",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.account.phone.v1",
    },
    ("budget", "uncounted"): {
        "endpoint": "GET /api/v1/projects/{project_id}/budget/uncounted",
        "auth": "key or login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.uncounted.v1",
    },
    ("alerts", "activity"): {
        "endpoint": "GET /api/v1/account/alerts/activity",
        "auth": "login; any account member",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.alerts.activity.v1",
    },
    ("alerts", "pending"): {
        "endpoint": "GET /api/v1/account/alerts/open-deliveries",
        "auth": "login; any account member",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.alerts.pending.v1",
    },
    ("account", "recover"): {
        "endpoint": "none (moved to the dashboard)",
        "auth": "none",
        "exits": "2 recovery happens in the dashboard",
        "schema": "solwyn.error.v1",
    },
    ("attest",): {
        "endpoint": "GET /api/v1/projects/{project}/spend-control-attestation or GET /api/v1/account/spend-control-attestation",
        "auth": "project: key or login; account: login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "stream (SpendControlAttestation JSON or section,field,value CSV)",
    },
    ("costs", "baseline"): {
        "endpoint": "GET /api/v1/projects/{project}/spend-baseline",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.baseline.v1",
    },
    ("costs", "unit-economics"): {
        "endpoint": "GET /api/v1/projects/{project}/unit-economics",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.unit-economics.v1",
    },
    ("costs", "drift"): {
        "endpoint": "GET /api/v1/projects/{project}/costs/drift",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.drift.v1",
    },
    ("costs", "priced-models"): {
        "endpoint": "GET /api/v1/projects/{project}/costs/priced-models",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.priced-models.v1",
    },
    ("costs", "simulate"): {
        "endpoint": "POST /api/v1/projects/{project}/costs/simulate",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.simulation.v1",
    },
    ("alerts",): {
        "endpoint": "GET /api/v1/projects/{project_id}/alert-events or /api/v1/account/alert-events",
        "auth": "key or login; account requires login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.v1",
    },
    ("alerts", "list"): {
        "endpoint": "GET /api/v1/projects/{project_id}/alert-events or /api/v1/account/alert-events",
        "auth": "key or login; account requires login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.v1",
    },
    ("alerts", "show"): {
        "endpoint": "GET /api/v1/projects/{project_id}/alert-events or /api/v1/account/alert-events (cursor walk; local event ID match)",
        "auth": "key or login; account requires login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.v1",
    },
    ("alerts", "export"): {
        "endpoint": "GET /api/v1/projects/{project_id}/alert-events/export or /api/v1/account/alert-events/export",
        "auth": "key or login; account requires login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "stream (CSV or NDJSON)",
    },
    ("alerts", "account"): {
        "endpoint": "/api/v1/account/alerts",
        "auth": "login; mutations require owner",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "show"): {
        "endpoint": "GET /api/v1/account/alerts",
        "auth": "login; any account member",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "set"): {
        "endpoint": "PUT /api/v1/account/alerts",
        "auth": "login owner",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "delete"): {
        "endpoint": "DELETE /api/v1/account/alerts",
        "auth": "login owner",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "snooze"): {
        "endpoint": "POST /api/v1/account/alerts/snooze",
        "auth": "login owner",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "unsnooze"): {
        "endpoint": "POST /api/v1/account/alerts/unsnooze",
        "auth": "login owner",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
    ("alerts", "account", "preview"): {
        "endpoint": "POST /api/v1/account/alerts/preview",
        "auth": "login; any account member",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.alerts.account.v1",
    },
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
        "auth": "key or login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rules", "list"): {
        "endpoint": "GET /api/v1/projects/{project_id}/budget",
        "auth": "key or login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rule"): {
        "endpoint": "composite budget rule router",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold"): {
        "endpoint": "composite budget threshold router",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "add"): {
        "endpoint": "GET then PUT /api/v1/projects/{project_id}/budget",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "restore-defaults"): {
        "endpoint": "POST "
        "/api/v1/projects/{project_id}/budget/restore-defaults; "
        "GET "
        "/api/v1/projects/{project_id}/budget",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rules.v1",
    },
    ("budget", "rule", "preview"): {
        "endpoint": "POST /api/v1/projects/{project_id}/alerts/preview",
        "auth": "key or login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_preview.v1",
    },
    ("budget", "rule", "remove"): {
        "endpoint": "DELETE /api/v1/projects/{project_id}/budget/rule/{rule_key}",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "pause"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/pause",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "resume"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/resume",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "snooze"): {
        "endpoint": "POST /api/v1/projects/{project_id}/budget/rule/{rule_key}/snooze",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "rule", "unsnooze"): {
        "endpoint": "POST "
        "/api/v1/projects/{project_id}/budget/rule/{rule_key}/unsnooze",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold", "snooze"): {
        "endpoint": "POST "
        "/api/v1/projects/{project_id}/budget/threshold/{percent}/snooze",
        "auth": "login owner/admin",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget.rule_action.v1",
    },
    ("budget", "threshold", "unsnooze"): {
        "endpoint": "POST "
        "/api/v1/projects/{project_id}/budget/threshold/{percent}/unsnooze",
        "auth": "login owner/admin",
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
        "auth": "login",
        "exits": "0 success; 1 generic/API or delivery "
        "failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.account.alert-email.v1",
    },
    ("account", "alert-email", "set"): {
        "endpoint": "PUT /api/v1/account/alert-email",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery "
        "failure; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.account.alert-email.v1",
    },
    ("account", "alert-email", "clear"): {
        "endpoint": "DELETE /api/v1/account/alert-email",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery "
        "failure; 2 usage; 3 auth; 5 not found",
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
        "auth": "login",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.list.v1",
    },
    ("channels", "show"): {
        "endpoint": "GET /api/v1/account/notification-channels/{channel_id}",
        "auth": "login",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "create"): {
        "endpoint": "POST /api/v1/account/notification-channels",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "update"): {
        "endpoint": "GET then PUT /api/v1/account/notification-channels/{channel_id}",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.v1",
    },
    ("channels", "test"): {
        "endpoint": "POST /api/v1/account/notification-channels/{channel_id}/test",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.channels.test.v1",
    },
    ("channels", "delete"): {
        "endpoint": "DELETE /api/v1/account/notification-channels/{channel_id}",
        "auth": "login; owner for mutations",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage or confirmation; 3 auth; 5 not found",
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
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.notifications.v1",
    },
    ("notifications", "set"): {
        "endpoint": "PUT /api/v1/projects/{project}/notifications",
        "auth": "owner/admin login (project.write); API keys refused",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
        "schema": "solwyn.notifications.v1",
    },
    ("notifications", "test"): {
        "endpoint": "POST /api/v1/projects/{project}/notifications/test-{channel}",
        "auth": "any; Slack/PagerDuty require a login or full-scope key; email requires a login",
        "exits": "0 success; 1 generic/API or delivery failure; 2 "
        "usage; 3 auth; 5 not found",
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
    ("audit",): {
        "endpoint": "GET /api/v1/account/audit-events",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.audit.v1",
    },
    ("budget",): {
        "endpoint": "composite router for budget subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("budget", "check"): {
        "endpoint": "POST /api/v1/budgets/check; POST /api/v1/budgets/check/batch (--batch)",
        "auth": "key",
        "exits": "0 allowed or fail-open; 1 generic/API or item error; 2 usage; 3 auth; 4 budget denial; 5 not found",
        "schema": "solwyn.budget-check.v1; solwyn.budget.check.batch.v1 (--batch)",
    },
    ("budget", "confirm"): {
        "endpoint": "POST /api/v1/budgets/confirm",
        "auth": "key",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 reservation not found",
        "schema": "solwyn.budget-confirm.v1",
    },
    ("budget", "set"): {
        "endpoint": "GET then PUT /api/v1/projects/{project}/budget",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.budget-set.v1",
    },
    ("budget", "status"): {
        "endpoint": "GET /api/v1/projects/{project}/budget",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 4 "
        "utilization gate; 5 not found",
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
        "endpoint": (
            "GET /api/v1/projects/{project}/costs or, with --account, "
            "GET /api/v1/account/costs"
        ),
        "auth": "API key or login; --account requires login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.costs.v1 or solwyn.account_costs.v1",
    },
    ("forecast",): {
        "endpoint": "GET /api/v1/projects/{project}/forecast",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.forecast.v1",
    },
    ("doctor",): {
        "endpoint": "GET /health/ready plus one credential-specific authenticated probe",
        "auth": "optional; a login or API key enables the authenticated probe",
        "exits": "0 all checks pass; 1 diagnostic failure; 2 usage",
        "schema": "solwyn.doctor.v1; with --mcp: solwyn.doctor.mcp.v1",
    },
    ("init",): {
        "endpoint": "composite onboarding workflow",
        "auth": "a login (a browser login when none is stored); an API key only for an existing project",
        "exits": "0 success; 1 generic/API; 2 usage or project mismatch; 3 auth; 5 not found; 6 approval or login pending",
        "schema": "solwyn.init.v2",
    },
    ("keys",): {
        "endpoint": "composite router for API key subcommands",
        "auth": "login, except keys env (local)",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("keys", "list"): {
        "endpoint": "GET /api/v1/keys",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 3 auth",
        "schema": "solwyn.keys-list.v1",
    },
    ("keys", "create"): {
        "endpoint": "POST /api/v1/keys",
        "auth": "login (browser approval)",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth or storage; 6 approval",
        "schema": "solwyn.keys-create.v1",
    },
    ("keys", "revoke"): {
        "endpoint": "DELETE /api/v1/keys/{key_id}",
        "auth": "login (browser approval)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        "schema": "solwyn.keys-revoke.v1 (CLI output after bodyless 204)",
    },
    ("keys", "rotate"): {
        "endpoint": "POST /api/v1/keys/{key_id}/rotate",
        "auth": "login (browser approval)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        "schema": "solwyn.keys-rotate.v1",
    },
    ("keys", "add-project"): {
        "endpoint": "none: only the dashboard adds a project to a key",
        "auth": "none; no credential lookup",
        "exits": "2 always",
        "schema": "solwyn.error.v1",
    },
    ("keys", "remove-project"): {
        "endpoint": "DELETE /api/v1/keys/{key_id}/projects/{project_id}",
        "auth": "login (browser approval)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found; 6 approval",
        "schema": "solwyn.keys-remove-project.v1",
    },
    ("login",): {
        "endpoint": (
            "POST /oauth/device_authorization and POST /oauth/token, or with "
            "--with-key GET /api/v1/auth/key and a local credential store write"
        ),
        "auth": "none",
        "exits": (
            "0 success; 1 generic/API; 2 usage; 3 auth, denied, expired or "
            "credential storage; 6 still waiting for browser approval"
        ),
        "schema": "solwyn.login.v2; with --no-store --json: solwyn.login.token.v1",
    },
    ("logout",): {
        "endpoint": (
            "POST /oauth/revoke (best effort) and a local credential store clear"
        ),
        "auth": "local (none)",
        "exits": "0 success; 2 usage; 3 credential storage",
        "schema": "solwyn.logout.v1",
    },
    ("credentials",): {
        "endpoint": "composite router for CLI login subcommands",
        "auth": "login",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("credentials", "list"): {
        "endpoint": "GET /api/v1/cli/credentials",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.credentials.list.v1",
    },
    ("credentials", "revoke"): {
        "endpoint": "GET /api/v1/cli/credentials; DELETE /api/v1/cli/credentials/{id}",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.credentials.revoke.v1",
    },
    ("approvals",): {
        "endpoint": "composite router for approval subcommands",
        "auth": "login",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("approvals", "status"): {
        "endpoint": "GET /api/v1/cli/approvals/{approval_id}",
        "auth": "login (only your own approvals)",
        "exits": (
            "0 shown; 1 generic/API; 2 usage; 3 auth; 5 not found; "
            "6 denied, expired, or still pending after --wait"
        ),
        "schema": "solwyn.approval.v1",
    },
    ("keys", "env"): {
        "endpoint": (
            "local credential store read; with a login, GET /api/v1/projects "
            "(and GET /api/v1/accounts/me for SOLWYN_TOKEN)"
        ),
        "auth": "local (the stored key; SOLWYN_API_KEY is never read)",
        "exits": "0 success; 2 usage or project mismatch; 3 no stored key or account unresolved",
        "schema": "solwyn.keys-env.v2",
    },
    ("mcp-config",): {
        "endpoint": "none (offline)",
        "auth": "none; no credential lookup",
        "exits": "0 descriptor emitted; 1 executable unavailable; 2 usage",
        "schema": "solwyn.mcp-config.v1",
    },
    ("mcp",): {
        "endpoint": "local stdio MCP server",
        "auth": "resolved per tool call",
        "exits": "0 clean shutdown; 1 startup or protocol failure; 2 usage",
        "schema": "MCP protocol (stdio JSON-RPC)",
    },
    ("plan",): {
        "endpoint": "GET /api/v1/billing/status",
        "auth": "login or API key with account context",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.plan.v1",
    },
    ("projects",): {
        "endpoint": "composite router for project subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("projects", "create"): {
        "endpoint": "POST /api/v1/projects",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.projects-create.v1",
    },
    ("projects", "delete"): {
        "endpoint": "DELETE /api/v1/projects/{id}",
        "auth": "login (owner or admin)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth or stored-key cleanup; 5 not found",
        "schema": "solwyn.projects-delete.v1 (CLI output after bodyless 204)",
    },
    ("projects", "list"): {
        "endpoint": "GET /api/v1/projects",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.projects-list.v1",
    },
    ("projects", "show"): {
        "endpoint": "GET /api/v1/projects/{project}",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.projects-show.v1",
    },
    ("providers",): {
        "endpoint": "composite router for provider visibility subcommands",
        "auth": "any",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("providers", "failovers"): {
        "endpoint": "GET /api/v1/projects/{project}/providers/failovers",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.providers.failovers.v1",
    },
    ("providers", "untracked"): {
        "endpoint": "GET /api/v1/projects/{project}/untracked-surfaces",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.providers.untracked.v1",
    },
    ("runs",): {
        "endpoint": "composite router for agent-run subcommands",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("runs", "list"): {
        "endpoint": "GET /api/v1/projects/{project}/agent-runs",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.runs.v1",
    },
    ("runs", "show"): {
        "endpoint": "GET /api/v1/projects/{project}/agent-runs then GET /api/v1/projects/{project}/agent-runs/{run_id}",
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.runs.v1",
    },
    ("runs", "stop"): {
        "endpoint": "optional GET /api/v1/projects/{project}/agent-runs then POST /api/v1/projects/{project}/agent-runs/{run_id}/stop",
        "auth": "login + budget.write",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found",
        "schema": "solwyn.runs.stop.v1",
    },
    ("projects", "rename"): {
        "endpoint": "PUT /api/v1/projects/{id}",
        "auth": "login (owner or admin)",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.projects-rename.v1",
    },
    ("status",): {
        "endpoint": (
            "GET /api/v1/projects/{project}/budget and GET "
            "/api/v1/projects/{project}/providers; optionally GET forecast "
            "and provider failovers; login also GET /api/v1/overview"
        ),
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.status.v1",
    },
    ("overview",): {
        "endpoint": "GET /api/v1/overview",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 3 auth",
        "schema": "solwyn.overview.v1",
    },
    ("whoami",): {
        "endpoint": (
            "local key identity or GET /api/v1/account/me, "
            "GET /api/v1/accounts/me, and GET /api/v1/projects"
        ),
        "auth": "any",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth",
        "schema": "solwyn.whoami.v1",
    },
    ("webhooks",): {
        "endpoint": "composite router for webhook target subcommands",
        "auth": "login",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("webhooks", "create"): {
        "endpoint": "POST /api/v1/projects/{project}/webhooks",
        "auth": "owner/admin login (project.write)",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.webhooks-create.v1",
    },
    ("webhooks", "delete"): {
        "endpoint": "DELETE /api/v1/projects/{project}/webhooks/{webhook_id}",
        "auth": "owner/admin login (project.write)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found",
        "schema": "solwyn.webhooks-delete.v1 (CLI output after bodyless 204)",
    },
    ("webhooks", "list"): {
        "endpoint": "GET /api/v1/projects/{project}/webhooks",
        "auth": "login",
        "exits": "0 success; 1 generic/API; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.webhooks-list.v1",
    },
    ("webhooks", "rotate"): {
        "endpoint": "POST /api/v1/projects/{project}/webhooks/{webhook_id}/rotate",
        "auth": "owner/admin login (project.write)",
        "exits": "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found",
        "schema": "solwyn.webhooks-rotate.v1",
    },
    ("record",): {
        "endpoint": "POST /api/v1/metadata/ingest",
        "auth": "key",
        "exits": "0 recorded or call-ID replay; 1 rejection/collision/API; 2 "
        "usage; 3 auth",
        "schema": "solwyn.record.v1",
    },
    ("tags",): {
        "endpoint": "composite tag router",
        "auth": "varies by subcommand",
        "exits": "0 success; 2 usage",
        "schema": "varies by subcommand",
    },
    ("tags", "list"): {
        "endpoint": "GET /api/v1/projects/{project}/tags",
        "auth": "any",
        "exits": "0 success; 1 API/upgrade; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.tags.v1",
    },
    ("tags", "values"): {
        "endpoint": "GET /api/v1/projects/{project}/tags/values",
        "auth": "any",
        "exits": "0 success; 1 API/upgrade; 2 usage; 3 auth; 5 not found",
        "schema": "solwyn.tags.values.v1",
    },
    ("tags", "retire"): {
        "endpoint": "DELETE /api/v1/account/tags",
        "auth": "login admin/owner",
        "exits": "0 retired; 1 API; 2 usage or confirmation; 3 auth; 5 never registered",
        "schema": "solwyn.tags.v1",
    },
}

_BILLING_EXITS = (
    "0 success; 1 generic/API; 2 usage or confirmation; 3 auth; 5 not found"
)
HELP_CONTRACTS.update(
    {
        ("plan", "cancel"): {
            "endpoint": "GET /api/v1/billing/status then POST /api/v1/billing/cancel",
            "auth": "owner login (billing.manage)",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.cancel.v1",
        },
        ("plan", "resume"): {
            "endpoint": "GET /api/v1/billing/status then DELETE /api/v1/billing/cancel",
            "auth": "owner login (billing.manage)",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.resume.v1",
        },
        ("plan", "change"): {
            "endpoint": "POST /api/v1/billing/plan-preview then POST /api/v1/billing/plan-change; --discard uses GET status then DELETE /api/v1/billing/plan-change",
            "auth": "login for preview; owner login (billing.manage) for writes",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.change.v1; solwyn.plan.discard.v1",
        },
        ("plan", "upgrade"): {
            "endpoint": "POST /api/v1/billing/checkout",
            "auth": "owner login (billing.manage)",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.upgrade.v1",
        },
        ("plan", "portal"): {
            "endpoint": "POST /api/v1/billing/portal",
            "auth": "owner login (billing.manage)",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.portal.v1",
        },
        ("plan", "pending"): {
            "endpoint": "composite router for pending plan intent",
            "auth": "owner login (billing.manage)",
            "exits": "0 success; 2 usage",
            "schema": "varies by subcommand",
        },
        ("plan", "pending", "dismiss"): {
            "endpoint": "GET /api/v1/billing/status then DELETE /api/v1/billing/plan-intent",
            "auth": "owner login (billing.manage)",
            "exits": _BILLING_EXITS,
            "schema": "solwyn.plan.pending.dismiss.v1",
        },
    }
)


_INIT_REMOTE_OPERATIONS = (
    "conditional browser login (POST /oauth/device_authorization, POST /oauth/token)",
    "GET /api/v1/projects",
    "optional POST /api/v1/projects",
    "final GET /api/v1/projects/{project}/budget",
    "--run-cap adds a rule with GET/GET/PUT /api/v1/projects/{project}/budget after creation",
)
_INIT_LOCAL_OPERATIONS = (
    "write .solwyn.env when missing",
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


_TEAM_EXITS = (
    "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found"
)
HELP_CONTRACTS.update(
    {
        ("team",): {
            "endpoint": "/api/v1/accounts/me",
            "auth": "login; account role enforced by Core",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "varies by command",
        },
        ("team", "members"): {
            "endpoint": "/api/v1/accounts/me/members",
            "auth": "login; account role enforced by Core",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.members.v1",
        },
        ("team", "invites"): {
            "endpoint": "/api/v1/accounts/me/invitations",
            "auth": "login; account role enforced by Core",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.invites.v1",
        },
        ("team", "members", "list"): {
            "endpoint": "GET /api/v1/accounts/me/members",
            "auth": "login; any account member",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.members.v1",
        },
        ("team", "members", "set-role"): {
            "endpoint": "PATCH /api/v1/accounts/me/members/{user_id}",
            "auth": "owner login",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.member.v1",
        },
        ("team", "members", "remove"): {
            "endpoint": "DELETE /api/v1/accounts/me/members/{user_id}",
            "auth": "login; owner/admin for allowed targets, or self",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.remove.v1",
        },
        ("team", "leave"): {
            "endpoint": "DELETE /api/v1/accounts/me/members/{user_id}",
            "auth": "login; your own membership",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.remove.v1",
        },
        ("team", "invites", "list"): {
            "endpoint": "GET /api/v1/accounts/me/invitations",
            "auth": "owner/admin login",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.invites.v1",
        },
        ("team", "invites", "send"): {
            "endpoint": "POST /api/v1/accounts/me/invitations",
            "auth": "owner/admin login",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.invitation.v1",
        },
        ("team", "invites", "resend"): {
            "endpoint": "POST /api/v1/accounts/me/invitations/{invitation_id}/resend",
            "auth": "owner/admin login",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.invitation-action.v1",
        },
        ("team", "invites", "revoke"): {
            "endpoint": "DELETE /api/v1/accounts/me/invitations/{invitation_id}",
            "auth": "owner/admin login",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.team.invitation-action.v1",
        },
        ("invite",): {
            "endpoint": "/api/v1/invitations/{token}",
            "auth": "public preview",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "varies by command",
        },
        ("invite", "show"): {
            "endpoint": "GET /api/v1/invitations/{token}",
            "auth": "none; public preview never reads stored credentials",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.invite.show.v1",
        },
        ("invite", "accept"): {
            "endpoint": "none (retired)",
            "auth": "none",
            "exits": "0 success; 1 generic/API or conflict; 2 usage or confirmation; 3 auth; 5 not found",
            "schema": "solwyn.error.v1",
        },
        ("account", "rename"): {
            "endpoint": "PATCH /api/v1/accounts/me",
            "auth": "owner login",
            "exits": _TEAM_EXITS,
            "schema": "solwyn.account.rename.v1",
        },
    }
)


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


def test_audit_help_explains_exact_actions_and_rule_event_gap() -> None:
    rendered = _normalized_help(("audit",))

    assert "Actions are exact-match strings" in rendered
    for action in (
        "account.created",
        "account.updated",
        "team.member_removed",
        "team.member_role_changed",
        "team.invitation_created",
        "team.invitation_revoked",
        "project.created",
        "project.updated",
        "project.archived",
        "budget.defaults_restored",
        "budget.updated",
        "api_key.rotated",
        "api_key.deleted",
        "agent_run.stopped",
        "billing.plan_intent_deleted",
        "billing.plan_changed",
        "billing.cancel_scheduled",
        "billing.cancel_undone",
        "billing.plan_change_discarded",
        "notification_channel.created",
        "notification_channel.updated",
        "notification_channel.deleted",
        "account_phone.verified",
        "account_phone.deleted",
        "consent.sms_alerts_changed",
        "upserted",
    ):
        assert action in rendered
    assert "only budget.updated is in this trail" in rendered
    assert "alerts list --rule-type" in rendered


def test_project_delete_help_states_irreversibility_and_key_revocation() -> None:
    rendered = _normalized_help(("projects", "delete"))

    assert "Irreversible" in rendered
    assert "keys stop reaching it in the same transaction" in rendered
    assert "stored key for the project" in rendered
    assert "immediately becomes unavailable" in rendered


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


def test_mcp_help_names_the_exact_tool_surface() -> None:
    rendered = _normalized_help(("mcp",))

    assert (
        "Tools: budget_status, budget_check, budget_confirm, get_project_status, get_forecast, get_costs, list_projects, get_cost_total, list_budget_rules, record_call, list_tags, list_tag_values, list_agent_runs, get_agent_run, list_alert_events, budget_check_batch, get_overview, list_cost_events, get_cost_baseline, get_unit_economics, get_cost_drift, list_priced_models, simulate_costs, list_provider_failovers, list_untracked_surfaces, list_audit_events, plan_status, plan_catalog, get_whoami, get_account_phone, list_alert_activity, list_open_alert_deliveries, budget_uncounted, get_account_alert, plan_preview, list_team_members, list_team_invites."
        in rendered
    )


def test_auth_and_key_help_is_secret_safe() -> None:
    login_help = _normalized_help(("login",))
    keys_help = " ".join(
        _normalized_help(path)
        for path in (("keys",), ("keys", "rotate"), ("keys", "revoke"))
    )

    assert "Keys are never accepted as command-line arguments or echoed" in login_help
    assert "The CLI never opens a browser" in login_help
    assert "--wait 0 starts and returns" in login_help
    assert "listing never reveals key material" in keys_help
    assert "otherwise the replacement is shown once" in keys_help


def test_webhooks_help_pins_secret_and_delivery_contract() -> None:
    rendered = " ".join(
        _normalized_help(path)
        for path in (
            ("webhooks",),
            ("webhooks", "list"),
            ("webhooks", "create"),
            ("webhooks", "rotate"),
        )
    )

    assert "signing secret is shown exactly once" in rendered
    assert "If the signing secret is lost, rotate the webhook" in rendered
    assert "X-Solwyn-Signature: sha256=<hex HMAC-SHA256 of the raw body>" in rendered
    assert "Idempotency-Key" in rendered
    assert "No timestamp header or replay window is defined" in rendered


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


# Writes a Solwyn login can make without a browser approval, or that never take
# a login (spec section 8: metering, read-only POSTs, OAuth and SELF routes).
_NO_APPROVAL_WRITES = re.compile(
    r"^/(oauth/"
    r"|api/v1/(budgets/|metadata/ingest|untracked-surfaces|cli/credentials"
    r"|invitations/|billing/plan-preview$|account/alerts/(latest-events|preview)$"
    r"|projects/\{[^}]+\}/(alerts/preview|costs/simulate"
    r"|providers/breaker-reports|untracked-surfaces)$))"
)
_WRITE = re.compile(r"\b(POST|PUT|PATCH|DELETE) (/[^\s;,)]+)")


def test_every_approval_capable_command_lists_exit_six() -> None:
    # Review item 10: a write that can need a browser approval can exit 6.
    missing = []
    for path, command in _commands(get_command(app)):
        epilog = command.epilog or ""
        endpoint = next(
            (line for line in epilog.splitlines() if line.startswith("Endpoint:")), ""
        )
        exits = next(
            (line for line in epilog.splitlines() if line.startswith("Exit codes:")),
            "",
        )
        writes = [
            route
            for _method, route in _WRITE.findall(endpoint)
            if not _NO_APPROVAL_WRITES.match(route)
        ]
        if writes and not re.search(r"\b6 ", exits):
            missing.append(" ".join(path))
    assert missing == []
