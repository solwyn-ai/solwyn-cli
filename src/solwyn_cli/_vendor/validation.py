"""Structural validation for API keys and project selectors.

This module intentionally mirrors only the public shapes. Authentication
remains the API's responsibility.

- ``solwyn_sk_<64 lowercase hex>``: an account API key. A key holds one or more
  projects; every request with it names its project in the ``Solwyn-Project``
  header (a project name or ``proj_`` id). The old ``sk_proj_`` shape is gone.
- ``proj_<24 lowercase hex>``: a project id. A selector shaped like one, in any
  letter case, is always an id (compared lowercased); anything else is a name.
"""

import re
from typing import Final

API_KEY_PATTERN: Final = re.compile(r"^solwyn_sk_[a-f0-9]{64}$")
API_KEY_PREFIX_PATTERN: Final = re.compile(r"^solwyn_sk_[a-f0-9]{16}$")
API_KEY_PREFIX_LENGTH: Final = 26
PROJECT_ID_PATTERN: Final = re.compile(r"^proj_[a-f0-9]{24}$")
_PROJECT_ID_SHAPE_CI: Final = re.compile(r"^proj_[a-f0-9]{24}$", re.IGNORECASE)
PROJECT_HEADER: Final = "Solwyn-Project"
PROJECT_ID_HEADER: Final = "Solwyn-Project-Id"
# Core reads at most this many characters of a selector before answering 404.
PROJECT_SELECTOR_MAX_LENGTH: Final = 100


def validate_api_key_format(api_key: str) -> str:
    """Return a structurally valid API key without authenticating it.

    Invalid values are never interpolated into the exception so credential
    material cannot leak through user-facing output or logs.
    """
    if API_KEY_PATTERN.fullmatch(api_key) is None:
        raise ValueError("Invalid API key format")
    return api_key


def trim_selector(value: str) -> str:
    """Trim a project selector the way Core does (spaces and tabs only)."""
    return value.strip(" \t")


def is_project_id(value: str) -> bool:
    """True when a trimmed selector reads as a ``proj_`` id, in any case."""
    return _PROJECT_ID_SHAPE_CI.fullmatch(trim_selector(value)) is not None


def canonical_project_id(value: str) -> str:
    """Return an id-shaped selector lowercased; callers check is_project_id."""
    return trim_selector(value).lower()
