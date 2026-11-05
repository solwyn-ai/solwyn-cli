"""Tests for the small vendored API key and project selector validators."""

import pytest

from solwyn_cli._vendor.validation import (
    canonical_project_id,
    is_project_id,
    trim_selector,
    validate_api_key_format,
)


def test_api_key_validator_accepts_the_canonical_shape() -> None:
    key = "solwyn_sk_" + "a1" * 32

    assert validate_api_key_format(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "solwyn_sk_" + "a" * 63,
        "solwyn_sk_" + "A" * 64,
        "solwyn_sk_" + "é" * 64,
        "solwyn_sk_../" + "a" * 64,
        # The 0.4.x project key shape is gone, with no alias.
        "sk_proj_" + "a1" * 32,
    ],
)
def test_api_key_validator_rejects_invalid_keys_without_echoing_them(key: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_api_key_format(key)

    assert str(exc_info.value) == "Invalid API key format"
    assert key not in str(exc_info.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("proj_" + "a" * 24, True),
        ("  PROJ_" + "A" * 24 + "\t", True),
        ("proj_" + "a" * 23, False),
        ("checkout-bot", False),
        ("proj_" + "g" * 24, False),
    ],
)
def test_project_id_shape_is_case_insensitive_after_trimming(
    value: str, expected: bool
) -> None:
    assert is_project_id(value) is expected


def test_selectors_trim_spaces_and_tabs_only_and_ids_lowercase() -> None:
    assert trim_selector(" \tcheckout bot \t") == "checkout bot"
    assert trim_selector("\ncheckout\n") == "\ncheckout\n"
    assert canonical_project_id(" PROJ_" + "AB" * 12) == "proj_" + "ab" * 12
