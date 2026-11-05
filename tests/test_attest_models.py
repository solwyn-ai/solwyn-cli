"""Strict response models for spend-control attestations."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from solwyn_cli.commands.attest import SpendControlAttestation

GOLDEN = Path(__file__).parent / "golden" / "attest-project-json.json"


def _payload() -> dict[str, object]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_attestation_response_tree_validates_every_export_section() -> None:
    payload = _payload()

    report = SpendControlAttestation.model_validate(payload)

    assert report.model_dump(mode="json") == payload
    assert report.audit.events[0].actor_user_id is None
    assert report.window.retention_clamped is True


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("scope", "account_id"),
        ("window", "effective_from"),
        ("spend_summary", "real_call_count"),
        ("alerts", "coverage_note"),
        ("account_spend_alert", "evidence_available"),
        ("subscription", "changes"),
        ("audit", "events"),
    ],
)
def test_attestation_required_response_fields_fail_closed(
    section: str,
    field: str,
) -> None:
    payload = _payload()
    nested = payload[section]
    assert isinstance(nested, dict)
    nested.pop(field)

    with pytest.raises(ValidationError):
        SpendControlAttestation.model_validate(payload)


def test_attestation_nested_unknown_fields_fail_closed() -> None:
    payload = _payload()
    audit = payload["audit"]
    assert isinstance(audit, dict)
    audit["unexpected"] = True

    with pytest.raises(ValidationError):
        SpendControlAttestation.model_validate(payload)
