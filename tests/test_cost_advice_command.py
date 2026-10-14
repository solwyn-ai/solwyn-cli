"""Behavioral contract tests for cost analysis and pricing commands."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from golden.harness import assert_matches_golden
from solwyn_cli import auth
from solwyn_cli.commands import cost_analysis, cost_pricing
from solwyn_cli.main import app


runner = CliRunner()
GOLDEN_DIR = Path(__file__).parent / "golden"
VALID_KEY = "sk_proj_" + "c8" * 32
WINDOW = {
    "requested_from": "2026-08-29T00:00:00Z",
    "requested_to": "2026-09-05T00:00:00Z",
    "effective_from": "2026-08-29T00:00:00Z",
    "effective_to": "2026-09-05T00:00:00Z",
    "retention_clamped": False,
}
BASELINE = {
    "project_id": "proj_alpha",
    "lookback_days": 7,
    "days_with_data": 6,
    "mean_daily_spend": 1.25,
    "stdev_daily_spend": 0.375,
    "armed": True,
}
UNIT_ECONOMICS = {
    "range": "30d",
    "window_start": "2026-08-06T00:00:00Z",
    "window_end": "2026-09-05T00:00:00Z",
    "prev_window_start": None,
    "prev_window_end": None,
    "cost_per_request": {
        "value": 0.0125,
        "prev_value": 0.01,
        "delta_pct": 0.25,
        "context": "200 requests",
    },
    "cost_per_agent_run": None,
    "tokens_per_dollar": {
        "value": 50000.0,
        "prev_value": None,
        "delta_pct": None,
        "context": "2.0M tokens served",
    },
    "avg_prompt_tokens": {
        "value": 120.0,
        "prev_value": 100.0,
        "delta_pct": 0.2,
        "context": "200 requests",
    },
    "suspected_cause": {
        "summary": "Model mix shifted toward a higher-cost target.",
        "confidence": "high",
        "spans": [
            {
                "kind": "model",
                "label": "gpt-5 usage spike",
                "contribution_pct": 61.5,
            }
        ],
    },
}
DRIFT = {
    "project_id": "proj_alpha",
    "range": "7d",
    "window": WINDOW,
    "actual_total": 12.5,
    "estimate_total": 10.0,
    "difference": 2.5,
    "included_event_count": 80,
    "missing_estimate_count": 3,
    "groups": [
        {
            "provider": "openai",
            "model": "gpt-5",
            "actual_cost": 12.5,
            "estimated_cost": 10.0,
            "difference": 2.5,
            "event_count": 80,
        }
    ],
    "caveats": {
        "estimate_heuristic": "server_side_input_equals_output",
        "null_estimates_excluded": True,
        "unit_priced_and_unpriced_estimates_are_null": True,
        "denied_events_included": True,
    },
}
PRICED_MODELS = {
    "project_id": "proj_alpha",
    "pricing_version": "2026-08-06",
    "provenance": "first_party_vendored_dataset",
    "models": [
        {"provider": "openai", "model": "gpt-5", "modality": "text"},
        {
            "provider": "bedrock",
            "model": "amazon.nova-pro-v1:0",
            "modality": "text",
        },
    ],
}
SIMULATION = {
    "project_id": "proj_alpha",
    "range": "7d",
    "window": WINDOW,
    "target": {
        "provider": "bedrock",
        "model": "amazon.nova-pro-v1:0",
        "modality": "text",
        "region": "us-east-1",
        "service_tier": "standard",
    },
    "baseline_actual_cost": 12.5,
    "simulated_cost": 9.75,
    "delta_cost": -2.75,
    "scanned_event_count": 100,
    "baseline_event_count": 95,
    "included_event_count": 90,
    "excluded_event_count": 10,
    "excluded": {
        "unpriced": 3,
        "local": 2,
        "modality_mismatch": 4,
        "missing_breakdown": 1,
    },
    "caveats": {
        "pricing_version": "2026-08-06",
        "current_rates_snapshot": True,
        "baseline_uses_stored_actual_cost": True,
        "tokenizer_equivalence_approximation": True,
        "unpriced_and_local_excluded": True,
        "modality_mismatches_excluded": True,
    },
}


class FakeClient:
    instances: list["FakeClient"] = []
    responses: list[httpx.Response] = []

    def __init__(self, *, api_url: str, credential: object = None) -> None:
        self.api_url = api_url
        self.credential = credential
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, path, kwargs))
        return self.__class__.responses.pop(0)


@pytest.fixture(autouse=True)
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    FakeClient.instances = []
    FakeClient.responses = []
    credential = auth.ProjectKeyCredential(api_key=VALID_KEY, source="environment")
    for module in (cost_analysis, cost_pricing):
        monkeypatch.setattr(module, "SolwynClient", FakeClient, raising=False)
        monkeypatch.setattr(
            module, "resolve_credential", lambda: credential, raising=False
        )
    yield


@pytest.mark.parametrize(
    ("command", "payload", "golden_stem"),
    [
        (["baseline", "--days", "7"], BASELINE, "costs-baseline"),
        (
            ["unit-economics", "--range", "30d", "--cause"],
            UNIT_ECONOMICS,
            "costs-unit-economics",
        ),
        (["drift", "--range", "7d"], DRIFT, "costs-drift"),
        (["priced-models"], PRICED_MODELS, "costs-priced-models"),
        (
            [
                "simulate",
                "--provider",
                "bedrock",
                "--model",
                "amazon.nova-pro-v1:0",
                "--region",
                "us-east-1",
                "--service-tier",
                "standard",
            ],
            SIMULATION,
            "costs-simulation",
        ),
    ],
)
@pytest.mark.parametrize("output_format", ["table", "json"])
def test_cost_advice_commands_match_goldens(
    command: list[str],
    payload: dict[str, Any],
    golden_stem: str,
    output_format: str,
) -> None:
    FakeClient.responses = [httpx.Response(200, json=payload)]
    options = ["--json"] if output_format == "json" else ["--no-color"]

    result = runner.invoke(
        app, [*options, "--project", "proj_alpha", "costs", *command]
    )

    assert result.exit_code == 0, result.stderr
    suffix = "json" if output_format == "json" else "txt"
    assert_matches_golden(
        result.stdout, GOLDEN_DIR / f"{golden_stem}-{output_format}.{suffix}"
    )


def test_cost_advice_requests_use_exact_paths_params_and_body() -> None:
    FakeClient.responses = [
        httpx.Response(200, json=BASELINE),
        httpx.Response(200, json=UNIT_ECONOMICS),
        httpx.Response(200, json=DRIFT),
        httpx.Response(200, json=PRICED_MODELS),
        httpx.Response(200, json=SIMULATION),
    ]
    commands = [
        ["baseline", "--days", "7"],
        ["unit-economics", "--range", "30d", "--no-vs-prev", "--cause"],
        [
            "drift",
            "--from",
            "2026-09-01T00:00:00Z",
            "--to",
            "2026-09-02T00:00:00Z",
        ],
        ["priced-models"],
        [
            "simulate",
            "--provider",
            "bedrock",
            "--model",
            "amazon.nova-pro-v1:0",
            "--region",
            " us-east-1 ",
            "--service-tier",
            "standard",
            "--from",
            "2026-09-01T00:00:00Z",
            "--to",
            "2026-09-02T00:00:00Z",
        ],
    ]

    for command in commands:
        result = runner.invoke(
            app, ["--json", "--project", "proj_alpha", "costs", *command]
        )
        assert result.exit_code == 0, result.stderr

    assert [
        request for client in FakeClient.instances for request in client.requests
    ] == [
        (
            "GET",
            "/api/v1/projects/proj_alpha/spend-baseline",
            {"params": {"days": 7}},
        ),
        (
            "GET",
            "/api/v1/projects/proj_alpha/unit-economics",
            {"params": {"range": "30d", "vs_prev": False, "cause": True}},
        ),
        (
            "GET",
            "/api/v1/projects/proj_alpha/costs/drift",
            {
                "params": {
                    "from": "2026-09-01T00:00:00Z",
                    "to": "2026-09-02T00:00:00Z",
                }
            },
        ),
        ("GET", "/api/v1/projects/proj_alpha/costs/priced-models", {}),
        (
            "POST",
            "/api/v1/projects/proj_alpha/costs/simulate",
            {
                "params": {
                    "from": "2026-09-01T00:00:00Z",
                    "to": "2026-09-02T00:00:00Z",
                },
                "retry_rate_limit": False,
                "retry_auth": False,
                "json": {
                    "target_provider": "bedrock",
                    "target_model": "amazon.nova-pro-v1:0",
                    "target_region": "us-east-1",
                    "target_service_tier": "standard",
                },
            },
        ),
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            ["--provider", "bedrock", "--model", "amazon.nova-pro-v1:0"],
            "Bedrock simulation requires explicit target_region and target_service_tier",
        ),
        (
            [
                "--provider",
                "openai",
                "--model",
                "gpt-5",
                "--region",
                "us-east-1",
            ],
            "target_region and target_service_tier are only supported for Bedrock",
        ),
    ],
)
def test_simulate_rejects_invalid_provider_context_before_http(
    options: list[str], message: str
) -> None:
    result = runner.invoke(
        app,
        ["--json", "--project", "proj_alpha", "costs", "simulate", *options],
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []
    assert message in result.stderr


def test_unit_economics_omits_null_optional_rows_without_rendering_zero() -> None:
    payload = dict(UNIT_ECONOMICS)
    payload["suspected_cause"] = None
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(
        app,
        ["--no-color", "--project", "proj_alpha", "costs", "unit-economics"],
    )

    assert result.exit_code == 0, result.stderr
    assert "Agent run" not in result.stdout
    assert "Suspected cause" not in result.stdout + result.stderr
    assert "Tokens / dollar" in result.stdout
    assert "—" in result.stdout


def test_simulation_table_explains_unequal_baseline_and_repricing_populations() -> None:
    payload = {
        **SIMULATION,
        "baseline_actual_cost": 100.0,
        "simulated_cost": 1.0,
        "delta_cost": -99.0,
        "scanned_event_count": 2,
        "baseline_event_count": 2,
        "included_event_count": 1,
        "excluded_event_count": 1,
        "excluded": {
            "unpriced": 0,
            "local": 0,
            "modality_mismatch": 1,
            "missing_breakdown": 0,
        },
    }
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(
        app,
        [
            "--no-color",
            "--project",
            "proj_alpha",
            "costs",
            "simulate",
            "--provider",
            "bedrock",
            "--model",
            "amazon.nova-pro-v1:0",
            "--region",
            "us-east-1",
            "--service-tier",
            "standard",
        ],
    )

    assert result.exit_code == 0, result.stderr
    table_rows = [line.split() for line in result.stdout.splitlines()]
    for row in (
        ["Scanned", "events", "2"],
        ["Baseline", "events", "2"],
        ["Repriced", "events", "1"],
        ["Excluded", "events", "1"],
    ):
        assert row in table_rows
    assert "Delta (repriced - baseline)" in result.stdout
    assert (
        "baseline uses stored actual cost for 2 priced served events; "
        "current-rate repricing includes 1 compatible event"
    ) in result.stderr
    assert (
        "1 modality-mismatch/missing-breakdown event remains in the baseline "
        "but is excluded from repricing"
    ) in result.stderr
    assert "0 unpriced/local events are excluded from both costs" in result.stderr
    assert (
        "2026-08-06 current-rate snapshot; tokenizer equivalence is approximate"
        in result.stderr
    )
    assert "Unequal event sets:" in result.stderr


def test_simulation_table_marks_equal_baseline_and_repricing_populations() -> None:
    payload = {
        **SIMULATION,
        "baseline_actual_cost": 100.0,
        "simulated_cost": 80.0,
        "delta_cost": -20.0,
        "scanned_event_count": 2,
        "baseline_event_count": 2,
        "included_event_count": 2,
        "excluded_event_count": 0,
        "excluded": {
            "unpriced": 0,
            "local": 0,
            "modality_mismatch": 0,
            "missing_breakdown": 0,
        },
    }
    FakeClient.responses = [httpx.Response(200, json=payload)]

    result = runner.invoke(
        app,
        [
            "--no-color",
            "--project",
            "proj_alpha",
            "costs",
            "simulate",
            "--provider",
            "bedrock",
            "--model",
            "amazon.nova-pro-v1:0",
            "--region",
            "us-east-1",
            "--service-tier",
            "standard",
        ],
    )

    assert result.exit_code == 0, result.stderr
    table_rows = [line.split() for line in result.stdout.splitlines()]
    assert ["Scanned", "events", "2"] in table_rows
    assert ["Baseline", "events", "2"] in table_rows
    assert ["Repriced", "events", "2"] in table_rows
    assert "Unequal event sets:" not in result.stderr


def test_drift_human_output_explains_estimate_heuristic_and_coverage() -> None:
    FakeClient.responses = [httpx.Response(200, json=DRIFT)]

    result = runner.invoke(
        app,
        ["--no-color", "--project", "proj_alpha", "costs", "drift"],
    )

    assert result.exit_code == 0, result.stderr
    assert "server-side input tokens equal output tokens" in result.stderr
    assert "80 estimates included; 3 events missing estimates" in result.stderr


@pytest.mark.parametrize("days", [2, 31])
def test_baseline_rejects_days_outside_core_bounds_without_http(days: int) -> None:
    result = runner.invoke(
        app,
        ["--project", "proj_alpha", "costs", "baseline", "--days", str(days)],
    )

    assert result.exit_code == 2
    assert FakeClient.instances == []
