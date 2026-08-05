"""Strategy control plane. Engine API — mounted unconditionally (survives
HEADLESS); the GUI is just another client of these routes with no privileged
path. Thin adapters over app.state.strategy_runner, same error mapping as
trading.py (ValueError -> 422)."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.signals.events import Event
from app.strategies import REGISTRY

router = APIRouter(prefix="/api", tags=["strategies"])


class CreateIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=24)
    params: dict = Field(default_factory=dict)


class ParamsIn(BaseModel):
    params: dict


class KillAllIn(BaseModel):
    flatten: bool = False


class TriggerIn(BaseModel):
    payload: dict = Field(default_factory=dict)


@router.get("/strategies/catalog")
async def catalog() -> dict:
    return {
        "kinds": [
            {
                "kind": cls.kind,
                "subscriptions": list(cls.subscriptions),
                "default_params": cls.default_params,
                "doc": (cls.__doc__ or "").strip().split("\n")[0],
            }
            for cls in REGISTRY.values()
        ]
    }


@router.get("/strategies")
async def list_instances(request: Request) -> dict:
    return {"instances": await request.app.state.strategy_runner.instances()}


@router.post("/strategies")
async def create_instance(request: Request, body: CreateIn) -> dict:
    try:
        return await request.app.state.strategy_runner.create(
            body.kind, body.name, body.params
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/strategies/{row_id}")
async def get_instance(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.patch("/strategies/{row_id}")
async def patch_params(request: Request, row_id: str, body: ParamsIn) -> dict:
    try:
        return await request.app.state.strategy_runner.update_params(row_id, body.params)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/enable")
async def enable(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.set_state(row_id, "enabled")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/pause")
async def pause(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.set_state(row_id, "paused")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/flatten")
async def flatten(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.flatten(row_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/kill-all")
async def kill_all(request: Request, body: KillAllIn) -> dict:
    return await request.app.state.strategy_runner.kill_all(body.flatten)


@router.post("/strategies/{row_id}/trigger")
async def trigger(request: Request, row_id: str, body: TriggerIn) -> dict:
    """Journal a manual signal and publish it scoped to this instance —
    the human-in-the-loop entry point (and the ref_tick proof knob)."""
    runner = request.app.state.strategy_runner
    try:
        instance = await runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if instance["state"] != "enabled":
        raise HTTPException(422, f"instance is {instance['state']}, not enabled")
    store = request.app.state.signal_store
    now = datetime.now(timezone.utc)
    event = Event(
        type="manual", ts=now, source="api-trigger",
        key=f"{row_id}:{now.isoformat()}", symbols=(),
        payload={"strategy_id": row_id, **body.payload},
    )
    event, _ = await store.record(event)
    request.app.state.event_bus.publish(event)
    return {"ok": True, "signal_id": event.signal_id}


@router.get("/strategies/{row_id}/decisions")
async def decisions(request: Request, row_id: str, limit: int = 100) -> dict:
    return {"decisions": await request.app.state.strategy_runner.decisions(row_id, limit)}


@router.get("/signals")
async def signals(
    request: Request, limit: int = 100, type: str | None = None,
    symbol: str | None = None,
) -> dict:
    return {"signals": await request.app.state.signal_store.recent(limit, type, symbol)}
