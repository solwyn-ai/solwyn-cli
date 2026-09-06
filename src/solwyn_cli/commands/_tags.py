"""Verbatim SDK-compatible tag parsing shared by attribution surfaces."""

import os
from collections.abc import Mapping, Sequence

import typer


def validate_tags(tags: Mapping[str, str] | None) -> dict[str, str] | None:
    """Copy bounded plaintext tags without coercion or whitespace trimming."""
    if tags is None:
        return None
    if not isinstance(tags, Mapping) or len(tags) > 10:
        raise ValueError("Tags must be a mapping with at most 10 keys.")
    for key, value in tags.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Tag keys and values must be strings.")
        if not 1 <= len(key) <= 64 or len(value) > 256:
            raise ValueError("Tag keys must be 1–64 characters; values at most 256.")
        if "\x00" in key or "\x00" in value:
            raise ValueError("Tags cannot contain NUL.")
    return dict(tags) or None


def parse_tags(
    flags: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Merge environment then repeated flags, splitting only the first equals."""
    env = os.environ if environ is None else environ
    entries = env["SOLWYN_TAGS"].split(",") if "SOLWYN_TAGS" in env else []
    result: dict[str, str] = {}
    for entry in [*entries, *(flags or [])]:
        if "=" not in entry or "," in entry:
            raise ValueError(
                "Tags entries must use key=value; commas cannot appear in values."
            )
        key, value = entry.split("=", 1)
        validate_tags({key: value})
        if key in result:
            typer.echo(f"Duplicate tag {key!r}: last value wins.", err=True)
        result[key] = value
    return validate_tags(result)
