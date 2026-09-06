"""Contract snapshot — freeze the machine-readable CLI surface contract.

Mirrors ``solwyn-python-sdk/tests/unit/test_contract_snapshot.py``: a
hand-reviewed committed snapshot pins the exact contract and ANY drift — an
added, removed, or renamed command, flag, default, exit code, or JSON output
key — fails CI loudly instead of silently breaking the claims the docs
pipeline verifies against.

The SDK freezes ``model_fields`` / enum values as hand-written EXPECTED sets and
asserts the live model equals them. This CLI has no single wire model, so the
frozen contract lives in ``tests/contract/cli_contract.json`` (regenerated with
``scripts/generate_contract.py``) and the comparison uses the repo's golden
unified-diff style. The live side is introspected, never transcribed:
``builder.build_cli_contract`` walks the real Typer/Click app, the frozen
``ExitCode`` enum, and the checked-in golden JSON fixtures.
"""

from __future__ import annotations

from difflib import unified_diff

from contract.builder import (
    ERROR_SCHEMA,
    EXIT_CODE_MEANINGS,
    SNAPSHOT_PATH,
    build_cli_contract,
    declared_schema_tokens,
    golden_schema_keys,
    render_contract,
)

from solwyn_cli.exit_codes import ExitCode


def test_cli_contract_snapshot_matches_the_live_cli() -> None:
    """The committed snapshot must equal the freshly introspected contract."""
    actual = render_contract()
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if actual == expected:
        return
    diff = "".join(
        unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(SNAPSHOT_PATH),
            tofile="live CLI contract",
        )
    )
    raise AssertionError(
        "CLI contract drift detected. If this change is intended, regenerate "
        "the snapshot with `python scripts/generate_contract.py` and review the "
        f"diff.\n{diff}"
    )


def test_exit_code_meaning_registry_covers_the_frozen_enum_exactly() -> None:
    """Every ExitCode member has a meaning, and no meaning is invented."""
    assert set(EXIT_CODE_MEANINGS) == set(ExitCode)
    assert all(meaning.strip() for meaning in EXIT_CODE_MEANINGS.values())


def test_contract_exit_codes_match_the_enum_values() -> None:
    contract = build_cli_contract()
    assert {entry["code"]: entry["name"] for entry in contract["exit_codes"]} == {
        member.value: member.name for member in ExitCode
    }


def test_every_declared_json_schema_is_exercised_by_a_golden_fixture() -> None:
    """A command claiming it emits JSON must have a captured golden output."""
    golden_schemas = set(golden_schema_keys())
    missing = declared_schema_tokens() - golden_schemas
    assert not missing, f"declared JSON schemas without a golden fixture: {missing}"


def test_contract_json_schemas_cover_declared_schemas_plus_error_envelope() -> None:
    contract = build_cli_contract()
    contract_schemas = {entry["schema"] for entry in contract["json_schemas"]}
    assert contract_schemas == declared_schema_tokens() | {ERROR_SCHEMA}
    assert all("schema" in entry["keys"] for entry in contract["json_schemas"])


def test_error_envelope_is_marked_and_shaped() -> None:
    contract = build_cli_contract()
    (error_entry,) = [
        entry for entry in contract["json_schemas"] if entry["schema"] == ERROR_SCHEMA
    ]
    assert error_entry["universal_error_envelope"] is True
    assert error_entry["keys"] == ["code", "detail", "hint", "message", "schema"]


def test_contract_omits_proprietary_api_internals() -> None:
    """Endpoint URLs and HTTP internals must never leak into the contract."""
    rendered = render_contract()
    for forbidden in ("/api/", "http://", "https://"):
        assert forbidden not in rendered


def test_cost_export_declares_stream_output_kind() -> None:
    contract = build_cli_contract()
    export = next(
        command
        for command in contract["commands"]
        if command["path"] == ["costs", "export"]
    )
    assert export["output_kind"] == "stream"
    assert export["json_schema"] is None


def test_underscore_rule_action_schema_is_declared_and_captured() -> None:
    assert "solwyn.budget.rule_action.v1" in declared_schema_tokens()
    schemas = {entry["schema"] for entry in build_cli_contract()["json_schemas"]}
    assert "solwyn.budget.rule_action.v1" in schemas


def test_account_costs_conditional_schema_is_declared_and_captured() -> None:
    assert "solwyn.account_costs.v1" in declared_schema_tokens()
    schemas = {entry["schema"] for entry in build_cli_contract()["json_schemas"]}
    assert "solwyn.account_costs.v1" in schemas
