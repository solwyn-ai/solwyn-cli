"""Tests for the small vendored project-key validator."""

import pytest

from solwyn_cli._vendor.validation import validate_project_key_format


def test_project_key_validator_accepts_the_canonical_shape() -> None:
    key = "sk_proj_" + "a1" * 32

    assert validate_project_key_format(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "sk_proj_" + "a" * 63,
        "sk_proj_" + "A" * 64,
        "sk_proj_" + "é" * 64,
        "sk_proj_../" + "a" * 64,
    ],
)
def test_project_key_validator_rejects_invalid_keys_without_echoing_them(
    key: str,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_project_key_format(key)

    assert str(exc_info.value) == "Invalid project key format"
    assert key not in str(exc_info.value)
