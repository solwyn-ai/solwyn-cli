"""Customer phone state and advisory outage traffic read contracts."""

from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from solwyn_cli.auth import JwtRequiredError, JwtCredential
from solwyn_cli.client import SolwynClient

UNCOUNTED_NOTICE = "Advisory unpriced upper bound: possibly overcounted, never priced spend. An unavailable tally is unknown; retry the read."


class UncountedTallyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    project_id: str
    uncounted_calls: int
    uncounted_tokens: int
    last_reported_at: datetime | None


class AccountPhoneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    state: Literal["none", "pending", "verified", "verified_with_pending_change"]
    phone_number: str | None = None
    verified: bool = False
    verified_at: datetime | None = None
    pending_phone_number: str | None = None
    verification_expires_at: datetime | None = None
    resend_available_at: datetime | None = None
    sms_alerts_consented_at: datetime | None = None
    sms_alerts_disclosure_version: str | None = None


def fetch_uncounted(*, client: SolwynClient, project_id: str) -> UncountedTallyResponse:
    data = UncountedTallyResponse.model_validate(
        client.request(
            "GET", f"/api/v1/projects/{quote(project_id, safe='')}/budget/uncounted"
        ).json()
    )
    if data.project_id != project_id:
        raise ValueError("Uncounted response project did not match.")
    return data


def fetch_phone(*, client: SolwynClient) -> AccountPhoneResponse:
    if not isinstance(client.current_credential, JwtCredential):
        raise JwtRequiredError("Phone state requires JWT. Run solwyn login.")
    return AccountPhoneResponse.model_validate(
        client.request("GET", "/api/v1/account/phone").json()
    )
