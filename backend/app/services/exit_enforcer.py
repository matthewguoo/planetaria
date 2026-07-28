"""Exit enforcement: quote-driven TP/SL + hard time stops, rebuilt from the DB
on startup. Alpaca has no bracket/OCO for options, so this service IS the
bracket. One monitor task per open plan.

Escalation ladder for exits (illiquid-friendly):
  1. marketable limit at mid -/+ 2% buffer, wait 5s
  2. reprice at mid -/+ 6%, wait 5s
  3. market order
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.models.trade import OPEN_STATUSES, TradePlan, as_utc
from app.services.plan_fsm import MONITOR_STATES, PlanEvent
from app.services.trade_service import TradeService, position_mid_from_quotes, round_tick

MONITOR_STATUSES = {s.value for s in MONITOR_STATES}

log = logging.getLogger("app.enforcer")

ESCALATION = [(0.02, 5.0), (0.06, 5.0), (None, 0.0)]  # (buffer, wait_after)


class ExitEnforcer:
    def __init__(self, db, market, trade: TradeService):
        self.db = db
        self.market = market
        self.trade = trade
        trade.enforcer = self
        self._monitors: dict[str, asyncio.Task] = {}

    # ----------------------------------------------------------- lifecycle

    async def startup_reconcile(self) -> None:
        """Rebuild monitors from DB; reconcile vs Alpaca; flag orphans."""
        plans = await self.trade.risk.open_plans()
        log.info("reconciling %d open plans", len(plans))
        for plan in plans:
            try:
                if plan.status == "planned" and not plan.entry_order_id:
                    # Crashed between plan commit and order submit: no order
                    # ever reached the broker, so nothing to manage.
                    await self.trade.fsm.apply(
                        plan.id, PlanEvent.ENTRY_CANCELLED,
                        notes="orphaned planned row (no order submitted)",
                    )
                    continue
                # Refresh entry order status in case fills happened while down.
                if plan.status in ("submitted", "partially_filled") and plan.entry_order_id:
                    status = await self.trade.order_status(plan.entry_order_id)
                    if status == "filled":
                        order = await self.trade.alpaca.call(
                            self.trade.alpaca.trading.get_order_by_id, plan.entry_order_id
                        )
                        raw = float(order.filled_avg_price or 0) or None
                        avg = self.trade._fill_value(plan, raw, is_entry=True)
                        await self.trade.fsm.apply(
                            plan.id, PlanEvent.ENTRY_FILLED,
                            fill_premium=avg if avg is not None else plan.entry_limit,
                            filled_qty=int(float(order.filled_qty or plan.qty)),
                        )
                    elif status in ("canceled", "expired", "rejected"):
                        order = await self.trade.alpaca.call(
                            self.trade.alpaca.trading.get_order_by_id, plan.entry_order_id
                        )
                        filled_qty = int(float(order.filled_qty or 0))
                        if filled_qty > 0:
                            await self.trade.fsm.apply(
                                plan.id, PlanEvent.ENTRY_CANCELLED_PARTIAL,
                                qty=filled_qty, filled_qty=filled_qty,
                                notes=f"entry {status} after partial fill (offline)",
                            )
                        else:
                            await self.trade.fsm.apply(
                                plan.id, PlanEvent.ENTRY_CANCELLED,
                                notes=f"entry {status} while offline",
                            )
                            continue
                if plan.status == "exiting" and plan.exit_order_id:
                    status = await self.trade.order_status(plan.exit_order_id)
                    if status == "filled":
                        order = await self.trade.alpaca.call(
                            self.trade.alpaca.trading.get_order_by_id, plan.exit_order_id
                        )
                        raw = float(order.filled_avg_price or 0) or None
                        avg = self.trade._fill_value(plan, raw, is_entry=False)
                        realized = None
                        if avg is not None and plan.fill_premium is not None:
                            realized = round(
                                (avg - plan.fill_premium) * 100 * plan.effective_qty, 2
                            )
                        await self.trade.fsm.apply(
                            plan.id, PlanEvent.EXIT_FILLED,
                            exit_premium=avg, realized_pnl=realized,
                        )
                        continue
                    if status in ("canceled", "expired", "rejected"):
                        await self.trade.fsm.apply(
                            plan.id, PlanEvent.EXIT_ORDER_DEAD, exit_order_id=None
                        )
                await self.arm(plan.id)
            except Exception:
                log.exception("reconcile failed for plan %s", plan.id)

        # Orphan check: Alpaca option positions with no open plan.
        if self.trade.alpaca.configured:
            try:
                positions = await self.trade.alpaca.call(self.trade.alpaca.trading.get_all_positions)
                plan_symbols = {leg["symbol"] for p in await self.trade.risk.open_plans() for leg in p.legs}
                for pos in positions:
                    if str(getattr(pos, "asset_class", "")) .endswith("option") or len(pos.symbol) > 12:
                        if pos.symbol not in plan_symbols:
                            log.error(
                                "ORPHAN POSITION (no exit plan!): %s qty=%s - close it manually "
                                "or via flatten-all", pos.symbol, pos.qty,
                            )
            except Exception as exc:
                log.warning("orphan scan failed: %s", exc)

    async def shutdown(self) -> None:
        for task in self._monitors.values():
            task.cancel()
        self._monitors.clear()

    # ------------------------------------------------------------ monitors

    async def arm(self, plan_id: str) -> None:
        if plan_id in self._monitors:
            return
        self._monitors[plan_id] = asyncio.create_task(
            self._monitor(plan_id), name=f"monitor-{plan_id}"
        )

    def disarm(self, plan_id: str) -> None:
        task = self._monitors.pop(plan_id, None)
        if task:
            task.cancel()

    async def _monitor(self, plan_id: str) -> None:
        try:
            plan = await self.trade.get_plan(plan_id)
            symbols = [leg["symbol"] for leg in plan.legs]
            await self.market.subscribe_options(symbols)

            queue: asyncio.Queue = asyncio.Queue(maxsize=200)
            for sym in symbols:
                self.market.broadcast.subscribe(f"oquote:{sym}", queue)
            self.market.broadcast.subscribe("plans", queue)

            log.info("monitor armed: plan %s TP=%.2f SL=%.2f stop=%s",
                     plan.id, plan.tp_premium, plan.sl_premium, plan.time_stop_utc)
            try:
                while True:
                    plan = await self.trade.get_plan(plan_id)
                    if plan.status not in OPEN_STATUSES:
                        return
                    timeout = (as_utc(plan.time_stop_utc) - datetime.now(timezone.utc)).total_seconds()
                    if timeout <= 0 and plan.status in MONITOR_STATUSES:
                        log.warning("TIME STOP hit for plan %s", plan.id)
                        if plan.status == "partially_filled":
                            # Stop the rest from filling, then close what did.
                            await self.trade.cancel_entry(plan)
                        await self._execute_exit(plan_id, "time_stop")
                        return
                    if timeout <= 0 and plan.status == "submitted":
                        await self.trade.cancel_entry(plan)
                        return
                    if plan.status == "exiting" and not plan.exit_order_id:
                        # Exit order died (cancel/reject) - resubmit the ladder.
                        log.warning("plan %s exiting with no live order - resubmitting", plan.id)
                        await self._execute_exit(plan_id, plan.exit_reason or "manual")
                        return
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=min(max(timeout, 0.1), 15.0))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("t") != "oquote" or plan.status not in MONITOR_STATUSES:
                        continue
                    quotes = {s: self.market.latest_quote(s) for s in symbols}
                    mid = position_mid_from_quotes(plan.legs, quotes)
                    if mid is None:
                        continue
                    if mid >= plan.tp_premium:
                        log.info("TP hit for plan %s (mid %.2f)", plan.id, mid)
                        await self._execute_exit(plan_id, "tp")
                        return
                    if mid <= plan.sl_premium:
                        log.warning("SL hit for plan %s (mid %.2f)", plan.id, mid)
                        await self._execute_exit(plan_id, "sl")
                        return
            finally:
                for sym in symbols:
                    self.market.broadcast.unsubscribe(f"oquote:{sym}", queue)
                self.market.broadcast.unsubscribe("plans", queue)
                await self.market.unsubscribe_options(symbols)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("monitor crashed for plan %s - REARMING in 5s", plan_id)
            await asyncio.sleep(5)
            self._monitors.pop(plan_id, None)
            await self.arm(plan_id)
        finally:
            self._monitors.pop(plan_id, None)

    # ------------------------------------------------------------ exits

    async def _execute_exit(self, plan_id: str, reason: str) -> None:
        """Escalation ladder, then a verification loop: this must not return
        with the position alive and nobody watching it."""
        for buffer, wait in ESCALATION:
            plan = await self.trade.get_plan(plan_id)
            if plan.status in ("closed", "cancelled", "rejected"):
                return
            if plan.exit_order_id:
                try:
                    await self.trade.cancel_order(plan.exit_order_id)
                except Exception:
                    pass
            quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in plan.legs}
            mid = position_mid_from_quotes(plan.legs, quotes)
            if mid is None:
                mid = plan.sl_premium
            # Marketable = accept a WORSE position value: sign-agnostic shift
            # downward by |mid|*buffer (long: sell lower; short: buy back higher).
            limit = None if buffer is None else round_tick(mid - abs(mid) * buffer)
            try:
                await self.trade.submit_exit(plan, reason, limit)
            except Exception as exc:
                log.error("exit submit failed for %s (%s) - retrying: %s", plan_id, reason, exc)
                await asyncio.sleep(2)
                continue
            if wait:
                await asyncio.sleep(wait)
                plan = await self.trade.get_plan(plan_id)
                if plan.status == "closed":
                    return
        log.info("exit ladder exhausted for %s; verifying market order", plan_id)
        await self._verify_closed(plan_id, reason)

    async def _verify_closed(self, plan_id: str, reason: str) -> None:
        """Poll until the plan closes; if the final order dies, resubmit a
        market close. Bounded per-iteration, unbounded overall — the ladder's
        whole point is that a triggered exit always finishes."""
        for attempt in range(120):  # ~10 min of 5s polls before loud rearm
            await asyncio.sleep(5)
            plan = await self.trade.get_plan(plan_id)
            if plan.status in ("closed", "cancelled", "rejected"):
                return
            if plan.exit_order_id:
                try:
                    status = await self.trade.order_status(plan.exit_order_id)
                except Exception as exc:
                    log.warning("exit status poll failed for %s: %s", plan_id, exc)
                    continue
                if status == "filled":
                    continue  # trade-update handler will close the plan
                if status in ("canceled", "expired", "rejected"):
                    await self.trade.fsm.apply(
                        plan_id, PlanEvent.EXIT_ORDER_DEAD, exit_order_id=None
                    )
            if not (await self.trade.get_plan(plan_id)).exit_order_id:
                log.warning("exit order dead for %s - resubmitting market close", plan_id)
                try:
                    await self.trade.submit_exit(plan, reason, None)
                except Exception as exc:
                    log.error("market close resubmit failed for %s: %s", plan_id, exc)
        log.error("plan %s STILL not closed after verification window - rearming monitor", plan_id)
        self._monitors.pop(plan_id, None)
        await self.arm(plan_id)

    # ---------------------------------------------------- manual actions

    async def manual_close(self, plan_id: str) -> None:
        plan = await self.trade.get_plan(plan_id)
        if plan.status == "submitted":
            await self.trade.cancel_entry(plan)
            return
        if plan.status in ("partially_filled", "filled", "exiting"):
            if plan.status == "partially_filled":
                await self.trade.cancel_entry(plan)
            self.disarm(plan_id)
            await self._execute_exit(plan_id, "manual")
            # Re-arm a watchdog in case the exit order dies.
            await self.arm(plan_id)

    async def flatten_all(self) -> int:
        plans = await self.trade.risk.open_plans()
        for plan in plans:
            try:
                await self.manual_close(plan.id)
            except Exception:
                log.exception("flatten failed for %s", plan.id)
        return len(plans)

    async def tighten_exits(self, plan_id: str, tp: float | None, sl: float | None,
                            time_stop_utc: datetime | None) -> TradePlan:
        """Exits may only tighten: TP down, SL up, time stop earlier."""
        plan = await self.trade.get_plan(plan_id)
        if plan.status not in OPEN_STATUSES:
            raise ValueError("plan is not open")
        fields: dict = {}
        if tp is not None:
            if tp >= plan.tp_premium:
                raise ValueError("TP may only move down (tighten)")
            if tp <= plan.sl_premium:
                raise ValueError("TP must stay above SL")
            fields["tp_premium"] = tp
        if sl is not None:
            if sl <= plan.sl_premium:
                raise ValueError("SL may only move up (tighten)")
            if sl >= (fields.get("tp_premium", plan.tp_premium)):
                raise ValueError("SL must stay below TP")
            fields["sl_premium"] = sl
        if time_stop_utc is not None:
            if time_stop_utc.tzinfo is None:
                time_stop_utc = time_stop_utc.replace(tzinfo=timezone.utc)
            if time_stop_utc >= as_utc(plan.time_stop_utc):
                raise ValueError("time stop may only move earlier")
            fields["time_stop_utc"] = time_stop_utc
        if not fields:
            raise ValueError("nothing to change")
        return await self.trade._update_plan(plan_id, **fields)
