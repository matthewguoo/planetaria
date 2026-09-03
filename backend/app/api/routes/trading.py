from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.models.trade import OPEN_STATUSES, TradePlan
from app.services.trade_service import position_mid_from_quotes

router = APIRouter(prefix="/api", tags=["trading"])


class LegOrder(BaseModel):
    symbol: str
    # Option-contract fields; optional so an equity leg (shares) can omit
    # them — place_trade enforces the structure per asset_class.
    right: str | None = None
    strike: float | None = None
    expiry: str | None = None
    side: Literal[-1, 1]
    ratio: int = 1
    entry: float
    iv: float | None = None
    half_spread: float | None = Field(default=None, ge=0)  # liquidity guard input
    # "open" = market-on-open entry (equity only; refused past 09:28 ET).
    auction: Literal["open"] | None = None


class OrderIn(BaseModel):
    underlying: str
    strategy: str
    asset_class: str = "option"
    extended_hours: bool = False
    strategy_id: str | None = None
    legs: list[LegOrder] = Field(min_length=1, max_length=4)
    qty: int = Field(ge=1, le=10_000)  # contract sets / shares
    entry_limit: float
    # Nullable exits: place_trade validates the allowed combinations —
    # options need both-or-neither; equity additionally allows SL-only
    # (swing entries that let winners run under a hard stop).
    tp_premium: float | None = None
    sl_premium: float | None = None
    # Optional for equity swing plans (no time stop = held until TP/SL or
    # manual close); place_trade still requires it for options.
    time_stop_utc: str | None = None


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


@router.get("/account/risk")
async def account_risk(request: Request) -> dict:
    """Portfolio risk snapshot: at-stop dollars/percent (simple and
    correlation-adjusted), aggregate greeks incl. rate sensitivity, and
    per-underlying beta/correlation factor rows."""
    try:
        return await request.app.state.portfolio_risk.snapshot()
    except Exception as exc:
        raise HTTPException(502, f"risk snapshot failed: {exc}")


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
        row["unrealized_pnl"] = plan.pnl_at(mid)
        out.append(row)

    # Live broker positions with no managing plan — surfaced so nothing in
    # the account can be invisible to the UI (and adoptable into management).
    try:
        untracked = await app.state.trade.untracked_positions(plans)
    except Exception as exc:  # broker down != positions page down
        return {"positions": out, "untracked": [], "untracked_error": str(exc)}
    return {"positions": out, "untracked": untracked}


class AdoptIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=32)
    tp_pct: float | None = Field(default=None, gt=0, le=10)
    sl_pct: float | None = Field(default=None, gt=0, le=0.95)
    time_stop_utc: str | None = None


@router.post("/positions/adopt")
async def adopt_positions(request: Request, body: AdoptIn) -> dict:
    """Fold untracked broker positions into managed plans with TP/SL/
    time-stop enforcement. Options group into multi-leg plans (one per
    underlying); stock positions adopt one single-leg equity plan per
    symbol at floor(qty) whole shares — a fractional residue stays
    untracked (close it by hand at the broker). NOTE: tp_pct/sl_pct are
    fractions of the ENTRY BASIS — for equity pass explicit values (the
    option defaults, e.g. a 50% stop, are not share-sized)."""
    app = request.app
    risk = await app.state.risk.get_settings()
    tp_pct = body.tp_pct if body.tp_pct is not None else risk["default_tp_pct"]
    sl_pct = body.sl_pct if body.sl_pct is not None else risk["default_sl_pct"]
    if body.time_stop_utc:
        time_stop = datetime.fromisoformat(body.time_stop_utc)
        if time_stop.tzinfo is None:
            time_stop = time_stop.replace(tzinfo=timezone.utc)
    else:
        # Default: today's configured cutoff (ET) — same default the designer uses.
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        hh, mm = str(risk["time_stop_et"]).split(":")
        now_et = datetime.now(et)
        time_stop = now_et.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0).astimezone(
            timezone.utc
        )
    try:
        adopted = await app.state.trade.adopt_positions(body.symbols, tp_pct, sl_pct, time_stop)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"adopt failed: {exc}")
    return {"adopted": adopted}


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


@router.get("/account/history")
async def account_history(request: Request, period: str = "1M", timeframe: str = "1D") -> dict:
    """Equity curve straight from the broker (Alpaca portfolio history)."""
    if period not in {"1D", "1W", "1M", "3M", "6M", "1A", "all"}:
        raise HTTPException(422, "period must be one of 1D/1W/1M/3M/6M/1A/all")
    if timeframe not in {"1Min", "5Min", "15Min", "1H", "1D"}:
        raise HTTPException(422, "timeframe must be one of 1Min/5Min/15Min/1H/1D")
    trade = request.app.state.trade
    if not trade.alpaca.configured:
        return {"timestamps": [], "equity": [], "profit_loss": [], "base_value": None}
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        hist = await trade.alpaca.call(
            trade.alpaca.trading.get_portfolio_history,
            GetPortfolioHistoryRequest(period=period, timeframe=timeframe),
        )
        return {
            "timestamps": list(hist.timestamp or []),
            "equity": [float(v) if v is not None else None for v in (hist.equity or [])],
            "profit_loss": [
                float(v) if v is not None else None for v in (hist.profit_loss or [])
            ],
            "base_value": float(hist.base_value) if hist.base_value is not None else None,
        }
    except Exception as exc:
        raise HTTPException(502, f"portfolio history failed: {exc}")


@router.get("/orders/open")
async def open_orders(request: Request) -> dict:
    """Live open orders at the broker, normalized (incl. MLEG legs)."""
    trade = request.app.state.trade
    if not trade.alpaca.configured:
        return {"orders": []}
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = await trade.alpaca.call(
            trade.alpaca.trading.get_orders,
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=100),
        )
    except Exception as exc:
        raise HTTPException(502, f"orders fetch failed: {exc}")

    def _norm(order) -> dict:
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "side": str(getattr(order.side, "value", order.side) or ""),
            "qty": float(order.qty) if order.qty is not None else None,
            "filled_qty": float(order.filled_qty or 0),
            "type": str(getattr(order.order_type, "value", order.order_type) or ""),
            "limit_price": float(order.limit_price) if order.limit_price is not None else None,
            "status": str(getattr(order.status, "value", order.status) or ""),
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "side": str(getattr(leg.side, "value", leg.side) or ""),
                    "ratio": float(leg.ratio_qty) if getattr(leg, "ratio_qty", None) else 1,
                }
                for leg in (order.legs or [])
            ],
        }

    return {"orders": [_norm(o) for o in orders]}


@router.delete("/orders/{order_id}")
async def cancel_order(request: Request, order_id: str) -> dict:
    trade = request.app.state.trade
    if not trade.alpaca.configured:
        raise HTTPException(503, "broker not configured")
    try:
        await trade.cancel_order(order_id)
    except Exception as exc:
        raise HTTPException(502, f"cancel failed: {exc}")
    return {"ok": True}


@router.get("/history")
async def history(request: Request, limit: int = 200) -> dict:
    """Closed trades enriched with entry/exit timestamps from the lifecycle
    journal — the chart's historical position view anchors its surface at the
    actual entry fill and draws the exit marker at the actual exit event."""
    from app.models.trade import PlanEventRow, as_utc

    ENTRY_EVENTS = ("entry_filled", "entry_partial_fill")
    EXIT_EVENTS = ("exit_filled", "force_closed")
    async with request.app.state.db.session() as session:
        result = await session.execute(
            select(TradePlan)
            .where(TradePlan.status.notin_(OPEN_STATUSES))
            .order_by(TradePlan.created_at.desc())
            .limit(min(limit, 1000))
        )
        plans = list(result.scalars())
        rows = [plan.to_dict() for plan in plans]
        ids = [plan.id for plan in plans]
        if ids:
            events = await session.execute(
                select(PlanEventRow.plan_id, PlanEventRow.event, PlanEventRow.ts)
                .where(
                    PlanEventRow.plan_id.in_(ids),
                    PlanEventRow.applied == 1,
                    PlanEventRow.event.in_(ENTRY_EVENTS + EXIT_EVENTS),
                )
                .order_by(PlanEventRow.id.asc())
            )
            entered: dict[str, str] = {}
            exited: dict[str, str] = {}
            for plan_id, event, ts in events:
                if ts is None:
                    continue
                iso = as_utc(ts).isoformat()
                if event in ENTRY_EVENTS and plan_id not in entered:
                    entered[plan_id] = iso
                elif event in EXIT_EVENTS:
                    exited[plan_id] = iso  # last exit event wins
            for row in rows:
                row["entered_at"] = entered.get(row["id"])
                # Broker fill time (exited_at column) wins; the journal ts is
                # when the ENGINE learned of the close — hours late for an
                # external liquidation captured by a later reconcile.
                row["exited_at"] = row.get("exited_at") or exited.get(row["id"])
        return {"trades": rows}
