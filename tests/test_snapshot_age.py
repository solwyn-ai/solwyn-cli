"""Freshness gate for the vendored OpenAPI snapshot."""

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest


SNAPSHOT_PATH = Path("src/solwyn_cli/spec/openapi_snapshot.json")
MAX_SNAPSHOT_AGE_DAYS = 30


def assert_snapshot_fresh(snapshot: dict[str, object], *, today: date) -> None:
    """Require an exact ISO date no more than thirty days in the past."""
    raw_date = snapshot.get("x-solwyn-snapshot-date")
    assert raw_date is not None, "x-solwyn-snapshot-date is missing"
    assert isinstance(raw_date, str), (
        "x-solwyn-snapshot-date must be a valid ISO UTC date (YYYY-MM-DD)"
    )
    try:
        synced_on = date.fromisoformat(raw_date)
    except ValueError:
        raise AssertionError(
            "x-solwyn-snapshot-date must be a valid ISO UTC date (YYYY-MM-DD)"
        ) from None
    assert synced_on.isoformat() == raw_date, (
        "x-solwyn-snapshot-date must be a valid ISO UTC date (YYYY-MM-DD)"
    )

    age_days = (today - synced_on).days
    assert age_days >= 0, "x-solwyn-snapshot-date cannot be in the future"
    assert age_days <= MAX_SNAPSHOT_AGE_DAYS, (
        f"OpenAPI snapshot is {age_days} days old; "
        f"maximum is {MAX_SNAPSHOT_AGE_DAYS} days"
    )


def _snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_checked_in_snapshot_is_at_most_thirty_days_old() -> None:
    assert_snapshot_fresh(_snapshot(), today=datetime.now(UTC).date())


def test_snapshot_exactly_thirty_days_old_is_accepted() -> None:
    today = date(2026, 8, 9)
    assert_snapshot_fresh(
        {"x-solwyn-snapshot-date": (today - timedelta(days=30)).isoformat()},
        today=today,
    )


def test_aged_mutated_snapshot_trips_freshness_gate() -> None:
    today = date(2026, 8, 10)
    mutated = copy.deepcopy(_snapshot())
    mutated["x-solwyn-snapshot-date"] = "2026-07-10"

    with pytest.raises(AssertionError, match="31 days old.*maximum is 30"):
        assert_snapshot_fresh(mutated, today=today)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "missing"),
        ("July 10, 2026", "valid ISO UTC date"),
        ("2026-07-10T00:00:00Z", "valid ISO UTC date"),
        ("2026-7-10", "valid ISO UTC date"),
    ],
)
def test_missing_or_invalid_snapshot_dates_fail_safely(
    value: object, message: str
) -> None:
    snapshot = {} if value is None else {"x-solwyn-snapshot-date": value}

    with pytest.raises(AssertionError, match=message):
        assert_snapshot_fresh(snapshot, today=date(2026, 7, 10))


def test_future_snapshot_date_fails_safely() -> None:
    with pytest.raises(AssertionError, match="future"):
        assert_snapshot_fresh(
            {"x-solwyn-snapshot-date": "2026-07-11"},
            today=date(2026, 7, 10),
        )
