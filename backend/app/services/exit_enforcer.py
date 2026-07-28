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
from uuid import uuid4

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
        self._reconcile_lock = asyncio.Lock()
        # Idempotency keys of exit submits that ERRORED (per plan): any of
        # them may have landed at the broker anyway ("ghost" order — never
        # recorded on the plan). Tracked until resolved or the plan closes.
        self._ghost_keys: dict[str, list[str]] = {}
        # Timing knobs (instance-level so pressure tests can compress them).
        self.escalation = list(ESCALATION)
        self.verify_poll_s = 5.0
        self.verify_attempts = 120  # ~10 min of polls before loud rearm
        self.rearm_delay_s = 5.0
        self.reconcile_interval_s = 45.0

    # ----------------------------------------------------------- lifecycle

    async def startup_reconcile(self) -> None:
        """Rebuild monitors from DB; reconcile vs Alpaca; flag orphans."""
        await self.reconcile_once(orphan_scan=True)

    async def reconcile_loop(self) -> None:
        """Periodic REST truth-sync. The TradingStream is the fast path for
        fills, but streams drop; without this loop a fill that lands during a
        stream gap would leave a live position unmanaged forever. Also
        self-heals: any open plan without a monitor gets re-armed."""
        while True:
            await asyncio.sleep(self.reconcile_interval_s)
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("periodic reconcile failed")

    async def reconcile_once(self, orphan_scan: bool = False) -> None:
        async with self._reconcile_lock:
            plans = await self.trade.risk.open_plans()
            if orphan_scan:
                log.info("reconciling %d open plans", len(plans))
            for plan in plans:
                try:
                    await self._reconcile_plan(plan)
                except Exception:
                    log.exception("reconcile failed for plan %s", plan.id)

            # Orphan check: Alpaca option positions with no open plan.
            if orphan_scan and self.trade.alpaca.configured:
                try:
                    positions = await self.trade.alpaca.call(
                        self.trade.alpaca.trading.get_all_positions, retries=1
                    )
                    plan_symbols = {
                        leg["symbol"]
                        for p in await self.trade.risk.open_plans()
                        for leg in p.legs
                    }
                    for pos in positions:
                        if str(getattr(pos, "asset_class", "")).endswith("option") or len(pos.symbol) > 12:
                            if pos.symbol not in plan_symbols:
                                log.error(
                                    "ORPHAN POSITION (no exit plan!): %s qty=%s - close it manually "
                                    "or via flatten-all", pos.symbol, pos.qty,
                                )
                except Exception as exc:
                    log.warning("orphan scan failed: %s", exc)

    async def _reconcile_plan(self, plan: TradePlan) -> None:
        """Sync one plan's order state from broker REST, then (re-)arm."""
        if plan.status == "planned" and not plan.entry_order_id:
            # Crashed between plan commit and order submit: no order
            # ever reached the broker, so nothing to manage.
            await self.trade.fsm.apply(
                plan.id, PlanEvent.ENTRY_CANCELLED,
                notes="orphaned planned row (no order submitted)",
            )
            return
        if not self.trade.alpaca.configured:
            await self.arm(plan.id)
            return
        # Refresh entry order status in case fills happened while down.
        if plan.status in ("submitted", "partially_filled") and plan.entry_order_id:
            status = await self.trade.order_status(plan.entry_order_id)
            if status == "filled":
                order = await self.trade.alpaca.call(
                    self.trade.alpaca.trading.get_order_by_id, plan.entry_order_id, retries=1
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
                    self.trade.alpaca.trading.get_order_by_id, plan.entry_order_id, retries=1
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
                    return
        if plan.status == "exiting" and plan.exit_order_id:
            observed_order = plan.exit_order_id
            status = await self.trade.order_status(observed_order)
            if status == "filled":
                order = await self.trade.alpaca.call(
                    self.trade.alpaca.trading.get_order_by_id, plan.exit_order_id, retries=1
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
                    guard={"exit_order_id": observed_order},
                    exit_premium=avg, realized_pnl=realized,
                )
                return
            if status in ("canceled", "expired", "rejected"):
                # Guard on the order this verdict is ABOUT: the escalation
                # ladder may have replaced it since we read the plan, and a
                # stale "dead" must not wipe the live order's id.
                await self.trade.fsm.apply(
                    plan.id, PlanEvent.EXIT_ORDER_DEAD,
                    guard={"exit_order_id": observed_order},
                    exit_order_id=None,
                )
        await self.arm(plan.id)

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

    # ------------------------------------------------------- ghost orders

    async def _handle_ghost(self, plan: TradePlan) -> bool:
        """Prior exit submits that errored ambiguously may still have landed
        at the broker ("ghost" orders: never recorded on the plan). Check each
        unresolved key: cancel a live ghost, adopt-and-close a filled one.
        Returns True when a ghost FILLED — the position is already closed, so
        the caller must NOT submit another close on top of it."""
        for key in list(self._ghost_keys.get(plan.id, ())):
            ghost = await self.trade._order_by_client_id(f"{plan.id}-x{key}")
            if ghost is None:
                continue  # never landed (so far) — keep watching this key
            if str(getattr(ghost, "id", "")) == (plan.exit_order_id or ""):
                self._resolve_ghost_key(plan.id, key)
                continue
            status = str(getattr(ghost, "status", "")).lower().split(".")[-1]
            if "fill" in status and "partial" not in status:
                log.warning("ghost exit order %s for plan %s FILLED - adopting it", ghost.id, plan.id)
                await self.trade.fsm.apply(
                    plan.id, PlanEvent.EXIT_SUBMITTED,
                    exit_order_id=str(ghost.id),
                    exit_reason=plan.exit_reason or "manual",
                )
                self._ghost_keys.pop(plan.id, None)
                try:
                    await self._reconcile_plan(await self.trade.get_plan(plan.id))
                except Exception:
                    log.exception("ghost adoption reconcile failed for %s", plan.id)
                return True
            if any(s in status for s in ("cancel", "expired", "rejected")):
                self._resolve_ghost_key(plan.id, key)
                continue
            log.warning("cancelling ghost exit order %s for plan %s", ghost.id, plan.id)
            try:
                await self.trade.cancel_order(str(ghost.id))
                self._resolve_ghost_key(plan.id, key)
            except Exception:
                pass
        return False

    async def _position_gone(self, plan: TradePlan) -> bool:
        """True when the broker reports NO remaining position in any of the
        plan's legs. Conservative: any error keeps the plan alive."""
        try:
            positions = {p["symbol"] for p in await self.trade.broker_positions(max_age_s=0.5)}
        except Exception as exc:
            log.warning("position check failed for %s: %s", plan.id, exc)
            return False
        return not ({leg["symbol"] for leg in plan.legs} & positions)

    def _resolve_ghost_key(self, plan_id: str, key: str) -> None:
        keys = self._ghost_keys.get(plan_id)
        if keys and key in keys:
            keys.remove(key)
        if not keys:
            self._ghost_keys.pop(plan_id, None)

    async def _sweep_ghosts_on_close(self, plan_id: str) -> None:
        """The plan is closed; cancel any unresolved ghost that is live so no
        stray closing order can fill into a fresh (reversed) position."""
        keys = self._ghost_keys.pop(plan_id, None)
        if not keys:
            return
        for key in keys:
            try:
                ghost = await self.trade._order_by_client_id(f"{plan_id}-x{key}")
                if ghost is None:
                    continue
                status = str(getattr(ghost, "status", "")).lower().split(".")[-1]
                if not any(s in status for s in ("fill", "cancel", "expired", "rejected")):
                    log.warning("sweeping live ghost order %s of closed plan %s", ghost.id, plan_id)
                    await self.trade.cancel_order(str(ghost.id))
            except Exception:
                log.exception("ghost sweep failed for plan %s key %s", plan_id, key)

    async def _monitor(self, plan_id: str) -> None:
        rearm = False
        try:
            plan = await self.trade.get_plan(plan_id)
            entry_ttl_min = float((await self.trade.risk.get_settings())["entry_ttl_min"])
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
                    # Entry TTL: an unfilled limit sitting past its shelf life
                    # is a stale price chasing the market — cancel, don't chase.
                    if plan.status == "submitted" and plan.created_at is not None:
                        age_min = (
                            datetime.now(timezone.utc) - as_utc(plan.created_at)
                        ).total_seconds() / 60
                        if age_min > entry_ttl_min:
                            log.warning(
                                "entry TTL (%.0fmin) hit for plan %s - cancelling unfilled entry",
                                entry_ttl_min, plan.id,
                            )
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
            log.exception("monitor crashed for plan %s - REARMING in %.0fs", plan_id, self.rearm_delay_s)
            rearm = True
        finally:
            # Deregister SELF first; the replacement (if any) is armed after,
            # so it can't be clobbered by this cleanup.
            self._monitors.pop(plan_id, None)
        if rearm:
            await asyncio.sleep(self.rearm_delay_s)
            await self.arm(plan_id)

    # ------------------------------------------------------------ exits

    async def _execute_exit(self, plan_id: str, reason: str) -> None:
        """Escalation ladder, then a verification loop: this must not return
        with the position alive and nobody watching it.

        Each rung submits under a fresh idempotency key (unique per
        invocation, stable within the submit) so an ambiguous broker failure
        recovers the SAME order instead of stacking a second close."""
        token = uuid4().hex[:6]
        for rung, (buffer, wait) in enumerate(self.escalation):
            plan = await self.trade.get_plan(plan_id)
            if plan.status in ("closed", "cancelled", "rejected"):
                await self._sweep_ghosts_on_close(plan_id)
                return
            if plan.exit_order_id:
                try:
                    await self.trade.cancel_order(plan.exit_order_id)
                except Exception:
                    pass
            if await self._handle_ghost(plan):
                return  # a ghost already filled; adopted and closing
            quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in plan.legs}
            mid = position_mid_from_quotes(plan.legs, quotes)
            if mid is None:
                mid = plan.sl_premium
            # Marketable = accept a WORSE position value: sign-agnostic shift
            # downward by |mid|*buffer (long: sell lower; short: buy back higher).
            limit = None if buffer is None else round_tick(mid - abs(mid) * buffer)
            key = f"{token}r{rung}"
            self._ghost_keys.setdefault(plan_id, []).append(key)
            try:
                await self.trade.submit_exit(plan, reason, limit, attempt_key=key)
                self._resolve_ghost_key(plan_id, key)  # recorded on the plan
            except Exception as exc:
                log.error("exit submit failed for %s (%s) - retrying: %s", plan_id, reason, exc)
                await asyncio.sleep(min(2.0, self.verify_poll_s))
                continue
            if wait:
                await asyncio.sleep(wait)
                plan = await self.trade.get_plan(plan_id)
                if plan.status == "closed":
                    await self._sweep_ghosts_on_close(plan_id)
                    return
        log.info("exit ladder exhausted for %s; verifying market order", plan_id)
        await self._verify_closed(plan_id, reason)

    async def _verify_closed(self, plan_id: str, reason: str) -> None:
        """Poll until the plan closes; if the final order dies, resubmit a
        market close. Bounded per-iteration, unbounded overall — the ladder's
        whole point is that a triggered exit always finishes."""
        resubmit_failures = 0
        for attempt in range(self.verify_attempts):
            await asyncio.sleep(self.verify_poll_s)
            plan = await self.trade.get_plan(plan_id)
            if plan.status in ("closed", "cancelled", "rejected"):
                await self._sweep_ghosts_on_close(plan_id)
                return
            if plan.exit_order_id:
                observed_order = plan.exit_order_id
                try:
                    status = await self.trade.order_status(observed_order)
                except Exception as exc:
                    log.warning("exit status poll failed for %s: %s", plan_id, exc)
                    continue
                if status == "filled":
                    # Don't depend on the TradingStream to learn this — close
                    # the plan from REST truth right here.
                    try:
                        await self._reconcile_plan(await self.trade.get_plan(plan_id))
                    except Exception as exc:
                        log.warning("close-from-REST failed for %s: %s", plan_id, exc)
                    continue
                if status in ("canceled", "expired", "rejected"):
                    await self.trade.fsm.apply(
                        plan_id, PlanEvent.EXIT_ORDER_DEAD,
                        guard={"exit_order_id": observed_order},
                        exit_order_id=None,
                    )
            if not (await self.trade.get_plan(plan_id)).exit_order_id:
                if await self._handle_ghost(await self.trade.get_plan(plan_id)):
                    continue
                log.warning("exit order dead for %s - resubmitting market close", plan_id)
                key = f"{uuid4().hex[:6]}v"
                self._ghost_keys.setdefault(plan_id, []).append(key)
                try:
                    await self.trade.submit_exit(plan, reason, None, attempt_key=key)
                    self._resolve_ghost_key(plan_id, key)
                    resubmit_failures = 0
                except Exception as exc:
                    log.error("market close resubmit failed for %s: %s", plan_id, exc)
                    resubmit_failures += 1
                    # Repeated rejections usually mean the position no longer
                    # exists at the broker (closed by an order we lost track
                    # of, expiry liquidation, or a manual close in their UI).
                    # Verify against position truth and force-close the plan
                    # instead of resubmitting forever.
                    if resubmit_failures >= 3 and await self._position_gone(plan):
                        log.error(
                            "position gone at broker for %s - FORCE_CLOSED", plan_id
                        )
                        await self.trade.fsm.apply(
                            plan_id, PlanEvent.FORCE_CLOSED,
                            notes="position vanished at broker during exit",
                        )
                        await self._sweep_ghosts_on_close(plan_id)
                        return
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
