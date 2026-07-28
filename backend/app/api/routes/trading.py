from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models.trade import OPEN_STATUSES, TradePlan
from app.services.trade_service import position_mid_from_quotes

router = APIRouter(prefix="/api", tags=["trading"])


class LegOrder(BaseModel):
    symbol: str
    right: str
    strike: float
    expiry: str
    side: int = Field(ge=-1, le=1)
    ratio: int = 1
    entry: float
    iv: float


class OrderIn(BaseModel):
    underlying: str
    strategy: str
    legs: list[LegOrder] = Field(min_length=1, max_length=4)
    qty: int = Field(ge=1, le=100)
    entry_limit: float
    tp_premium: float
    sl_premium: float
    time_stop_utc: str


class TightenIn(BaseModel):
    tp_premium: float | None = None
    sl_premium: float | None = None
    time_stop_utc: str | None = None


@router.get("/account")
async def account(request: Request) -> dict:
    try:
        acct = await request.app.state.trade.get_account()
    except Exception as exc:
        raise HTTPException(502, f"account fetch failed: {exc}")
    risk = await request.app.state.risk.get_settings()
    realized = await request.app.state.risk.todays_realized_pnl()
    return {**acct, "risk": risk, "day_realized_pnl": realized}


@router.put("/settings/risk")
async def update_risk(request: Request, patch: dict) -> dict:
    try:
        return await request.app.state.risk.update_settings(patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/orders")
async def place_order(request: Request, body: OrderIn) -> dict:
    try:
        return await request.app.state.trade.place_trade(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"order failed: {exc}")


@router.get("/positions")
async def positions(request: Request) -> dict:
    app = request.app
    plans = await app.state.risk.open_plans()
    out = []
    for plan in plans:
        quotes = {leg["symbol"]: app.state.market.latest_quote(leg["symbol"]) for leg in plan.legs}
        mid = position_mid_from_quotes(plan.legs, quotes)
        row = plan.to_dict()
        row["mark"] = round(mid, 4) if mid is not None else None
        row["quote_stale"] = mid is None
        if mid is not None and plan.fill_premium:
            row["unrealized_pnl"] = round((mid - plan.fill_premium) * 100 * plan.qty, 2)
        else:
            row["unrealized_pnl"] = None
        out.append(row)
    return {"positions": out}


@router.post("/positions/{plan_id}/close")
async def close_position(request: Request, plan_id: str) -> dict:
    try:
        await request.app.state.enforcer.manual_close(plan_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True}


@router.post("/positions/flatten")
async def flatten(request: Request) -> dict:
    count = await request.app.state.enforcer.flatten_all()
    return {"ok": True, "count": count}


@router.patch("/positions/{plan_id}/exits")
async def tighten(request: Request, plan_id: str, body: TightenIn) -> dict:
    try:
        ts = datetime.fromisoformat(body.time_stop_utc) if body.time_stop_utc else None
        plan = await request.app.state.enforcer.tighten_exits(
            plan_id, body.tp_premium, body.sl_premium, ts
        )
        return plan.to_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/history")
async def history(request: Request, limit: int = 200) -> dict:
    async with request.app.state.db.session() as session:
        result = await session.execute(
            select(TradePlan)
            .where(TradePlan.status.notin_(OPEN_STATUSES))
            .order_by(TradePlan.created_at.desc())
            .limit(min(limit, 1000))
        )
        return {"trades": [plan.to_dict() for plan in result.scalars()]}
