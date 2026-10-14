"""Typed analytics and read-only pricing requests shared by CLI/MCP."""

from typing import Any, TypeVar
from urllib.parse import quote

from pydantic import BaseModel

from solwyn_cli import _mcp_operation as operation
from solwyn_cli._vendor.wire import CostSimulationRequest, ProviderName, ServiceTier
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._cost_advice_models import (
    PricedModelsResponse,
    CostSimulationResponse,
)
from solwyn_cli.commands._cost_models import CostEventsResponse
from solwyn_cli.commands._cost_filters import CostQueryError, cost_params, window_params

T = TypeVar("T", bound=BaseModel)


def fetch_project_read(
    *,
    client: SolwynClient,
    project_id: str,
    path: str,
    response_model: type[T],
    params: dict[str, Any] | None = None,
    body: BaseModel | None = None,
) -> T:
    kwargs = {"params": params} if params is not None else {}
    if body is not None:
        kwargs["json"] = body.model_dump(mode="json")
    if path == "costs/simulate":
        kwargs.update(retry_rate_limit=False, retry_auth=False)
    data = response_model.model_validate(
        client.request(
            "POST" if body is not None else "GET",
            f"/api/v1/projects/{quote(project_id, safe='')}/{path}",
            **kwargs,
        ).json()
    )
    if hasattr(data, "project_id") and data.project_id != project_id:
        raise ValueError("Cost read response project did not match.")
    return data


def fetch_priced_models(
    *, client: SolwynClient, project_id: str
) -> PricedModelsResponse:
    return fetch_project_read(
        client=client,
        project_id=project_id,
        path="costs/priced-models",
        response_model=PricedModelsResponse,
    )


def simulation_request(
    *,
    provider: ProviderName,
    model: str,
    region: str | None = None,
    service_tier: ServiceTier | None = None,
) -> CostSimulationRequest:
    region = region.strip() if region is not None else None
    if provider is ProviderName.BEDROCK:
        if not region or service_tier is None:
            raise CostQueryError("Bedrock simulation requires region and service_tier.")
    elif region is not None or service_tier is not None:
        raise CostQueryError("Region and service_tier are only supported for Bedrock.")
    return CostSimulationRequest(
        target_provider=provider,
        target_model=model,
        target_region=region,
        target_service_tier=service_tier,
    )


def fetch_simulation(
    *,
    client: SolwynClient,
    project_id: str,
    request: CostSimulationRequest,
    time_range: str = "7d",
    from_: str | None = None,
    to: str | None = None,
) -> CostSimulationResponse:
    return fetch_project_read(
        client=client,
        project_id=project_id,
        path="costs/simulate",
        response_model=CostSimulationResponse,
        params=window_params(time_range=time_range, from_=from_, to=to),
        body=request,
    )


def fetch_cost_events(
    *,
    client: SolwynClient,
    project_id: str,
    cursor: str | None = None,
    limit: int = 50,
    all_pages: bool = False,
    **filters: Any,
) -> CostEventsResponse:
    if not 1 <= limit <= 100:
        raise CostQueryError("Cost event limit must be 1–100.")
    params = cost_params(**filters) | {"limit": limit}
    items = []
    seen: set[str] = set()
    window = None
    while True:
        operation.page(cursor)
        if cursor is not None:
            if cursor in seen:
                raise ValueError("Cost events repeated a cursor.")
            seen.add(cursor)
        page = fetch_project_read(
            client=client,
            project_id=project_id,
            path="costs/events",
            response_model=CostEventsResponse,
            params=params | ({"cursor": cursor} if cursor is not None else {}),
        )
        if any(item.project_id != project_id for item in page.items):
            raise ValueError("Cost event project did not match.")
        if page.has_more != (page.next_cursor is not None):
            raise ValueError("Cost event continuation was inconsistent.")
        items.extend(page.items)
        if window is None:
            window = page.window
        else:
            window.retention_clamped |= page.window.retention_clamped
        cursor = page.next_cursor
        data = CostEventsResponse(
            items=items, next_cursor=cursor, has_more=page.has_more, window=window
        )
        operation.retain(data, cursor=cursor)
        if not all_pages or cursor is None:
            return data
