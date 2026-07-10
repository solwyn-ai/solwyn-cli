"""Structural validation for project API keys.

This module intentionally mirrors only the public key shape. Authentication
remains the API's responsibility.
"""

import re
from typing import Final

PROJECT_KEY_PATTERN: Final = re.compile(r"^sk_proj_[a-f0-9]{64}$")


def validate_project_key_format(project_key: str) -> str:
    """Return a structurally valid project API key without authenticating it.

    Invalid values are never interpolated into the exception so credential
    material cannot leak through user-facing output or logs.
    """
    if PROJECT_KEY_PATTERN.fullmatch(project_key) is None:
        raise ValueError("Invalid project key format")
    return project_key
