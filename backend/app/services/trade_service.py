"""Trade lifecycle: validated placement -> DB-persisted plan -> Alpaca paper
order -> fill tracking -> exit handoff to the enforcer.

Discipline invariants enforced here, not in the UI:
- No order without TP, SL, and time stop.
- Plan row committed BEFORE the order hits Alpaca.
- Paper-only (AlpacaService is constructed paper=True; config refuses live).
"""

import asyncio
import logging
from datetime import datetime, timezone

from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from app.models.trade import OPEN_STATUSES, TradePlan
from app.services.alpaca import AlpacaService

log = logging.getLogger("app.trade")

TICK = 0.01


def round_tick(x: float) -> float:
    return max(round(round(x / TICK) * TICK, 2), TICK)


def position_mid_from_quotes(legs: list[dict], quotes: dict[str, dict]) -> float | None:
    """Side-weighted net mid per share; None if any leg quote is missing."""
    total = 0.0
    for leg in legs:
        quote = quotes.get(leg["symbol"])
        if not quote or not (quote.get("bid") or quote.get("ask")):
            return None
        total += leg["side"] * leg.get("ratio", 1) * quote["mid"]
    return total


class TradeService:
    def __init__(self, db, alpaca: AlpacaService, market, risk):
        self.db = db
        self.alpaca = alpaca
        self.market = market
        self.risk = risk
        self.enforcer = None  # set by ExitEnforcer at startup (circular)
        self._account_cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------- account

    async def get_account(self) -> dict:
        import time as _time

        if self._account_cache and _time.monotonic() - self._account_cache[0] < 10:
            return self._account_cache[1]
        if not self.alpaca.configured:
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0,
                    "daytrade_count": 0, "status": "NO_KEYS", "paper": True}
        acct = await self.alpaca.call(self.alpaca.trading.get_account)
        out = {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "daytrade_count": int(acct.daytrade_count or 0),
            "status": str(acct.status.value if hasattr(acct.status, "value") else acct.status),
            "paper": True,
        }
        self._account_cache = (_time.monotonic(), out)
        return out

    # ------------------------------------------------------------ placement

    async def place_trade(self, payload: dict) -> dict:
        """payload: {underlying, strategy, legs:[{symbol,right,strike,expiry,side,ratio,entry,iv}],
        qty, entry_limit, tp_premium, sl_premium, time_stop_utc}"""
        if not self.alpaca.configured:
            raise ValueError("Alpaca keys not configured - trading unavailable")

        legs = payload["legs"]
        qty = int(payload["qty"])
        entry_limit = float(payload["entry_limit"])
        tp = float(payload["tp_premium"])
        sl = float(payload["sl_premium"])
        time_stop = datetime.fromisoformat(payload["time_stop_utc"])
        if time_stop.tzinfo is None:
            time_stop = time_stop.replace(tzinfo=timezone.utc)

        if qty < 1:
            raise ValueError("qty must be >= 1")
        if not legs or len(legs) > 4:
            raise ValueError("1-4 legs required")
        if entry_limit <= 0:
            raise ValueError("net credit entries not supported in v1")
        if not (sl < entry_limit < tp):
            raise ValueError(
                f"exits must bracket entry: SL {sl} < entry {entry_limit} < TP {tp}"
            )

        account = await self.get_account()
        entry_cost = entry_limit * 100 * qty
        max_loss = (entry_limit - sl) * 100 * qty
        expiry = max(leg["expiry"] for leg in legs)
        violations = await self.risk.validate_new_trade(
            account_equity=account["equity"],
            entry_cost_dollars=entry_cost,
            max_loss_dollars=max_loss,
            time_stop_utc=time_stop,
            expiry_date_et=expiry,
        )
        if violations:
            raise ValueError("; ".join(violations))

        plan = TradePlan(
            underlying=payload["underlying"].upper(),
            strategy=payload["strategy"],
            legs=legs,
            qty=qty,
            entry_limit=entry_limit,
            tp_premium=tp,
            sl_premium=sl,
            time_stop_utc=time_stop,
            status="planned",
        )
        async with self.db.session() as session:
            session.add(plan)
            await session.commit()
            await session.refresh(plan)

        try:
            order = await self._submit_entry(plan)
        except Exception as exc:
            await self._update_plan(plan.id, status="rejected", notes=str(exc))
            raise ValueError(f"order rejected: {exc}")

        await self._update_plan(plan.id, status="submitted", entry_order_id=str(order.id))
        if self.enforcer:
            await self.enforcer.arm(plan.id)
        log.info("plan %s submitted (%s x%d, order %s)", plan.id, plan.strategy, qty, order.id)
        return (await self.get_plan(plan.id)).to_dict()

    async def _submit_entry(self, plan: TradePlan):
        legs = plan.legs
        if len(legs) == 1:
            leg = legs[0]
            request = LimitOrderRequest(
                symbol=leg["symbol"],
                qty=plan.qty * leg.get("ratio", 1),
                side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                position_intent=(
                    PositionIntent.BUY_TO_OPEN if leg["side"] > 0 else PositionIntent.SELL_TO_OPEN
                ),
                limit_price=round_tick(plan.entry_limit),
                time_in_force=TimeInForce.DAY,
            )
        else:
            request = LimitOrderRequest(
                order_class=OrderClass.MLEG,
                qty=plan.qty,
                limit_price=round_tick(plan.entry_limit),
                time_in_force=TimeInForce.DAY,
                legs=[
                    OptionLegRequest(
                        symbol=leg["symbol"],
                        ratio_qty=leg.get("ratio", 1),
                        side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                        position_intent=(
                            PositionIntent.BUY_TO_OPEN
                            if leg["side"] > 0
                            else PositionIntent.SELL_TO_OPEN
                        ),
                    )
                    for leg in legs
                ],
            )
        return await self.alpaca.call(self.alpaca.trading.submit_order, request)

    # ------------------------------------------------------------- updates

    async def get_plan(self, plan_id: str) -> TradePlan:
        async with self.db.session() as session:
            plan = await session.get(TradePlan, plan_id)
            if plan is None:
                raise ValueError(f"no plan {plan_id}")
            return plan

    async def _update_plan(self, plan_id: str, **fields) -> TradePlan:
        async with self.db.session() as session:
            plan = await session.get(TradePlan, plan_id)
            for key, value in fields.items():
                setattr(plan, key, value)
            await session.commit()
            await session.refresh(plan)
        self.market.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
        return plan

    async def on_trade_update(self, update) -> None:
        """TradingStream handler (fills for entry AND exit orders)."""
        try:
            order = update.order
            order_id = str(order.id)
            event = str(update.event)
            async with self.db.session() as session:
                from sqlalchemy import or_, select

                result = await session.execute(
                    select(TradePlan).where(
                        or_(
                            TradePlan.entry_order_id == order_id,
                            TradePlan.exit_order_id == order_id,
                        )
                    )
                )
                plan = result.scalars().first()
            if plan is None:
                return
            is_entry = plan.entry_order_id == order_id
            avg = float(order.filled_avg_price or 0) if order.filled_avg_price else None
            log.info("trade update: plan %s %s event=%s avg=%s", plan.id, "entry" if is_entry else "exit", event, avg)

            if event in ("fill",):
                if is_entry:
                    await self._update_plan(plan.id, status="filled", fill_premium=avg)
                    if self.enforcer:
                        await self.enforcer.arm(plan.id)
                else:
                    realized = None
                    if avg is not None and plan.fill_premium is not None:
                        realized = round((avg - plan.fill_premium) * 100 * plan.qty, 2)
                    await self._update_plan(
                        plan.id, status="closed", exit_premium=avg, realized_pnl=realized
                    )
                    if self.enforcer:
                        self.enforcer.disarm(plan.id)
            elif event in ("canceled", "expired", "rejected"):
                if is_entry:
                    await self._update_plan(plan.id, status="cancelled", notes=f"entry {event}")
                    if self.enforcer:
                        self.enforcer.disarm(plan.id)
                else:
                    # Exit order failed - enforcer escalation will retry.
                    await self._update_plan(plan.id, exit_order_id=None)
        except Exception:
            log.exception("trade update handling failed")

    # -------------------------------------------------------------- exits

    async def submit_exit(self, plan: TradePlan, reason: str, limit_price: float | None) -> None:
        """Submit closing order (reverse all legs). limit None => market."""
        legs = plan.legs
        if len(legs) == 1:
            leg = legs[0]
            common = dict(
                symbol=leg["symbol"],
                qty=plan.qty * leg.get("ratio", 1),
                side=OrderSide.SELL if leg["side"] > 0 else OrderSide.BUY,
                position_intent=(
                    PositionIntent.SELL_TO_CLOSE if leg["side"] > 0 else PositionIntent.BUY_TO_CLOSE
                ),
                time_in_force=TimeInForce.DAY,
            )
            request = (
                MarketOrderRequest(**common)
                if limit_price is None
                else LimitOrderRequest(**common, limit_price=round_tick(limit_price))
            )
        else:
            mleg_legs = [
                OptionLegRequest(
                    symbol=leg["symbol"],
                    ratio_qty=leg.get("ratio", 1),
                    side=OrderSide.SELL if leg["side"] > 0 else OrderSide.BUY,
                    position_intent=(
                        PositionIntent.SELL_TO_CLOSE
                        if leg["side"] > 0
                        else PositionIntent.BUY_TO_CLOSE
                    ),
                )
                for leg in legs
            ]
            common = dict(
                order_class=OrderClass.MLEG,
                qty=plan.qty,
                time_in_force=TimeInForce.DAY,
                legs=mleg_legs,
            )
            request = (
                MarketOrderRequest(**common)
                if limit_price is None
                else LimitOrderRequest(**common, limit_price=round_tick(limit_price))
            )
        order = await self.alpaca.call(self.alpaca.trading.submit_order, request)
        await self._update_plan(
            plan.id, status="exiting", exit_order_id=str(order.id), exit_reason=reason
        )

    async def cancel_order(self, order_id: str) -> None:
        await self.alpaca.call(self.alpaca.trading.cancel_order_by_id, order_id)

    async def cancel_entry(self, plan: TradePlan) -> None:
        if plan.entry_order_id and plan.status == "submitted":
            try:
                await self.cancel_order(plan.entry_order_id)
            except Exception as exc:
                log.warning("cancel entry %s failed: %s", plan.entry_order_id, exc)

    async def order_status(self, order_id: str) -> str:
        order = await self.alpaca.call(self.alpaca.trading.get_order_by_id, order_id)
        return str(order.status.value if hasattr(order.status, "value") else order.status)
