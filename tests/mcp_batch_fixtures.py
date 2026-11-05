"""Exact Core journal fixtures for MCP consumers."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

CHECKED = datetime(2026, 9, 6, tzinfo=UTC)
ID = str(UUID(int=(int(CHECKED.timestamp() * 1000) << 80) | (7 << 76) | (2 << 62) | 1))


def journal(results, operation_id=ID, status="finished"):
    expiry = datetime.fromtimestamp(
        (UUID(operation_id).int >> 80) / 1000, UTC
    ) + timedelta(days=1)
    return {
        "operation_id": operation_id,
        "status": status,
        "expires_at": expiry.isoformat(),
        "items": [
            {
                "index": i,
                "state": "completed",
                "result": deepcopy(value),
                "error_code": None,
                "checked_at": CHECKED.isoformat(),
                "reservation_expires_at": (CHECKED + timedelta(seconds=300)).isoformat()
                if value["reservation_id"]
                else None,
            }
            for i, value in enumerate(results)
        ],
    }
