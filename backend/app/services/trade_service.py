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
    """Round to a cent, preserving sign (negative = net credit for MLEG
    orders per Alpaca semantics). Never returns exactly 0."""
    r = round(round(x / TICK) * TICK, 2)
    if r == 0.0:
        r = TICK if x >= 0 else -TICK
    return r


def position_mid_from_quotes(legs: list[dict], quotes: dict[str, dict]) -> float | None:
    """Side-weighted net mid per share; None if any leg quote is missing."""
    total = 0.0
    for leg in legs:
        quote = quotes.get(leg["symbol"])
        if not quote or not (quote.get("bid") or quote.get("ask")):
            return None
        total += leg["side"] * leg.get("ratio", 1) * quote["mid"]
    return total


def parse_occ_symbol(symbol: str) -> dict | None:
    """OCC option symbol -> {underlying, expiry, right, strike}; None if not OCC."""
    try:
        if len(symbol) < 16:
            return None
        strike = int(symbol[-8:]) / 1000.0
        right = symbol[-9].upper()
        if right not in ("C", "P"):
            return None
        yymmdd = symbol[-15:-9]
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        underlying = symbol[:-15].strip()
        if not underlying:
            return None
        return {"underlying": underlying, "expiry": expiry, "right": right, "strike": strike}
    except (ValueError, IndexError):
        return None


class TradeService:
    def __init__(self, db, alpaca: AlpacaService, market, risk):
        self.db = db
        self.alpaca = alpaca
        self.market = market
        self.risk = risk
        self.enforcer = None  # set by ExitEnforcer at startup (circular)
        self._account_cache: tuple[float, dict] | None = None
        self._positions_cache: tuple[float, list[dict]] | None = None

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

    # ---------------------------------------------------- broker positions

    async def broker_positions(self, max_age_s: float = 5.0) -> list[dict]:
        """Live positions straight from the Alpaca account, normalized.
        These are the ground truth; TradePlans are our management layer on
        top. Cached briefly to keep the positions poll off the rate budget."""
        import time as _time

        if self._positions_cache and _time.monotonic() - self._positions_cache[0] < max_age_s:
            return self._positions_cache[1]
        if not self.alpaca.configured:
            return []
        positions = await self.alpaca.call(self.alpaca.trading.get_all_positions)
        out: list[dict] = []
        for pos in positions:
            occ = parse_occ_symbol(pos.symbol)
            asset_class = str(getattr(pos, "asset_class", "") or "").lower()
            # OCC parse is the reliable signal; asset_class strings vary.
            is_option = occ is not None and ("option" in asset_class or not asset_class)
            qty = float(pos.qty)

            def _f(name: str) -> float | None:
                value = getattr(pos, name, None)
                return float(value) if value is not None else None

            out.append(
                {
                    "symbol": pos.symbol,
                    "qty": qty,
                    "side": 1 if qty > 0 else -1,
                    "asset_class": "option" if is_option else "stock",
                    "avg_entry_price": _f("avg_entry_price") or 0.0,
                    "current_price": _f("current_price"),
                    "market_value": _f("market_value"),
                    "unrealized_pl": _f("unrealized_pl"),
                    "occ": occ if is_option else None,
                }
            )
        self._positions_cache = (_time.monotonic(), out)
        return out

    async def untracked_positions(self) -> list[dict]:
        """Broker positions not covered by any open plan's legs."""
        plans = await self.risk.open_plans()
        covered = {leg["symbol"] for plan in plans for leg in plan.legs}
        return [p for p in await self.broker_positions() if p["symbol"] not in covered]

    async def adopt_positions(
        self,
        symbols: list[str],
        tp_pct: float,
        sl_pct: float,
        time_stop_utc: datetime,
    ) -> list[dict]:
        """Fold untracked broker option positions into managed TradePlans —
        one multi-leg plan per underlying (chunked at 4 legs, the MLEG close
        limit) — so the enforcer runs TP/SL/time exits on them."""
        from math import gcd

        untracked = {p["symbol"]: p for p in await self.untracked_positions()}
        chosen = [untracked[s] for s in symbols if s in untracked and untracked[s]["occ"]]
        if not chosen:
            raise ValueError("no adoptable untracked option positions in selection")

        by_underlying: dict[str, list[dict]] = {}
        for p in chosen:
            by_underlying.setdefault(p["occ"]["underlying"], []).append(p)

        adopted: list[dict] = []
        for underlying, group in sorted(by_underlying.items()):
            group.sort(key=lambda p: (p["occ"]["expiry"], p["occ"]["strike"]))
            for chunk_start in range(0, len(group), 4):
                chunk = group[chunk_start : chunk_start + 4]
                qtys = [max(int(abs(p["qty"])), 1) for p in chunk]
                sets = qtys[0]
                for q in qtys[1:]:
                    sets = gcd(sets, q)
                sets = max(sets, 1)

                legs, entry = [], 0.0
                for p, q in zip(chunk, qtys):
                    ratio = max(q // sets, 1)
                    legs.append(
                        {
                            "symbol": p["symbol"],
                            "right": p["occ"]["right"],
                            "strike": p["occ"]["strike"],
                            "expiry": p["occ"]["expiry"],
                            "side": p["side"],
                            "ratio": ratio,
                            "entry": p["avg_entry_price"],
                            "iv": 0.0,
                        }
                    )
                    entry += p["side"] * ratio * p["avg_entry_price"]
                if abs(entry) < TICK:
                    entry = TICK  # zero-cost basis: manage on absolute premium

                plan = TradePlan(
                    underlying=underlying[:12],
                    strategy="adopted",
                    legs=legs,
                    qty=sets,
                    entry_limit=round(entry, 4),
                    tp_premium=round(entry + abs(entry) * tp_pct, 4),
                    sl_premium=round(entry - abs(entry) * sl_pct, 4),
                    time_stop_utc=time_stop_utc,
                    status="filled",
                    fill_premium=round(entry, 4),
                    notes="adopted from broker positions",
                )
                async with self.db.session() as session:
                    session.add(plan)
                    await session.commit()
                    await session.refresh(plan)
                self.market.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
                if self.enforcer:
                    await self.enforcer.arm(plan.id)
                log.info("adopted %d broker legs into plan %s (%s)", len(legs), plan.id, underlying)
                adopted.append(plan.to_dict())
        return adopted

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
        if abs(entry_limit) < TICK:
            raise ValueError("net premium must be at least one tick")
        if not (sl < entry_limit < tp):
            raise ValueError(
                f"exits must bracket entry: SL {sl} < entry {entry_limit} < TP {tp}"
            )

        account = await self.get_account()
        # Capital consumed: debit paid for longs; margin (structural worst case)
        # for credit structures; stop-risk proxy when risk is undefined.
        max_loss = (entry_limit - sl) * 100 * qty
        if entry_limit > 0:
            entry_cost = entry_limit * 100 * qty
        else:
            from app.services.options_math import Leg, structural_max_loss

            structural = structural_max_loss([Leg.from_dict(leg) for leg in legs])
            entry_cost = (structural * 100 * qty) if structural is not None else max_loss * 3
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
            # Single-leg limit prices are unsigned premiums; side carries
            # direction (a short entry's net credit arrives as negative
            # entry_limit in position terms).
            leg = legs[0]
            request = LimitOrderRequest(
                symbol=leg["symbol"],
                qty=plan.qty * leg.get("ratio", 1),
                side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                position_intent=(
                    PositionIntent.BUY_TO_OPEN if leg["side"] > 0 else PositionIntent.SELL_TO_OPEN
                ),
                limit_price=abs(round_tick(plan.entry_limit)),
                time_in_force=TimeInForce.DAY,
            )
        else:
            # MLEG limit prices are signed: positive = net debit, negative =
            # net credit. entry_limit already uses that convention.
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

    def _fill_value(self, plan: TradePlan, avg: float | None, *, is_entry: bool) -> float | None:
        """Normalize a broker fill price into position-value terms (signed,
        same axis as entry_limit/TP/SL).

        Single-leg: broker prices are unsigned premiums; leg side supplies the
        sign for entry AND exit (a short's buy-to-close debit is negative
        position value). MLEG: prices are signed net debit/credit in the
        ORDER's orientation — entries share the position orientation, exits
        are reversed so the sign flips.
        """
        if avg is None:
            return None
        if len(plan.legs) == 1:
            return plan.legs[0]["side"] * abs(avg)
        if is_entry:
            # Defensive: a credit entry cannot fill at a debit; force the
            # entry_limit's sign if the feed reports magnitude only.
            import math as _math

            return _math.copysign(abs(avg), plan.entry_limit) if avg > 0 else avg
        return -avg

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
            raw_avg = float(order.filled_avg_price) if order.filled_avg_price else None
            avg = self._fill_value(plan, raw_avg, is_entry=is_entry)
            filled_qty = int(float(order.filled_qty or 0))
            log.info(
                "trade update: plan %s %s event=%s avg=%s filled=%d",
                plan.id, "entry" if is_entry else "exit", event, avg, filled_qty,
            )

            if event == "fill":
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
            elif event == "partial_fill":
                # Record the running average so a later cancel still knows the
                # real cost basis of whatever DID fill.
                if is_entry and avg is not None:
                    await self._update_plan(plan.id, fill_premium=avg)
            elif event in ("canceled", "expired", "rejected"):
                if is_entry:
                    if filled_qty > 0:
                        # Entry died with a partial position on the books:
                        # shrink the plan to what filled and keep managing it.
                        log.warning(
                            "plan %s entry %s after partial fill (%d/%d) - managing partial",
                            plan.id, event, filled_qty, plan.qty,
                        )
                        await self._update_plan(
                            plan.id, status="filled", qty=filled_qty,
                            fill_premium=avg if avg is not None else plan.fill_premium,
                            notes=f"entry {event} after partial fill {filled_qty}",
                        )
                        if self.enforcer:
                            await self.enforcer.arm(plan.id)
                    else:
                        await self._update_plan(plan.id, status="cancelled", notes=f"entry {event}")
                        if self.enforcer:
                            self.enforcer.disarm(plan.id)
                else:
                    # Exit order failed - monitor/escalation will resubmit.
                    await self._update_plan(plan.id, exit_order_id=None)
        except Exception:
            log.exception("trade update handling failed")

    # -------------------------------------------------------------- exits

    async def submit_exit(self, plan: TradePlan, reason: str, limit_price: float | None) -> None:
        """Submit closing order (reverse all legs). limit None => market.

        limit_price is in POSITION-VALUE terms (signed, same axis as
        entry/TP/SL). The closing order has every leg reversed, so its
        submitted limit is the NEGATION: closing a debit position collects a
        credit (negative MLEG limit), closing a credit position pays a debit
        (positive MLEG limit). Single-leg orders take the unsigned premium.
        """
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
                else LimitOrderRequest(**common, limit_price=abs(round_tick(limit_price)))
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
                else LimitOrderRequest(**common, limit_price=round_tick(-limit_price))
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
