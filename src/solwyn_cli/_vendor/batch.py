"""Opt-in Core batch journal contract, independent of the legacy SDK batch."""

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from solwyn_cli._vendor.wire import BudgetCheckRequest

ResultT = TypeVar("ResultT", bound=BaseModel)


def operation_expiry(value: str) -> datetime:
    identifier = UUID(value)
    if identifier.version != 7 or str(identifier) != value:
        raise ValueError("A canonical UUIDv7 operation ID is required")
    return datetime.fromtimestamp((identifier.int >> 80) / 1000, UTC) + timedelta(
        days=1
    )


def new_operation_id() -> str:
    """RFC 9562 UUIDv7: 48-bit UTC milliseconds, version/variant and random bits."""
    milliseconds = time.time_ns() // 1_000_000
    random = secrets.randbits(74)
    return str(
        UUID(
            int=(milliseconds << 80)
            | (7 << 76)
            | ((random >> 62) << 64)
            | (2 << 62)
            | (random & ((1 << 62) - 1))
        )
    )


class RecoverableBudgetCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=36, max_length=36)
    checks: list[BudgetCheckRequest] = Field(min_length=1, max_length=32)

    @field_validator("operation_id")
    @classmethod
    def validate_operation_id(cls, value: str) -> str:
        operation_expiry(value)
        return value


class BudgetCheckOutcome(BaseModel, Generic[ResultT]):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    index: int = Field(ge=0, le=31, strict=True)
    state: Literal["completed", "failed", "unattempted", "unknown"]
    result: ResultT | None = None
    error_code: Literal["check_failed", "reservation_unknown"] | None = None
    checked_at: datetime | None = None
    reservation_expires_at: datetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> "BudgetCheckOutcome":
        if self.state == "unattempted":
            if any(
                value is not None
                for value in (
                    self.result,
                    self.error_code,
                    self.checked_at,
                    self.reservation_expires_at,
                )
            ):
                raise ValueError("Unattempted item cannot contain an outcome")
            return self
        if self.checked_at is None or self.checked_at.tzinfo is None:
            raise ValueError("Attempted item requires an aware original timestamp")
        if self.state == "completed":
            if self.result is None or self.error_code is not None:
                raise ValueError("Completed item requires only a result")
            expected = (
                self.checked_at + timedelta(seconds=300)
                if self.result.reservation_id
                else None
            )
            if self.reservation_expires_at != expected:
                raise ValueError(
                    "Reservation authority must retain its original deadline"
                )
        elif (
            self.result is not None
            or self.reservation_expires_at is not None
            or self.error_code
            != {"failed": "check_failed", "unknown": "reservation_unknown"}[self.state]
        ):
            raise ValueError("Failed and unknown items require distinct fixed errors")
        return self


class RecoverableBudgetCheckResponse(BaseModel, Generic[ResultT]):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation_id: str
    status: Literal["running", "finished", "interrupted"]
    expires_at: datetime
    items: list[BudgetCheckOutcome[ResultT]] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def consistent(self) -> "RecoverableBudgetCheckResponse":
        if self.expires_at != operation_expiry(self.operation_id):
            raise ValueError("Operation expiry does not match its immutable identity")
        if [item.index for item in self.items] != list(range(len(self.items))):
            raise ValueError("Every original index must appear once in order")
        if self.status == "finished" and any(
            item.state != "completed" for item in self.items
        ):
            raise ValueError("Finished operations require completed items")
        # Execution is sequential: a terminal completed prefix, at most one
        # unknown/failed item, then an unattempted suffix. Interrupted admission
        # can have only unattempted items.
        suffix = False
        uncertain = False
        for item in self.items:
            if item.state == "completed":
                if suffix or uncertain:
                    raise ValueError("Completed item follows an unfinished item")
            elif item.state in {"unknown", "failed"}:
                if suffix or uncertain:
                    raise ValueError("Multiple or out-of-order attempted items")
                uncertain = True
            else:
                suffix = True
        return self
