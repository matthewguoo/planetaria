"""System state + feed settings + plan event journal."""

from fastapi import APIRouter, HTTPException, Request

from app.services.system_state import system_state

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system/state")
async def get_system_state(request: Request) -> dict:
    try:
        return await system_state(request.app.state)
    except Exception as exc:
        raise HTTPException(502, f"system state failed: {exc}")


@router.get("/settings/feed")
async def get_feed_settings(request: Request) -> dict:
    return await request.app.state.feed_settings.get()


@router.put("/settings/feed")
async def update_feed_settings(request: Request, patch: dict) -> dict:
    try:
        updated = await request.app.state.feed_settings.update(patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    # (public_poll_s is stored but applies to nothing since the keyless demo
    # feed was removed 2026-08-05; field kept for settings-schema stability.)
    return updated


@router.get("/positions/{plan_id}/events")
async def plan_events(request: Request, plan_id: str, limit: int = 100) -> dict:
    """Append-only lifecycle journal for one plan — every event that reached
    the FSM, including dropped ones (the audit trail)."""
    events = await request.app.state.trade.fsm.events_for(plan_id, min(limit, 500))
    return {"plan_id": plan_id, "events": events}
