"""budget-api — the service under test.

Deliberately boring: an in-memory item store with no database, because the store
isn't the point. The point is a service whose failure modes can be driven from the
outside, precisely enough that an alerting rule can be held to account.

Two wiring decisions worth calling out:

* **Chaos is a router-level dependency, not middleware.** It applies to the API
  routes only. Probes must stay healthy under injected failure, or kubelet restarts
  the pod halfway through a scenario and takes the experiment with it. The admin
  endpoint must stay reachable for the same reason — otherwise injecting 100%
  errors would be a one-way door.

* **Metrics middleware wraps everything, and excludes what shouldn't be measured.**
  The exclusion list lives in `metrics.py` alongside the reasoning.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.chaos import ChaosConfig, chaos
from app.metrics import CONTENT_TYPE, MetricsMiddleware, metrics


class ItemCreate(BaseModel):
    """Request body for creating an item."""

    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=0)


class Item(ItemCreate):
    """A stored item."""

    id: str


class ChaosRequest(BaseModel):
    """Partial update to the chaos config. Omitted fields are left unchanged.

    Bounds are declared here so FastAPI rejects bad input with a 422 before it
    reaches the controller; `ChaosConfig` validates again as defence in depth, so
    the invariant holds even for callers that bypass the API.
    """

    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: int | None = Field(default=None, ge=0)
    latency_jitter_ms: int | None = Field(default=None, ge=0)


class ChaosResponse(BaseModel):
    """The chaos config currently in force."""

    error_rate: float
    latency_ms: int
    latency_jitter_ms: int

    @classmethod
    def of(cls, config: ChaosConfig) -> ChaosResponse:
        return cls(
            error_rate=config.error_rate,
            latency_ms=config.latency_ms,
            latency_jitter_ms=config.latency_jitter_ms,
        )


_items: dict[str, Item] = {}


async def inject_chaos() -> None:
    """Apply the configured failure modes to this request.

    Latency is applied before the error decision, so an injected failure costs the
    same wall-clock time as a success. Real dependencies rarely fail instantly, and
    a fast-failing error path would quietly *improve* the latency SLI during an
    availability incident.
    """
    await chaos.apply_latency()
    if chaos.should_fail():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="chaos: injected failure",
        )


app = FastAPI(
    title="budget-api",
    description="A service with controllable failure modes, for SLO verification.",
    version="0.1.0",
)
app.add_middleware(MetricsMiddleware, metrics=metrics)


# ---------- operational endpoints (never chaos-affected, never in the SLI) ----------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness: is the process up? Answering at all is the answer."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    """Readiness: should this pod receive traffic?

    Nothing to wait on yet — no database, no warm cache — so it mirrors liveness.
    It exists as a separate endpoint because the moment there *is* a dependency,
    this is where the check belongs, and the manifests already point at it.
    """
    return {"status": "ready"}


@app.get("/metrics", tags=["ops"])
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=metrics.render(), media_type=CONTENT_TYPE)


# ---------- admin: chaos control (never chaos-affected, so it can't lock itself out) ----------

admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/chaos")
async def get_chaos() -> ChaosResponse:
    """Read the chaos config currently in force."""
    return ChaosResponse.of(chaos.config)


@admin.post("/chaos")
async def set_chaos(request: ChaosRequest) -> ChaosResponse:
    """Apply a partial update to the chaos config."""
    return ChaosResponse.of(chaos.update(**request.model_dump(exclude_none=True)))


@admin.delete("/chaos")
async def reset_chaos() -> ChaosResponse:
    """Return the service to healthy. The harness calls this between scenarios."""
    return ChaosResponse.of(chaos.reset())


app.include_router(admin)


# ---------- the API surface: what the SLOs are actually about ----------

api = APIRouter(prefix="/api/v1", tags=["items"], dependencies=[Depends(inject_chaos)])


@api.get("/items")
async def list_items() -> list[Item]:
    return list(_items.values())


@api.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(payload: ItemCreate) -> Item:
    item = Item(id=uuid.uuid4().hex, **payload.model_dump())
    _items[item.id] = item
    return item


@api.get("/items/{item_id}")
async def get_item(item_id: Annotated[str, Field(max_length=200)]) -> Item:
    item = _items.get(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")
    return item


app.include_router(api)
