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


@router.get("/portfolio")
async def portfolio(request: Request, period: str = "1M", timeframe: str = "1D") -> dict:
    """Cross-account portfolio: every registered paper account's equity,
    day P/L, positions and curve — read-only, 30s cached."""
    if period not in {"1D", "1W", "1M", "3M", "6M", "1A", "all"}:
        raise HTTPException(422, "period must be one of 1D/1W/1M/3M/6M/1A/all")
    if timeframe not in {"1Min", "5Min", "15Min", "1H", "1D"}:
        raise HTTPException(422, "timeframe must be one of 1Min/5Min/15Min/1H/1D")
    try:
        return await request.app.state.portfolio_accounts.snapshot(period, timeframe)
    except Exception as exc:
        raise HTTPException(502, f"portfolio snapshot failed: {exc}")


@router.get("/market/clock")
async def market_clock(request: Request) -> dict:
    """Broker session calendar: open now, and the next open/close.

    `/api/system/state` reports the enforcer's CACHED clock, which stays
    `{known: false}` until something asks — and with no open plans nothing
    ever does, so a page that reads only the cache shows UNKNOWN forever.
    This forces the fetch. The snapshot is reused for a whole session regime,
    so polling this is one broker call per regime, not one per request.
    """
    clock = request.app.state.enforcer.clock
    try:
        await clock.is_open()
    except Exception as exc:                                  # noqa: BLE001
        # Fail soft: a calendar we could not fetch is unknown, not closed.
        # Saying "CLOSED" on a network blip is a guess dressed as a fact.
        return {**clock.status(), "error": str(exc)}
    return clock.status()


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


@router.get("/system/accounts")
async def list_accounts(request: Request) -> dict:
    return await request.app.state.accounts.list_accounts()


@router.post("/system/accounts/select")
async def select_account(request: Request, body: dict) -> dict:
    """Persist which PAPER account the engine uses; applies at next boot.
    Refused while any plan is open (see AccountService docstring)."""
    from sqlalchemy import func, select

    from app.models.trade import OPEN_STATUSES, TradePlan

    name = str(body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(422, "name required")
    async with request.app.state.db.session() as session:
        open_plans = await session.scalar(
            select(func.count()).select_from(TradePlan)
            .where(TradePlan.status.in_(OPEN_STATUSES))
        )
    try:
        return await request.app.state.accounts.select(name, int(open_plans or 0))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/positions/{plan_id}/events")
async def plan_events(request: Request, plan_id: str, limit: int = 100) -> dict:
    """Append-only lifecycle journal for one plan — every event that reached
    the FSM, including dropped ones (the audit trail)."""
    events = await request.app.state.trade.fsm.events_for(plan_id, min(limit, 500))
    return {"plan_id": plan_id, "events": events}
