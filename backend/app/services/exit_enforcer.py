"""Exit enforcement: quote-driven TP/SL + hard time stops, rebuilt from the DB
on startup. Alpaca has no bracket/OCO for options, so this service IS the
bracket. One monitor task per open plan.

Escalation ladder for exits (illiquid-friendly):
  1. marketable limit at mid -/+ 2% buffer, wait 5s
  2. reprice at mid -/+ 6%, wait 5s
  3. market order

The ladder only runs while the market is OPEN. An exit triggered while the
market is closed (time stop firing after a machine-sleep wake, manual close
at night) PARKS instead: one resting limit order that queues for the next
session, no cancel/replace churn, no market orders the broker would reject.
At the next open the monitor resumes the real ladder against live quotes.
(Incident 2026-07-30: the pre-clock ladder churned ~2 cancel/replace orders
per minute against a closed market all night without ever closing.)
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.models.trade import OPEN_STATUSES, TradePlan, as_utc
from app.services.market_clock import MarketClock, equity_session
from app.services.plan_fsm import MONITOR_STATES, PlanEvent
from app.services.trade_service import TradeService, position_mid_from_quotes, round_tick

MONITOR_STATUSES = {s.value for s in MONITOR_STATES}

log = logging.getLogger("app.enforcer")

ESCALATION = [(0.02, 5.0), (0.06, 5.0), (None, 0.0)]  # (buffer, wait_after)
# Equity ladder outside RTH: the market rung is FORBIDDEN (verified
# 2026-08-04: after-hours market orders are silently queued for the next
# open, not rejected) — substitute this very aggressive marketable limit.
EQUITY_AFTER_HOURS_MARKET_BUFFER = 0.10

# SL trigger stack (see fair_value.py for the estimator): TP/SL act on a
# Kalman fair value (microprice observations weighted by quote quality,
# theo-drift prediction between quotes) — a wide junk print CANNOT move
# the trigger price, so it cannot shake the position out. On top of the
# estimator: a breach must persist sl_confirm_s (dwell) — unless it is
# DEEP (>= SL_DEEP_FRAC of the TP-SL span past the stop), where blowup
# prevention beats shakeout prevention and it fires immediately. A raw
# microprice deep past the stop also fires immediately when the quote is
# QUALITY (half-spread <= SL_QUALITY_HS_FRAC of the span): a tight
# two-sided market printing a crash is real, don't wait for the filter.
# Hysteresis stops dwell-clock churn exactly at the line.
SL_DEEP_FRAC = 0.25
SL_HYSTERESIS_FRAC = 0.02
SL_QUALITY_HS_FRAC = 0.10
# Process noise: the position's fair value can plausibly drift this
# fraction of the bracket span per second (0-DTE realistic); measurement
# trust then follows from each quote's spread.
KF_PROC_FRAC = 0.05


def model_position_value(plan: TradePlan, spot: float, now_ms: float) -> float | None:
    """BSM position value from the plan's stored leg IVs at a given spot —
    the TRIGGER used on underlying ticks (stock quotes stream constantly;
    option quotes may not). Execution decisions still use real option mids;
    this only decides when to force-refresh them."""
    from app.services.options_math import (
        TRADING_HOURS_PER_YEAR,
        bs_price,
        trading_hours_to_expiry,
    )

    if spot <= 0:
        return None
    total = 0.0
    for leg in plan.legs:
        iv = float(leg.get("iv") or 0) or 0.25
        tau = trading_hours_to_expiry(leg["expiry"], now_ms) / TRADING_HOURS_PER_YEAR
        total += (
            leg["side"]
            * leg.get("ratio", 1)
            * bs_price(spot, float(leg["strike"]), max(tau, 0.0), iv, leg["right"])
        )
    return total


class ExitEnforcer:
    def __init__(self, db, market, trade: TradeService, clock: MarketClock | None = None):
        self.db = db
        self.market = market
        self.trade = trade
        trade.enforcer = self
        self.clock = clock or MarketClock(trade.alpaca)
        # Plans whose exit is PARKED: a single closing limit resting against a
        # closed market, waiting for the next open. The monitor resumes the
        # real escalation ladder when the clock flips open. Repopulated after
        # a restart by reconcile (exiting + live order + market closed).
        self._parked: set[str] = set()
        self._monitors: dict[str, asyncio.Task] = {}
        self._reconcile_lock = asyncio.Lock()
        # Idempotency keys of exit submits that ERRORED (per plan): any of
        # them may have landed at the broker anyway ("ghost" order — never
        # recorded on the plan). Tracked until resolved or the plan closes.
        self._ghost_keys: dict[str, list[str]] = {}
        # One exit ladder per plan at a time: a manual close, a monitor
        # trigger, and flatten-all must serialize, not interleave orders.
        self._exit_locks: dict[str, asyncio.Lock] = {}
        self.last_reconcile_ts: float | None = None
        # Monitors are quote-OR-poll driven: when the option stream is quiet
        # (illiquid wings, after hours), each monitor polls REST at this
        # cadence instead of waiting forever for a tick that never comes.
        # Near a threshold the cadence tightens to quote_poll_near_s, and
        # underlying stock ticks trigger immediate model-value checks — the
        # far-poll floor is a backstop, not the reaction time.
        self.quote_poll_s = 15.0
        self.quote_poll_near_s = 1.0
        # Rest the TP at the broker as a live limit order (zero-latency fills
        # that survive engine downtime). SL/time stay software-enforced.
        self.resting_tp = True
        # plan_id -> "ok" | "no-mid: SYM,..." — surfaced in /api/system/state
        # so a monitor that CANNOT evaluate TP/SL is visible, never silent.
        self.monitor_health: dict[str, str] = {}
        # Timing knobs (instance-level so pressure tests can compress them).
        self.escalation = list(ESCALATION)
        self.verify_poll_s = 5.0
        self.verify_attempts = 120  # ~10 min of polls before loud rearm
        self.rearm_delay_s = 5.0
        self.reconcile_interval_s = 45.0
        # Defense-in-depth for the TIME STOP (the one exit with no broker-side
        # backstop): each monitor loop stamps a heartbeat; reconcile restarts
        # monitors that stopped beating (wedged await), and fires an overdue
        # time stop DIRECTLY when the monitor failed to. The monitor normally
        # wakes at least every quote_poll_s, so a stop more than
        # time_stop_grace_s overdue means enforcement is broken.
        self.monitor_beat: dict[str, float] = {}
        self.monitor_wedge_s = 90.0
        self.time_stop_grace_s = 30.0
        self._backstop_tasks: set[asyncio.Task] = set()

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
            self.last_reconcile_ts = time.time()
            plans = await self.trade.risk.open_plans()
            if orphan_scan:
                log.info("reconciling %d open plans", len(plans))
            for plan in plans:
                # Wedged-monitor watchdog: a monitor task that exists but has
                # not completed a loop iteration recently is stuck inside an
                # await — it will never fire TP/SL/time stop. Kill + rearm.
                beat = self.monitor_beat.get(plan.id)
                if (
                    plan.id in self._monitors
                    and beat is not None
                    and time.monotonic() - beat > self.monitor_wedge_s
                ):
                    log.error(
                        "monitor for plan %s WEDGED (no heartbeat for %.0fs) - restarting",
                        plan.id, time.monotonic() - beat,
                    )
                    self.disarm(plan.id)
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

            # Startup-only: backfill closed plans that carry no exit data
            # (external liquidation landed while the engine was down).
            if orphan_scan and self.trade.alpaca.configured:
                try:
                    await self._repair_blank_exits()
                except Exception:
                    log.exception("blank-exit repair scan failed")

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
        # TIME-STOP BACKSTOP, independent of the monitor task: an overdue stop
        # on a held position means the monitor failed (wedged, crashed loop,
        # engine just restarted after downtime) — fire the exit ladder NOW.
        # Runs as its own task so a slow escalation can't stall reconcile.
        if plan.status in ("filled", "partially_filled") and plan.time_stop_utc is not None:
            overdue_s = (
                datetime.now(timezone.utc) - as_utc(plan.time_stop_utc)
            ).total_seconds()
            if overdue_s > self.time_stop_grace_s:
                log.error(
                    "plan %s is %.0fs past its time stop with no exit - "
                    "reconcile backstop firing time_stop", plan.id, overdue_s,
                )
                if plan.status == "partially_filled" and self.trade.alpaca.configured:
                    try:
                        await self.trade.cancel_entry(plan)
                    except Exception:
                        log.exception("backstop entry cancel failed for %s", plan.id)
                task = asyncio.create_task(
                    self._execute_exit(plan.id, "time_stop"),
                    name=f"backstop-exit-{plan.id}",
                )
                self._backstop_tasks.add(task)
                task.add_done_callback(self._backstop_tasks.discard)
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
        # Manual/external liquidation: a held position that VANISHED at the
        # broker without any exit order of ours means someone closed it out
        # from under the engine (broker UI, expiry). Detect it here directly
        # instead of waiting for an exit submit to bounce three times, and
        # capture the real closing fills for the trade record. The updated_at
        # age gate avoids racing the broker's position-propagation right
        # after an entry fill.
        if (
            plan.status in ("filled", "partially_filled")
            and not plan.exit_order_id
            and plan.updated_at is not None
            and (datetime.now(timezone.utc) - as_utc(plan.updated_at)).total_seconds() > 90
            and await self._position_gone(plan)
        ):
            log.error(
                "plan %s: position gone at broker with no exit order - "
                "external liquidation, capturing fills", plan.id,
            )
            await self._force_close_with_capture(plan, "position closed outside the engine")
            return
        # Broker-resting TP truth-sync: a fill that the stream missed must
        # still close the plan; a dead resting order must re-rest. (Reachable
        # only when the plan is still "filled", i.e. no exit ladder holds the
        # lock — under-ladder callers of _reconcile_plan always see exiting.)
        if plan.status == "filled" and plan.tp_order_id:
            observed_tp = plan.tp_order_id
            status = await self.trade.order_status(observed_tp)
            if status == "filled":
                await self.absorb_tp_locked(plan.id, observed_tp)
                return
            if status in ("canceled", "expired", "rejected"):
                # Pin the clear to the order this verdict is about: a tighten
                # may have replaced it with a live resting order meanwhile.
                await self.trade.fsm.update_fields(
                    plan.id, expect={"tp_order_id": observed_tp}, tp_order_id=None
                )
        if plan.status == "exiting" and plan.exit_order_id:
            observed_order = plan.exit_order_id
            status = await self.trade.order_status(observed_order)
            if status == "filled":
                order = await self.trade.alpaca.call(
                    self.trade.alpaca.trading.get_order_by_id, plan.exit_order_id, retries=1
                )
                raw = float(order.filled_avg_price or 0) or None
                avg = self.trade._fill_value(plan, raw, is_entry=False)
                realized = plan.pnl_at(avg)
                eq = self.trade._quality_on_fill(plan, "exit", avg)
                await self.trade.fsm.apply(
                    plan.id, PlanEvent.EXIT_FILLED,
                    guard={"exit_order_id": observed_order},
                    exit_premium=avg, realized_pnl=realized,
                    exited_at=as_utc(getattr(order, "filled_at", None)),
                    **({"exec_quality": eq} if eq is not None else {}),
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
            elif self.trade.alpaca.configured and not await self.clock.is_open():
                # A closing order resting against a closed market IS a parked
                # exit, whoever placed it — marking it here makes parking
                # survive an engine restart (the in-memory set starts empty),
                # so the monitor still resumes the ladder at the open.
                self._parked.add(plan.id)
        await self.arm(plan.id)

    async def shutdown(self) -> None:
        tasks = list(self._monitors.values())
        for task in tasks:
            task.cancel()
        self._monitors.clear()
        # Await the cancellations: a monitor mid-DB-operation must finish its
        # cleanup BEFORE the caller tears down the database underneath it.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------ monitors

    async def arm(self, plan_id: str) -> None:
        if plan_id in self._monitors:
            return
        self._monitors[plan_id] = asyncio.create_task(
            self._monitor(plan_id), name=f"monitor-{plan_id}"
        )

    def disarm(self, plan_id: str) -> None:
        task = self._monitors.pop(plan_id, None)
        self.monitor_health.pop(plan_id, None)
        self.monitor_beat.pop(plan_id, None)
        self._parked.discard(plan_id)
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

    async def absorb_tp_locked(self, plan_id: str, order_id: str) -> None:
        """Absorb a resting-TP fill under the plan's exit lock, so it cannot
        interleave with a concurrently-triggered exit ladder. The ladder's own
        takedown path (cancel_resting_tp inside _execute_exit_locked) already
        holds the lock and absorbs directly."""
        lock = self._exit_locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            await self.trade._absorb_tp_fill(plan_id, order_id)

    async def _position_gone(self, plan: TradePlan) -> bool:
        """True when the broker reports NO remaining position in any of the
        plan's legs. Conservative: any error keeps the plan alive."""
        if not self.trade.alpaca.configured:
            # Keyless there is no position truth at all: an empty list here
            # is ignorance, not evidence — never force-close on it.
            return False
        try:
            positions = {p["symbol"] for p in await self.trade.broker_positions(max_age_s=0.5)}
        except Exception as exc:
            log.warning("position check failed for %s: %s", plan.id, exc)
            return False
        return not ({leg["symbol"] for leg in plan.legs} & positions)

    @staticmethod
    def _cluster_exit_events(
        plan: TradePlan, records: list[dict], window_s: float = 90.0
    ) -> list[dict]:
        """Group raw per-leg closing fills into position-level exit EVENTS:
        fills within `window_s` of each other belong to one closing wave
        (an MLEG chunk, or a broker desk pairing single-leg closes seconds
        apart). Each event: {ts, premium (net/set), qty (sets)}. Returns []
        when the fills don't cleanly pair across legs — callers then fall
        back to the single aggregate exit."""
        leg_by_sym = {leg["symbol"]: leg for leg in plan.legs}
        clusters: list[list[dict]] = []
        for rec in sorted(records, key=lambda r: r["ts"]):
            if clusters and (rec["ts"] - clusters[-1][0]["ts"]).total_seconds() <= window_s:
                clusters[-1].append(rec)
            else:
                clusters.append([rec])
        events: list[dict] = []
        for cluster in clusters:
            per_leg: dict[str, tuple[float, float]] = {}
            for rec in cluster:
                q0, notional = per_leg.get(rec["symbol"], (0.0, 0.0))
                per_leg[rec["symbol"]] = (q0 + rec["qty"], notional + rec["qty"] * rec["avg"])
            if set(per_leg) != set(leg_by_sym):
                return []  # one-sided wave (assignment leg-out) — no clean pairing
            sets_per_leg = [
                per_leg[s][0] / max(leg_by_sym[s].get("ratio", 1), 1) for s in per_leg
            ]
            if len({round(q, 4) for q in sets_per_leg}) > 1:
                return []  # unequal per-leg qty inside the wave
            premium = sum(
                leg["side"] * leg.get("ratio", 1) * (per_leg[s][1] / per_leg[s][0])
                for s, leg in leg_by_sym.items()
            )
            events.append({
                "ts": max(rec["ts"] for rec in cluster).isoformat(),
                "premium": round(premium, 4),
                "qty": int(round(sets_per_leg[0])),
            })
        return events

    async def _capture_external_exit(
        self, plan: TradePlan
    ) -> tuple[float | None, int | None, datetime | None, list[dict] | None, str]:
        """The position vanished outside our exit path (manual close in the
        broker UI, broker auto-liquidation, expiry). Recover the ACTUAL
        closing fills from broker order history so the trade record and chart
        exit markers carry real numbers, not blanks.

        Returns (net exit premium per set, contract sets captured, last
        closing-fill time, per-wave exit events, detail). Premium =
        side*ratio-weighted per-leg closing average, each leg's average
        weighted by fill quantity — external liquidations land in CHUNKS at
        different prices (verified 2026-07-29: Alpaca closed 22 sets via
        auto_liquidate at 15:30 ET and the last 9 at 15:58 ET, at very
        different prices).

        The order scan deliberately carries NO `symbols` filter: Alpaca's
        GET /orders drops MLEG parent orders when `symbols` is set (verified
        against paper — the 22-set auto_liquidate MLEG order was invisible
        with the filter, present without it), and missing a chunk silently
        skews the average. Plan legs are matched in code instead."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        leg_syms = {leg["symbol"] for leg in plan.legs}
        ours = {plan.entry_order_id, plan.exit_order_id, plan.tp_order_id} - {None}
        orders: list = []
        after = as_utc(plan.created_at)
        try:
            for _page in range(5):  # paginate by submitted_at; bounded
                request = GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    after=after,
                    limit=200,
                    nested=True,
                )
                batch = await self.trade.alpaca.call(
                    self.trade.alpaca.trading.get_orders, request, retries=1
                )
                orders.extend(batch)
                if len(batch) < 200:
                    break
                last = max(
                    (as_utc(getattr(o, "submitted_at", None)) for o in batch
                     if getattr(o, "submitted_at", None) is not None),
                    default=None,
                )
                if last is None or last <= after:
                    break
                after = last
        except Exception as exc:
            log.warning("external-exit order scan failed for %s: %s", plan.id, exc)

        # symbol -> [contracts closed, qty-weighted avg price, last fill ts]
        fills: dict[str, list] = {}
        # raw per-leg closing fills, for wave clustering
        records: list[dict] = []

        def _absorb(order) -> None:
            # Prune OUR OWN order trees BEFORE recursing: the children of our
            # entry/exit/TP MLEG orders carry broker-generated client ids, so
            # a per-node check would absorb our own exit legs as "external".
            if str(getattr(order, "id", "")) in ours:
                return
            client_id = str(getattr(order, "client_order_id", "") or "")
            if client_id.startswith(plan.id):
                return
            for child in getattr(order, "legs", None) or []:
                _absorb(child)
            symbol = getattr(order, "symbol", None)
            if symbol not in leg_syms:
                return
            filled = float(getattr(order, "filled_qty", 0) or 0)
            avg = float(getattr(order, "filled_avg_price", 0) or 0)
            intent = str(getattr(order, "position_intent", "") or "")
            if filled <= 0 or avg <= 0 or "close" not in intent.lower():
                return
            filled_at = as_utc(getattr(order, "filled_at", None))
            prev_qty, prev_avg, prev_ts = fills.get(symbol, (0.0, 0.0, None))
            total = prev_qty + filled
            latest = max((t for t in (prev_ts, filled_at) if t is not None), default=None)
            fills[symbol] = [total, (prev_avg * prev_qty + avg * filled) / total, latest]
            if filled_at is not None:
                records.append({"symbol": symbol, "qty": filled, "avg": avg, "ts": filled_at})

        for order in orders:
            _absorb(order)

        if fills and all(leg["symbol"] in fills for leg in plan.legs):
            premium = sum(
                leg["side"] * leg.get("ratio", 1) * fills[leg["symbol"]][1]
                for leg in plan.legs
            )
            # Contract SETS closed = per-leg contracts / leg ratio; unequal
            # per-leg counts (partial assignment, one-sided expiry) make the
            # per-set premium approximate — say so instead of hiding it.
            sets_per_leg = [
                fills[leg["symbol"]][0] / max(leg.get("ratio", 1), 1)
                for leg in plan.legs
            ]
            sets_closed = int(min(sets_per_leg))
            exited_at = max(
                (fills[s][2] for s in leg_syms if fills[s][2] is not None),
                default=None,
            )
            detail = "closing fills recovered from broker history"
            if len({round(s) for s in sets_per_leg}) > 1:
                detail += f" (UNEQUAL per-leg close qty {sets_per_leg})"
            events = self._cluster_exit_events(plan, records)
            if sum(e["qty"] for e in events) != sets_closed:
                events = []  # waves don't add up — trust only the aggregate
            if len(events) > 1:
                detail += f" in {len(events)} waves"
            return round(premium, 4), sets_closed, exited_at, events or None, detail

        # Fallback: last defensible mark (stream cache) — approximate but
        # far better than a blank exit on the trade record.
        quotes = {s: self.market.latest_quote(s) for s in leg_syms}
        mid = position_mid_from_quotes(plan.legs, quotes)
        if mid is not None:
            return round(mid, 4), None, None, None, "no external fills found; last mark used"
        return None, None, None, None, "no fills or quotes recoverable"

    async def _force_close_with_capture(self, plan: TradePlan, context: str) -> None:
        premium, sets_closed, exited_at, events, detail = (
            await self._capture_external_exit(plan)
        )
        realized = plan.pnl_at(premium, sets_closed or None)
        if realized is not None and sets_closed and sets_closed < plan.effective_qty:
            detail += (
                f"; only {sets_closed}/{plan.effective_qty} sets found in history"
                " - P/L covers the captured sets only"
            )
        await self.trade.fsm.apply(
            plan.id, PlanEvent.FORCE_CLOSED,
            exit_premium=premium,
            realized_pnl=realized,
            exited_at=exited_at,
            exit_fills=events,
            exit_reason=plan.exit_reason or "external",
            notes=f"{context}; {detail}",
        )
        await self._sweep_ghosts_on_close(plan.id)

    async def _repair_blank_exits(self, max_age_days: float = 7.0) -> None:
        """Self-heal trade records: a plan force-closed WITHOUT exit data
        (engine down during an external liquidation, or closed by a build
        that predates fill capture) gets its exit premium / P/L / exit time
        backfilled from broker order history. Runs once per startup."""
        from datetime import timedelta

        from sqlalchemy import and_, or_, select

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(
                    TradePlan.status == "closed",
                    TradePlan.fill_premium.is_not(None),
                    or_(
                        TradePlan.exit_premium.is_(None),
                        # Externally-closed plans repaired before wave capture
                        # existed: re-capture to fill in the per-wave events.
                        and_(
                            TradePlan.exit_reason == "external",
                            TradePlan.exit_fills.is_(None),
                        ),
                    ),
                )
            )
            plans = [
                p for p in result.scalars()
                if p.updated_at is not None and as_utc(p.updated_at) >= cutoff
            ]
        for plan in plans:
            try:
                premium, sets_closed, exited_at, events, detail = (
                    await self._capture_external_exit(plan)
                )
                if premium is None:
                    log.warning("blank-exit repair: nothing recoverable for %s", plan.id)
                    continue
                realized = plan.pnl_at(premium, sets_closed or None)
                already_noted = "exit backfilled from broker history" in (plan.notes or "")
                await self.trade.fsm.update_fields(
                    plan.id,
                    exit_premium=premium,
                    realized_pnl=realized,
                    exited_at=exited_at,
                    exit_fills=events,
                    exit_reason=plan.exit_reason or "external",
                    notes=plan.notes if already_noted else (
                        ((plan.notes + " | ") if plan.notes else "")
                        + f"exit backfilled from broker history: {detail}"
                    ),
                )
                log.warning(
                    "blank-exit repair: plan %s backfilled exit=%.4f realized=%s (%s)",
                    plan.id, premium, realized, detail,
                )
            except Exception:
                log.exception("blank-exit repair failed for plan %s", plan.id)

    def _resolve_ghost_key(self, plan_id: str, key: str) -> None:
        keys = self._ghost_keys.get(plan_id)
        if keys and key in keys:
            keys.remove(key)
        if not keys:
            self._ghost_keys.pop(plan_id, None)

    async def _sweep_ghosts_on_close(self, plan_id: str) -> None:
        """The plan is closed; cancel any unresolved ghost that is live so no
        stray closing order can fill into a fresh (reversed) position."""
        self._parked.discard(plan_id)
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
            risk_cfg = await self.trade.risk.get_settings()
            entry_ttl_min = float(risk_cfg["entry_ttl_min"])
            sl_confirm_s = float(risk_cfg.get("sl_confirm_s", 3.0))
            symbols = [leg["symbol"] for leg in plan.legs]
            is_equity = plan.asset_class == "equity"
            underlying = plan.underlying

            queue: asyncio.Queue = asyncio.Queue(maxsize=200)
            if is_equity:
                # Shares: the stock quote IS the position quote — one
                # subscription, no option legs, no BSM model path.
                for sym in symbols:
                    await self.market.subscribe_stock(sym)
                    self.market.broadcast.subscribe(f"quote:{sym}", queue)
            else:
                await self.market.subscribe_options(symbols)
                await self.market.subscribe_stock(underlying)
                for sym in symbols:
                    self.market.broadcast.subscribe(f"oquote:{sym}", queue)
                # Underlying ticks stream far more often than option quotes — they
                # are the fast SL trigger (model-value proximity check below).
                self.market.broadcast.subscribe(f"quote:{underlying}", queue)
            self.market.broadcast.subscribe("plans", queue)

            log.info("monitor armed: plan %s TP=%.2f SL=%.2f stop=%s",
                     plan.id, plan.tp_premium, plan.sl_premium, plan.time_stop_utc)
            # Seed the quote cache NOW (REST / demo-synth): a leg that never
            # ticks on the stream must not leave TP/SL unevaluable.
            try:
                await self._refresh_plan_quotes(plan, symbols, max_age_s=0)
            except Exception as exc:
                log.warning("initial quote seed failed for %s: %s", plan_id, exc)
            last_no_mid_warn = 0.0
            last_plan_fetch = 0.0
            next_tp_rest_try = 0.0
            wait_s = self.quote_poll_near_s
            msg: dict | None = None
            from app.services.fair_value import FairValueFilter, position_quote_stats

            fv_filter = FairValueFilter()
            sl_breach_since: float | None = None
            last_quote_sig: tuple | None = None
            try:
                while True:
                    # Liveness heartbeat: reconcile's wedged-monitor watchdog
                    # restarts this task if the stamp goes stale.
                    self.monitor_beat[plan_id] = time.monotonic()
                    # Underlying ticks can arrive at stream rate; re-reading
                    # the plan row every wake would hammer the DB. Refresh on
                    # plan pushes and at least once a second otherwise.
                    if (
                        msg is None
                        or msg.get("t") == "plan"
                        or time.monotonic() - last_plan_fetch > 1.0
                    ):
                        plan = await self.trade.get_plan(plan_id)
                        last_plan_fetch = time.monotonic()
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
                    # Parked exit (limit resting against a closed market):
                    # when the clock flips open, resume the REAL ladder so the
                    # close reprices against live quotes and can escalate to
                    # market — the parked limit alone could sit unfilled.
                    if (
                        plan.status == "exiting"
                        and plan.exit_order_id
                        and plan_id in self._parked
                    ):
                        if await self._session_open(plan):
                            lock = self._exit_locks.get(plan_id)
                            if lock is None or not lock.locked():
                                log.warning(
                                    "market open - resuming escalation ladder "
                                    "for parked exit %s", plan_id,
                                )
                                self._parked.discard(plan_id)
                                await self._execute_exit(
                                    plan_id, plan.exit_reason or "time_stop"
                                )
                                return
                        else:
                            self.monitor_health[plan_id] = "exit parked until market open"
                    # Rest the TP at the broker once filled (retry with
                    # backoff on failure — never spam a broken broker).
                    if (
                        self.resting_tp
                        and self.trade.alpaca.configured
                        and plan.status == "filled"
                        and not plan.tp_order_id
                        and not plan.exit_order_id
                        and time.monotonic() >= next_tp_rest_try
                    ):
                        try:
                            await self.trade.submit_resting_tp(plan)
                            plan = await self.trade.get_plan(plan_id)
                            last_plan_fetch = time.monotonic()
                        except Exception as exc:
                            next_tp_rest_try = time.monotonic() + 30
                            log.warning("resting TP submit failed for %s: %s", plan_id, exc)

                    # Wake on a stream tick OR the poll cadence — the stream
                    # is the fast path, but TP/SL must keep evaluating when
                    # the stream is quiet (illiquid legs, after hours).
                    try:
                        # The time-stop deadline only drives the wake while it
                        # is still ahead. Once past it (status exiting — the
                        # firing branches above returned otherwise), a clamped
                        # negative timeout would spin this loop at 10Hz for as
                        # long as the exit takes (all night, when parked).
                        msg = await asyncio.wait_for(
                            queue.get(),
                            timeout=min(max(timeout, 0.1), wait_s) if timeout > 0 else wait_s,
                        )
                    except asyncio.TimeoutError:
                        msg = None
                        try:
                            # Staleness tolerance tracks the adaptive cadence:
                            # tight (1s) near a threshold or while no-mid,
                            # relaxed when far — otherwise the 30s default
                            # throttle defeats the tight re-check loop.
                            await self._refresh_plan_quotes(
                                plan, symbols, max_age_s=min(30.0, max(wait_s, 1.0))
                            )
                        except Exception as exc:
                            log.warning("quote poll failed for %s: %s", plan_id, exc)
                    if plan.status not in MONITOR_STATUSES:
                        # Exiting: nothing to evaluate here — idle at the far
                        # cadence (the near-threshold cadence would REST-poll
                        # quotes every second for the whole exit).
                        wait_s = self.quote_poll_s
                        continue

                    # Underlying tick: model-value proximity check. If the
                    # modeled premium is at/near a threshold, force-refresh
                    # the option quotes NOW instead of waiting for a tick.
                    # (Options only: for shares the stock quote IS the
                    # position observation — no model between them.)
                    if not is_equity and msg is not None and msg.get("t") == "quote":
                        spot = float(msg.get("mid") or 0)
                        mv = model_position_value(plan, spot, time.time() * 1000)
                        if mv is not None:
                            # Theo drift: the model's CHANGE moves the fair
                            # value between option quotes (level bias cancels).
                            fv_filter.on_model_value(mv)
                            span = abs(plan.tp_premium - plan.sl_premium) or 1.0
                            near = 0.15 * span
                            if mv >= plan.tp_premium - near or mv <= plan.sl_premium + near:
                                try:
                                    await self.market.refresh_option_quotes(
                                        symbols, max_age_s=1.0
                                    )
                                except Exception as exc:
                                    log.warning("model-trigger refresh failed for %s: %s",
                                                plan_id, exc)

                    quotes = self._plan_quotes(plan, symbols)
                    stats = position_quote_stats(plan.legs, quotes)
                    if stats is None:
                        # Keep trying at the tight cadence until quotes appear.
                        wait_s = self.quote_poll_near_s
                        missing = [s for s in symbols
                                   if not quotes.get(s)
                                   or not (quotes[s].get("bid") or quotes[s].get("ask"))]
                        self.monitor_health[plan_id] = f"no-mid: {','.join(missing)}"
                        now_mono = time.monotonic()
                        if now_mono - last_no_mid_warn > 60:
                            last_no_mid_warn = now_mono
                            log.warning(
                                "plan %s: TP/SL UNEVALUABLE - no quote for %s "
                                "(stream quiet and REST returned nothing)",
                                plan_id, missing,
                            )
                        continue
                    self.monitor_health[plan_id] = "ok"
                    micro, half_spread = stats
                    span = abs(plan.tp_premium - plan.sl_premium) or 1.0
                    # Kalman update: quote trust scales with its spread —
                    # a wide junk print CANNOT move the trigger price. Only
                    # NEW quotes count as observations: re-reading the same
                    # cached snapshot every wake would pile up phantom
                    # confidence and pin the fair value against theo drift.
                    quote_sig = tuple(
                        ((quotes[s] or {}).get("ts"), (quotes[s] or {}).get("mid"))
                        for s in symbols
                    )
                    if quote_sig != last_quote_sig or fv_filter.value is None:
                        last_quote_sig = quote_sig
                        fv = fv_filter.on_quote(
                            micro, half_spread, time.monotonic(),
                            q_rate=(KF_PROC_FRAC * span) ** 2,
                        )
                    else:
                        fv = fv_filter.value
                    quality = half_spread <= SL_QUALITY_HS_FRAC * span
                    # Adaptive cadence: tighten the poll floor near thresholds.
                    dist = min(abs(fv - plan.tp_premium), abs(fv - plan.sl_premium))
                    wait_s = self.quote_poll_near_s if dist < 0.2 * span else self.quote_poll_s
                    # TP side: software trigger only when no broker-resting TP
                    # is working the level already.
                    if not plan.tp_order_id and fv >= plan.tp_premium:
                        log.info("TP hit for plan %s (fv %.2f)", plan.id, fv)
                        await self._execute_exit(plan_id, "tp")
                        return
                    # SL: deep breaches fire NOW — on the fair value, or on a
                    # QUALITY raw microprice (a tight two-sided market
                    # printing a crash is real; don't wait for the filter).
                    # Shallow breaches must persist (dwell) so residual noise
                    # can't shake the position out.
                    deep = SL_DEEP_FRAC * span
                    if (plan.sl_premium - fv >= deep) or (
                        quality and plan.sl_premium - micro >= deep
                    ):
                        log.warning("SL hit for plan %s (fv %.2f micro %.2f, deep breach)",
                                    plan.id, fv, micro)
                        await self._execute_exit(plan_id, "sl")
                        return
                    if fv <= plan.sl_premium:
                        now_mono = time.monotonic()
                        if sl_confirm_s <= 0:
                            log.warning("SL hit for plan %s (fv %.2f)", plan.id, fv)
                            await self._execute_exit(plan_id, "sl")
                            return
                        if sl_breach_since is None:
                            sl_breach_since = now_mono
                            log.info("plan %s: SL breach @ %.2f - confirming for %.1fs",
                                     plan.id, fv, sl_confirm_s)
                        elif now_mono - sl_breach_since >= sl_confirm_s:
                            log.warning("SL hit for plan %s (fv %.2f, held %.1fs)",
                                        plan.id, fv, now_mono - sl_breach_since)
                            await self._execute_exit(plan_id, "sl")
                            return
                        self.monitor_health[plan_id] = (
                            f"sl-confirming ({time.monotonic() - sl_breach_since:.1f}s)"
                        )
                        # Re-check on fresh quotes at a tight cadence while
                        # the clock runs on the breach.
                        wait_s = min(wait_s, 0.5)
                        try:
                            await self._refresh_plan_quotes(plan, symbols, max_age_s=0.5)
                        except Exception:
                            pass
                    elif sl_breach_since is not None and (
                        fv > plan.sl_premium + SL_HYSTERESIS_FRAC * span
                    ):
                        log.info("plan %s: SL breach cleared (fv %.2f) - dwell reset",
                                 plan.id, fv)
                        sl_breach_since = None
            finally:
                if is_equity:
                    for sym in symbols:
                        self.market.broadcast.unsubscribe(f"quote:{sym}", queue)
                        await self.market.unsubscribe_stock(sym)
                else:
                    for sym in symbols:
                        self.market.broadcast.unsubscribe(f"oquote:{sym}", queue)
                    self.market.broadcast.unsubscribe(f"quote:{underlying}", queue)
                    await self.market.unsubscribe_options(symbols)
                    await self.market.unsubscribe_stock(underlying)
                self.market.broadcast.unsubscribe("plans", queue)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("monitor crashed for plan %s - REARMING in %.0fs", plan_id, self.rearm_delay_s)
            rearm = True
        finally:
            # Deregister SELF — and only self: _verify_closed's rearm path
            # may already have installed a replacement under this key, and
            # popping blindly would orphan it as an unkillable duplicate.
            if self._monitors.get(plan_id) is asyncio.current_task():
                self._monitors.pop(plan_id, None)
                self.monitor_health.pop(plan_id, None)
                self.monitor_beat.pop(plan_id, None)
        if rearm:
            await asyncio.sleep(self.rearm_delay_s)
            await self.arm(plan_id)

    # ------------------------------------------------------------ exits

    async def _refresh_plan_quotes(self, plan: TradePlan, symbols: list[str],
                                   max_age_s: float) -> None:
        """Force-refresh the quotes a plan's TP/SL evaluates on. Options go
        through the option REST path; equities through the staleness-aware
        stock quote fetch (internally throttled)."""
        if plan.asset_class == "equity":
            for sym in symbols:
                await self.market.fetch_latest_stock_quote(sym)
        else:
            await self.market.refresh_option_quotes(symbols, max_age_s=max_age_s)

    def _plan_quotes(self, plan: TradePlan, symbols: list[str]) -> dict:
        """Quotes keyed by symbol for TP/SL evaluation. Equity extra: in the
        overnight session the quote book is silent but Blue Ocean TRADES
        print (the overnight poller rolls them into 1m bars) — when the
        freshest bar is newer than the cached quote, observe the trade tape
        instead, as a synthetic two-sided quote with ~10bps half-spread. The
        Kalman layer treats that uncertainty honestly, and it beats freezing
        the bracket on an 8pm quote all night."""
        quotes = {s: self.market.latest_quote(s) for s in symbols}
        if plan.asset_class != "equity":
            return quotes
        for sym in symbols:
            try:
                bars = self.market.bars.get_bars(sym, "1m", limit=1)
            except Exception:
                continue
            if not bars:
                continue
            bar = bars[-1]
            bar_ts = float(bar.get("t") or 0)
            quote_ts = float((quotes.get(sym) or {}).get("ts") or 0)
            price = float(bar.get("c") or 0)
            if bar_ts > quote_ts and price > 0:
                half_spread = max(price * 0.001, 0.01)
                quotes[sym] = {
                    "bid": round(price - half_spread, 4),
                    "ask": round(price + half_spread, 4),
                    "mid": price,
                    "ts": bar_ts,
                    "synthetic": "overnight-trade",
                }
        return quotes

    async def _session_open(self, plan: TradePlan) -> bool:
        """Can exit orders WORK right now for this plan? Options: the broker
        RTH clock (fail-open). Equity plans flagged extended_hours: the
        verified 24/5 session map — limit orders fill premarket, postmarket
        and overnight, so only the weekend gap parks them."""
        if plan.asset_class == "equity" and plan.extended_hours:
            return equity_session() is not None
        return await self.clock.is_open()

    async def _rung_limit(self, plan: TradePlan, mid: float,
                          buffer: float | None) -> float | None:
        """Ladder rung price. None means market order — FORBIDDEN for
        equities outside RTH (after-hours market orders queue silently for
        the next open, verified 2026-08-04): substitute a very aggressive
        marketable limit instead."""
        if buffer is None:
            if plan.asset_class == "equity" and not await self.clock.is_open():
                return round_tick(mid - abs(mid) * EQUITY_AFTER_HOURS_MARKET_BUFFER)
            return None
        return round_tick(mid - abs(mid) * buffer)

    async def _execute_exit(self, plan_id: str, reason: str) -> None:
        """Escalation ladder, then a verification loop: this must not return
        with the position alive and nobody watching it.

        Each rung submits under a fresh idempotency key (unique per
        invocation, stable within the submit) so an ambiguous broker failure
        recovers the SAME order instead of stacking a second close.
        Serialized per plan: concurrent triggers (manual close racing a
        monitor, flatten-all racing both) queue instead of interleaving."""
        lock = self._exit_locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            await self._execute_exit_locked(plan_id, reason)

    async def _execute_exit_locked(self, plan_id: str, reason: str) -> None:
        # A broker-resting TP is a live closing order — it MUST come down
        # before any other close goes up, or the two could both fill. If the
        # cancel loses the race to a fill, the position is already closed.
        plan = await self.trade.get_plan(plan_id)
        if plan.tp_order_id:
            if await self.trade.cancel_resting_tp(plan):
                log.info("plan %s: resting TP filled during %s trigger - already closed",
                         plan_id, reason)
                await self._sweep_ghosts_on_close(plan_id)
                return
        # Closed market: the ladder is useless (limits can't fill, market
        # orders bounce) and actively harmful (all-night cancel/replace
        # churn). Park one resting limit instead; the monitor resumes the
        # ladder at the next open. Keyless mode skips this — simulated fills
        # land instantly regardless of the session.
        if self.trade.alpaca.configured and not await self._session_open(plan):
            await self._park_exit_locked(plan_id, reason)
            return
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
            limit = await self._rung_limit(plan, mid, buffer)
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

    async def _park_exit_locked(self, plan_id: str, reason: str) -> None:
        """Market is closed: ensure ONE closing limit order rests — it queues
        for the next session, so it works the open even if this engine is
        down by then — mark the plan parked, and return without escalating.
        The monitor resumes the real ladder when the clock flips open.
        Caller must hold the plan's exit lock."""
        plan = await self.trade.get_plan(plan_id)
        if plan.status in ("closed", "cancelled", "rejected"):
            await self._sweep_ghosts_on_close(plan_id)
            return
        self._parked.add(plan_id)
        self.monitor_health[plan_id] = "exit parked until market open"
        if plan.exit_order_id:
            # A closing order already rests (prior rung, prior park) — that
            # IS the parked order; reconcile keeps its status honest.
            return
        if await self._handle_ghost(plan):
            return  # a ghost already filled; adopted and closing
        quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in plan.legs}
        mid = position_mid_from_quotes(plan.legs, quotes)
        if mid is None:
            mid = plan.sl_premium
        # Rung-1 marketability: enough give to fill at the open, not a fire
        # sale against a gap (a gap through the limit waits for the ladder).
        buffer = self.escalation[0][0] or 0.02
        limit = round_tick(mid - abs(mid) * buffer)
        key = f"{uuid4().hex[:6]}p"
        self._ghost_keys.setdefault(plan_id, []).append(key)
        try:
            await self.trade.submit_exit(plan, reason, limit, attempt_key=key)
            self._resolve_ghost_key(plan_id, key)
        except Exception:
            # Status is unchanged, so the reconcile backstop / monitor
            # re-trigger the exit (and re-park) on their own cadence.
            log.exception("parked exit submit failed for %s - will retry", plan_id)
            return
        next_open = await self.clock.next_open()
        when = "the next market open"
        if next_open is not None:
            from zoneinfo import ZoneInfo

            when = next_open.astimezone(ZoneInfo("America/New_York")).strftime(
                "%Y-%m-%d %H:%M ET"
            )
        log.warning(
            "plan %s: %s exit PARKED (market closed) - one limit @ %.2f resting until %s",
            plan_id, reason, limit, when,
        )
        plan = await self.trade.get_plan(plan_id)
        if "exit parked" not in (plan.notes or ""):
            await self.trade.fsm.update_fields(
                plan_id,
                notes=((plan.notes + " | ") if plan.notes else "")
                + f"exit parked (market closed) - resting limit works {when}",
            )

    async def _verify_closed(self, plan_id: str, reason: str) -> None:
        """Poll until the plan closes; if the final order dies, resubmit a
        market close. Bounded per-iteration, unbounded overall — the ladder's
        whole point is that a triggered exit always finishes."""
        resubmit_failures = 0
        for attempt in range(self.verify_attempts):
            await asyncio.sleep(self.verify_poll_s)
            plan = await self.trade.get_plan(plan_id)
            # Market closed mid-verify (a late-day ladder ran into the bell):
            # stop driving — park and let the monitor resume at the open.
            if self.trade.alpaca.configured and not await self._session_open(plan):
                log.warning(
                    "market closed during exit verification for %s - parking", plan_id
                )
                await self._park_exit_locked(plan_id, reason)
                return
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
                # Market close — except equities outside RTH, where "market"
                # means an aggressive limit (see _rung_limit).
                quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"])
                          for leg in plan.legs}
                mid = position_mid_from_quotes(plan.legs, quotes)
                resubmit_limit = await self._rung_limit(
                    plan, mid if mid is not None else plan.sl_premium, None
                )
                key = f"{uuid4().hex[:6]}v"
                self._ghost_keys.setdefault(plan_id, []).append(key)
                try:
                    await self.trade.submit_exit(plan, reason, resubmit_limit, attempt_key=key)
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
                        await self._force_close_with_capture(
                            plan, "position vanished at broker during exit"
                        )
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
        """Exit-edit discipline: the LOSS side may only tighten (SL up, time
        stop earlier) — widening the loss you'll accept mid-trade is the
        classic tilt move and stays forbidden. TP is a PROFIT target: moving
        it (either direction, behind an explicit UI confirm) changes ambition,
        not risk, so it is freely editable as long as it stays above SL."""
        plan = await self.trade.get_plan(plan_id)
        if plan.status not in OPEN_STATUSES:
            raise ValueError("plan is not open")
        fields: dict = {}
        if tp is not None:
            if tp == plan.tp_premium:
                raise ValueError("TP unchanged")
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
        lock = self._exit_locks.setdefault(plan_id, asyncio.Lock())
        if lock.locked():
            raise ValueError("exit in progress - cannot change exits now")
        async with lock:
            # Commit the new levels FIRST, then take the old resting TP down:
            # the cancel's own broadcast wakes the monitor, and it must
            # re-rest against the NEW tp_premium, not the old one.
            updated = await self.trade._update_plan(plan_id, **fields)
            if "tp_premium" in fields and plan.tp_order_id:
                if await self.trade.cancel_resting_tp(plan):
                    raise ValueError("take-profit just filled at the broker")
            return updated
