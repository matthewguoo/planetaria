"""The administration window's vitals: request latency / throughput, broker
call latency, the ticker cache, every exit monitor with its state, and one
plan's own activity feed (its journal + the log lines about it). Read-only;
mounted on both servers next to routes/admin.py."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.routes import admin as admin_routes
from app.api.routes.admin import tail_lines
from app.services.call_log import CALL_LOG
from app.services.vitals import REQUESTS, broker_vitals, cache_vitals, monitors_status

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/vitals")
async def admin_vitals(request: Request) -> dict:
    st = request.app.state
    plans: list = []
    try:
        plans = await st.risk.open_plans()
    except Exception:  # noqa: BLE001
        pass
    monitors = monitors_status(st.enforcer, plans) if getattr(st, "enforcer", None) else []
    cache = cache_vitals(st.market, getattr(st, "contracts", None), getattr(st, "chain", None)) \
        if getattr(st, "market", None) else {"tickers": [], "tickers_cached": 0}
    return {
        "requests": REQUESTS.summary(),
        "broker": broker_vitals(CALL_LOG),
        "cache": cache,
        "monitors": monitors,
        "monitors_ok": sum(1 for m in monitors if m["ok"]),
    }


@router.get("/plans/{plan_id}/feed")
async def admin_plan_feed(request: Request, plan_id: str, limit: int = 80, log_lines: int = 60) -> dict:
    """One plan's activity: its journal rows and the engine log lines that
    name it (the enforcer logs 'plan <id>' on every decision)."""
    st = request.app.state
    try:
        plan = await st.trade.get_plan(plan_id)
    except Exception:  # noqa: BLE001
        plan = None
    if plan is None:
        raise HTTPException(404, f"no plan {plan_id}")
    events = await st.trade.fsm.events_for(plan_id, min(limit, 300))
    path = admin_routes.log_file_path()
    lines: list[str] = []
    if path is not None and path.exists():
        needle = plan_id
        short = plan_id[:8]
        lines = [ln for ln in tail_lines(path, 4000, max_bytes=2_000_000)
                 if needle in ln or short in ln][-min(log_lines, 300):]
    return {"plan": plan.to_dict(), "events": events, "log": lines}
