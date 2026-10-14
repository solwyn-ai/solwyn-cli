"""Guarded CLI aggregation with the same cursor semantics as audit --all."""

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def read_pages(
    fetch: Callable[[str | None], T],
    *,
    field: str,
    cursor: str | None,
    all_pages: bool,
) -> T:
    """Keep order and final continuation; never emit a partial success on failure."""
    seen: set[str] = set()
    rows = []
    window = None
    while True:
        if cursor is not None:
            if cursor in seen:
                raise ValueError("Pagination repeated a cursor.")
            seen.add(cursor)
        page = fetch(cursor)
        if page.has_more != (page.next_cursor is not None):
            raise ValueError("Pagination state was inconsistent.")
        if not all_pages:
            return page
        rows.extend(getattr(page, field))
        if hasattr(page, "window"):
            if window is None:
                window = page.window.model_copy(deep=True)
            else:
                window.retention_clamped |= page.window.retention_clamped
        updates = {field: rows}
        if window is not None:
            updates["window"] = window
        data = page.model_copy(update=updates)
        if not page.has_more:
            return data
        cursor = page.next_cursor
