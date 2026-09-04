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
from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.trade import OPEN_STATUSES, TradePlan, as_utc
from app.services.market_clock import MarketClock, equity_session
from app.services.plan_fsm import MONITOR_STATES, PlanEvent
from app.services.spread_optimizer import (
    EntryWork,
    exit_ladder,
    exit_limit,
    work_spread_enabled,
)
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


def _bracket_span(plan: TradePlan) -> float:
    """Premium-space scale for thresholds, cadence and filter noise.
    Bracketed plans use the TP-SL span; SL-only swing plans (no target) use
    the entry-basis -> SL distance, which is the risk the trader declared."""
    sl = plan.sl_premium
    if sl is None:
        return 1.0
    if plan.tp_premium is not None:
        return abs(plan.tp_premium - sl) or 1.0
    basis = plan.fill_premium if plan.fill_premium is not None else plan.entry_limit
    return abs(basis - sl) or 1.0


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
        # Spread-optimizer ladder is derived from the risk settings at exit
        # time (exit_ladder); tests pin a compressed one here.
        self.spread_ladder_override: list[tuple[float | None, float]] | None = None
        self.verify_poll_s = 5.0
        # How long a MARKET partial close is awaited inline before the
        # stream / reconcile take over (the fill usually lands in a second).
        self.partial_wait_s = 10.0
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

    def _now(self) -> datetime:
        """The one wall-clock read for date/age decisions, so a test can pin
        it. (Incident: the expiry-settlement tests hard-coded 2026-09-03
        legs and began failing at midnight ET that night.)"""
        return datetime.now(timezone.utc)

    # A `planned` row is legitimately order-less for the length of one
    # broker submit (place_trade commits FIRST, then awaits the broker, up to
    # ~15s). Only a row older than this is an orphan of a crash.
    ORPHAN_PLANNED_AGE_S = 90.0

    async def _reconcile_plan(self, plan: TradePlan) -> None:
        """Sync one plan's order state from broker REST, then (re-)arm."""
        if plan.status == "planned" and not plan.entry_order_id:
            stamp = plan.updated_at or plan.created_at
            age_s = (
                (self._now() - as_utc(stamp)).total_seconds()
                if stamp is not None else float("inf")
            )
            if age_s < self.ORPHAN_PLANNED_AGE_S:
                # place_trade is mid-submit: cancelling now would leave the
                # order it is about to get at the broker with a terminal plan
                # (the FSM would drop the later ENTRY_SUBMITTED).
                return
            # Crashed between plan commit and order submit: no order
            # ever reached the broker, so nothing to manage.
            await self.trade.fsm.apply(
                plan.id, PlanEvent.ENTRY_CANCELLED,
                notes="orphaned planned row (no order submitted)",
            )
            return
        if plan.partial_exit and self.trade.alpaca.configured:
            try:
                plan = await self._reconcile_partial(plan)
            except Exception:
                log.exception("partial-exit reconcile failed for %s", plan.id)
            if plan.status in ("closed", "cancelled", "rejected"):
                return
        # Manual/external liquidation, checked BEFORE the time-stop backstop:
        # a held position that VANISHED at the broker without any exit order
        # of ours means someone closed it out from under the engine (broker
        # UI, desk auto-liquidation, expiry). The backstop below returns
        # early on every pass while a stop is overdue, so if this check came
        # after it, a vanished position with an overdue stop would ladder
        # forever and never be captured (incident 2026-09-03: fly-1's legs
        # expired at 16:00 and reconcile spent the evening re-firing an exit
        # that had nothing left to close). The updated_at age gate avoids
        # racing the broker's position-propagation right after an entry fill.
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
                if (
                    filled_qty == 0
                    and (plan.pricing or {}).get("reworking") == plan.entry_order_id
                ):
                    # The spread optimizer is replacing this rung right
                    # now; its own cancel is not the entry dying.
                    return
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
                realized = plan.close_pnl(avg)
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

    async def _expiry_cutoff_date(self) -> date:
        """Option legs expiring BEFORE this ET date can never trade again.
        While the market is open, today's expiries are still live; once it
        closes, anything not tradable by the next open is dead. MarketClock
        is fail-open, so a clock failure reads as 'today still tradable' —
        the conservative direction (park/ladder rather than settle)."""
        et_today = self._now().astimezone(ZoneInfo("America/New_York")).date()
        if await self.clock.is_open():
            return et_today
        next_open = await self.clock.next_open()
        if next_open is not None:
            return next_open.astimezone(ZoneInfo("America/New_York")).date()
        return et_today

    @staticmethod
    def _leg_expiry(leg: dict) -> date | None:
        raw = leg.get("expiry")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None

    async def _plan_expired(self, plan: TradePlan) -> bool:
        """Every leg is an option that can never trade again. Parking such a
        plan is a category error: there is no next session for it — the
        position stops existing at the closing bell of its expiry day.
        (Incident 2026-09-03: fly-1's 0DTE exit was parked 'until the next
        open' at 16:00; the short put expired 33c ITM and was assigned.)"""
        expiries = [self._leg_expiry(leg) for leg in plan.legs or []]
        if not expiries or any(e is None for e in expiries):
            return False
        cutoff = await self._expiry_cutoff_date()
        return all(e < cutoff for e in expiries)

    async def _held_close_legs(self, plan: TradePlan) -> tuple[list[dict], int]:
        """The subset of plan legs the broker still holds, and the sets that
        can close. Normally (plan.legs, effective_qty) — but the broker's
        desk clips legs out of expiring structures on its own (verified
        2026-09-03: their auto-liquidation bought back fly-1's short call at
        15:45, five minutes before our stop; every 4-leg close after that
        was unfillable). ([], 0) means nothing is held. Broker truth being
        unavailable degrades to the full structure, never to []."""
        if not self.trade.alpaca.configured:
            return plan.legs, plan.effective_qty
        try:
            held: dict[str, float] = {}
            for pos in await self.trade.broker_positions(max_age_s=2.0):
                held[pos["symbol"]] = held.get(pos["symbol"], 0.0) + abs(pos["qty"])
        except Exception as exc:
            log.warning("held-legs check failed for %s: %s", plan.id, exc)
            return plan.legs, plan.effective_qty
        legs = [leg for leg in plan.legs if held.get(leg["symbol"], 0) > 0]
        if len(legs) == len(plan.legs):
            return plan.legs, plan.effective_qty
        if not legs:
            return [], 0
        # Another plan could hold the same symbol; capping at this plan's own
        # qty keeps a shared-symbol close from eating a sibling's position.
        sets = min(
            int(held[leg["symbol"]] // max(leg.get("ratio", 1), 1)) for leg in legs
        )
        return legs, max(1, min(sets, plan.effective_qty))

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

        # Legs with no closing fills that can never trade again EXPIRED —
        # value them at intrinsic against the underlying's mark instead of
        # abandoning the whole capture to a blank. This is how a structure
        # the desk partially clipped (2026-09-03: short call bought back at
        # 15:45, the rest expired) still settles to a real number: real
        # fills for the clipped legs, intrinsic for the expired ones.
        expired_valued: list[str] = []
        missing = [leg for leg in plan.legs if leg["symbol"] not in fills]
        if missing:
            cutoff = await self._expiry_cutoff_date()
            spot = None
            if self.market is not None:
                spot = (self.market.latest_quote(plan.underlying) or {}).get("mid")
            if spot is not None:
                for leg in missing:
                    expiry = self._leg_expiry(leg)
                    strike = leg.get("strike")
                    right = str(leg.get("right") or "").upper()
                    if expiry is None or expiry >= cutoff or strike is None \
                            or right not in ("C", "P"):
                        continue
                    intrinsic = (
                        max(float(spot) - float(strike), 0.0) if right == "C"
                        else max(float(strike) - float(spot), 0.0)
                    )
                    fills[leg["symbol"]] = [
                        float(plan.effective_qty * max(leg.get("ratio", 1), 1)),
                        round(intrinsic, 4),
                        None,
                    ]
                    expired_valued.append(leg["symbol"])

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
            if expired_valued:
                detail += (
                    f"; {len(expired_valued)} expired leg(s) valued at intrinsic"
                    " vs the underlying's mark (approximate)"
                )
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
        realized = plan.close_pnl(premium, sets_closed or None)
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
                realized = plan.close_pnl(premium, sets_closed or None)
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
        try:
            closed = await self.trade.get_plan(plan_id)
            pending = (closed.partial_exit or {}).get("order_id")
            if pending:
                await self.trade.cancel_order(pending)
                await self.trade.fsm.update_fields(plan_id, partial_exit=None)
        except Exception:
            log.exception("partial sweep failed for plan %s", plan_id)
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

            # A plan with neither bracket is time-stop only. That is a real
            # exit plan — the engine's invariant is that every position has
            # one, not that every position has a stop — and it is what the
            # PEAD research concludes for: a stop inside the whipsaw band of a
            # name that just moved 5% is where most of that strategy's edge
            # goes. Per-strategy circuit breakers bound the risk instead.
            bracketless = plan.tp_premium is None and plan.sl_premium is None
            if bracketless:
                log.info("monitor armed: plan %s NO BRACKET, time stop %s",
                         plan.id, plan.time_stop_utc)
            else:
                # SL-only (tp None) is the equity swing shape: a hard stop
                # under an open-ended winner.
                log.info("monitor armed: plan %s TP=%s SL=%.2f stop=%s",
                         plan.id,
                         "-" if plan.tp_premium is None else f"{plan.tp_premium:.2f}",
                         plan.sl_premium, plan.time_stop_utc)
            # Seed the quote cache NOW over REST: a leg that never ticks on
            # the stream must not leave TP/SL unevaluable.
            try:
                await self._refresh_plan_quotes(plan, symbols, max_age_s=0)
            except Exception as exc:
                log.warning("initial quote seed failed for %s: %s", plan_id, exc)
            last_no_mid_warn = 0.0
            last_plan_fetch = 0.0
            next_tp_rest_try = 0.0
            entry_work_last = time.monotonic()
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
                        # Re-derive from the fresh row: tighten_exits may
                        # have GIVEN a stopless plan a stop, and a flag
                        # computed once at arm time would skip evaluating
                        # it until the next restart.
                        was_bracketless = bracketless
                        bracketless = plan.tp_premium is None and plan.sl_premium is None
                        if was_bracketless and not bracketless:
                            log.info("plan %s: bracket added while armed - "
                                     "evaluating TP=%s SL=%s from now on",
                                     plan.id, plan.tp_premium, plan.sl_premium)
                            wait_s = self.quote_poll_near_s
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
                    # SPREAD OPTIMIZER entry chase: every step_s, walk the
                    # resting rung one step toward the touch (inside the TTL
                    # above, which is the chase's hard ceiling).
                    if plan.status == "submitted" and (plan.pricing or {}).get("entry"):
                        work = EntryWork.from_json(plan.pricing["entry"])
                        if not work.exhausted:
                            due = time.monotonic() - entry_work_last >= work.step_s
                            if due:
                                outcome = await self.trade.rework_entry(plan)
                                entry_work_last = time.monotonic()
                                if outcome == "abandoned":
                                    return
                                if outcome == "replaced":
                                    plan = await self.trade.get_plan(plan_id)
                                    last_plan_fetch = time.monotonic()
                            # Wake at the chase cadence, never slower.
                            wait_s = min(wait_s, max(work.step_s, 0.25))
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
                        and plan.tp_premium is not None  # nothing to rest otherwise
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
                    if bracketless:
                        # No TP and no SL: the time stop above IS the exit
                        # plan, and there is no threshold to evaluate. Skip the
                        # quote machinery entirely rather than run a Kalman
                        # filter against triggers that do not exist — and idle
                        # at the far cadence, because nothing here gets more
                        # urgent as a price approaches anything.
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
                            span = _bracket_span(plan)
                            near = 0.15 * span
                            near_tp = (plan.tp_premium is not None
                                       and mv >= plan.tp_premium - near)
                            if near_tp or mv <= plan.sl_premium + near:
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
                    span = _bracket_span(plan)
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
                    dist = abs(fv - plan.sl_premium)
                    if plan.tp_premium is not None:
                        dist = min(dist, abs(fv - plan.tp_premium))
                    wait_s = self.quote_poll_near_s if dist < 0.2 * span else self.quote_poll_s
                    # TP side: software trigger only when no broker-resting TP
                    # is working the level already (and only when a TP exists —
                    # SL-only swing plans have no target).
                    if (plan.tp_premium is not None
                            and not plan.tp_order_id and fv >= plan.tp_premium):
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

    async def _spread_rung_limit(self, plan: TradePlan, mid: float,
                                 frac: float | None, legs: list[dict],
                                 quotes: dict) -> float | None:
        """Spread-optimizer rung: `frac` position half-spreads below the
        mid, floored to a tick. None = market, with the same after-hours
        equity substitution as the legacy ladder. A book with no usable
        half-spread (one-sided, crossed) falls back to the legacy rung so
        the exit still escalates."""
        if frac is None:
            return await self._rung_limit(plan, mid, None)
        from app.services.fair_value import position_quote_stats

        stats = position_quote_stats(legs, quotes)
        if stats is None:
            return await self._rung_limit(plan, mid, self.escalation[0][0] or 0.02)
        _, half_spread = stats
        return exit_limit(mid, half_spread, frac)

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
        if plan.partial_exit:
            plan = await self._cancel_partial_exit(plan)
            if plan.status in ("closed", "cancelled", "rejected"):
                await self._sweep_ghosts_on_close(plan_id)
                return
        # Closed market: the ladder is useless (limits can't fill, market
        # orders bounce) and actively harmful (all-night cancel/replace
        # churn). Park one resting limit instead; the monitor resumes the
        # ladder at the next open. Keyless mode skips this — simulated fills
        # land instantly regardless of the session. EXCEPTION: a structure
        # whose every leg has expired has no next session — parking it waits
        # for a market that will never reopen for these symbols while the
        # ITM legs get assigned. Settle it against reality instead.
        if self.trade.alpaca.configured and not await self._session_open(plan):
            if await self._plan_expired(plan):
                log.error(
                    "plan %s: %s exit triggered after its legs expired - "
                    "settling from broker history + intrinsic", plan_id, reason,
                )
                await self._force_close_with_capture(
                    plan, "legs expired before the exit completed"
                )
                return
            await self._park_exit_locked(plan_id, reason)
            return
        token = uuid4().hex[:6]
        # Which ladder: the plan's stamped choice, else the global toggle.
        # The spread ladder prices rungs in half-spreads of the live book
        # (mid -> inside -> touch -> market); the legacy one in % of mid.
        try:
            risk_cfg = await self.trade.risk.get_settings()
        except Exception:
            risk_cfg = {}
        worked = work_spread_enabled(plan.pricing, risk_cfg)
        ladder = (
            exit_ladder(float(risk_cfg.get("spread_opt_step_s", 3.0)),
                        float(risk_cfg.get("spread_opt_exit_max", 1.0)))
            if worked else self.escalation
        )
        if worked and self.spread_ladder_override is not None:
            ladder = self.spread_ladder_override
        for rung, (buffer, wait) in enumerate(ladder):
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
            # Close what the broker actually HOLDS, not what the plan says:
            # the desk clips legs out of expiring structures on its own, and
            # a close naming a leg the account no longer holds bounces on
            # every rung (incident 2026-09-03).
            close_legs, close_sets = await self._held_close_legs(plan)
            if not close_legs:
                log.error(
                    "plan %s: no legs held at broker mid-exit - "
                    "capturing external fills instead of laddering", plan_id,
                )
                await self._force_close_with_capture(plan, "position vanished during exit")
                return
            reduced = len(close_legs) < len(plan.legs)
            if reduced:
                log.warning(
                    "plan %s: broker holds %d of %d legs - closing the remainder "
                    "(x%d)", plan_id, len(close_legs), len(plan.legs), close_sets,
                )
            quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in close_legs}
            mid = position_mid_from_quotes(close_legs, quotes)
            if mid is None and not reduced:
                mid = plan.sl_premium
            # Marketable = accept a WORSE position value: sign-agnostic shift
            # downward by |mid|*buffer (long: sell lower; short: buy back higher).
            if worked:
                limit = (
                    await self._spread_rung_limit(plan, mid, buffer, close_legs, quotes)
                    if mid is not None else None
                )
            else:
                limit = await self._rung_limit(plan, mid, buffer) if mid is not None else None
            key = f"{token}r{rung}"
            self._ghost_keys.setdefault(plan_id, []).append(key)
            try:
                await self.trade.submit_exit(
                    plan, reason, limit, attempt_key=key,
                    close_legs=close_legs if reduced else None,
                    close_sets=close_sets if reduced else None,
                )
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
        close_legs, close_sets = await self._held_close_legs(plan)
        if not close_legs:
            log.error(
                "plan %s: no legs held at broker while parking - "
                "capturing external fills", plan_id,
            )
            await self._force_close_with_capture(plan, "position vanished before park")
            return
        reduced = len(close_legs) < len(plan.legs)
        quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in close_legs}
        mid = position_mid_from_quotes(close_legs, quotes)
        if mid is None and not reduced:
            mid = plan.sl_premium
        if mid is None:
            # No defensible price to rest at — leave it unparked; the
            # reconcile backstop retries with fresher quotes.
            log.warning("plan %s: no quotes to price a parked exit - deferring", plan_id)
            return
        # Rung-1 marketability: enough give to fill at the open, not a fire
        # sale against a gap (a gap through the limit waits for the ladder).
        buffer = self.escalation[0][0] or 0.02
        limit = round_tick(mid - abs(mid) * buffer)
        key = f"{uuid4().hex[:6]}p"
        self._ghost_keys.setdefault(plan_id, []).append(key)
        try:
            await self.trade.submit_exit(
                plan, reason, limit, attempt_key=key,
                close_legs=close_legs if reduced else None,
                close_sets=close_sets if reduced else None,
            )
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

    async def manual_close(self, plan_id: str, qty: int | None = None,
                           order_type: str = "market", limit_price: float | None = None) -> dict:
        """Human close. Whole @ market = the exit ladder; whole @ limit = the
        broker-resting TP moved to that price (the stop stays armed); a
        quantity below what is held = a partial close under its own order
        with the plan continuing for the remainder."""
        plan = await self.trade.get_plan(plan_id)
        if plan.status == "planned":
            if not plan.entry_order_id:
                await self.trade.fsm.apply(plan.id, PlanEvent.ENTRY_CANCELLED,
                                           notes="cancelled before any order was submitted")
                return {"mode": "whole", "status": "cancelled"}
            await self.trade.cancel_entry(plan)
            return {"mode": "whole", "status": "entry_cancelled"}
        if plan.status == "submitted":
            await self.trade.cancel_entry(plan)
            return {"mode": "whole", "status": "entry_cancelled"}
        if plan.status not in ("partially_filled", "filled", "exiting"):
            raise ValueError(f"plan is {plan.status} - nothing to close")
        held = plan.effective_qty
        if qty is not None and 0 < qty < held:
            if plan.status == "exiting":
                raise ValueError("exit in progress - cannot partial close now")
            return await self._partial_close(plan_id, int(qty),
                                             limit_price if order_type == "limit" else None)
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit close needs limit_price")
            if plan.status == "exiting":
                raise ValueError("exit in progress - cannot rest a limit now")
            updated = await self.tighten_exits(plan_id, tp=float(limit_price), sl=None, time_stop_utc=None)
            return {"mode": "whole", "status": "resting_tp", "tp_premium": updated.tp_premium}
        if plan.status == "partially_filled":
            await self.trade.cancel_entry(plan)
        self.disarm(plan_id)
        await self._execute_exit(plan_id, "manual")
        # Re-arm a watchdog in case the exit order dies.
        await self.arm(plan_id)
        return {"mode": "whole", "status": "ladder"}

    async def _partial_close(self, plan_id: str, qty: int, limit: float | None) -> dict:
        """Close `qty` of the held units under the plan's exit lock. The
        resting TP comes down first (it plus a partial would over-commit
        the position at the broker); the monitor re-rests it at the reduced
        size after the fill. Market partials are awaited briefly; a limit
        partial rests and is resolved by the stream / reconcile."""
        lock = self._exit_locks.setdefault(plan_id, asyncio.Lock())
        if lock.locked():
            raise ValueError("exit in progress - cannot partial close now")
        async with lock:
            plan = await self.trade.get_plan(plan_id)
            if plan.partial_exit:
                raise ValueError("a partial close is already pending - cancel it first")
            if plan.status not in ("filled", "partially_filled"):
                raise ValueError(f"plan is {plan.status} - cannot partial close")
            held = plan.effective_qty
            if not 0 < qty < held:
                raise ValueError(f"qty must be between 1 and {held - 1} for a partial close")
            if limit is not None and plan.sl_premium is not None and limit <= plan.sl_premium:
                raise ValueError("limit must be above the stop")
            if plan.tp_order_id:
                if await self.trade.cancel_resting_tp(plan):
                    return {"mode": "partial", "status": "closed_by_tp", "closed_qty": held, "remaining_qty": 0}
                plan = await self.trade.get_plan(plan_id)
            key = f"p{uuid4().hex[:6]}"
            stamp = {"key": key, "qty": qty, "limit": limit, "order_id": None,
                     "ts": datetime.now(timezone.utc).isoformat()}
            await self.trade.fsm.apply(plan_id, PlanEvent.EXIT_PARTIAL_SUBMITTED, partial_exit=stamp)
            try:
                order = await self.trade.submit_partial_exit(plan, qty, limit, key)
            except Exception as exc:
                await self.trade.fsm.update_fields(plan_id, partial_exit=None)
                raise ValueError(f"partial close rejected: {exc}")
            order_id = str(order.id)
            await self.trade.fsm.update_fields(plan_id, partial_exit={**stamp, "order_id": order_id})
            if limit is not None:
                return {"mode": "partial", "status": "resting", "order_id": order_id,
                        "closed_qty": 0, "remaining_qty": held}
            deadline = time.monotonic() + self.partial_wait_s
            while time.monotonic() < deadline:
                status = (await self.trade.order_status(order_id)).lower()
                if status == "filled":
                    fresh = await self.trade.alpaca.call(
                        self.trade.alpaca.trading.get_order_by_id, order_id, retries=1)
                    updated = await self.trade.apply_partial_fill(plan_id, fresh)
                    return {"mode": "partial", "status": "filled", "order_id": order_id,
                            "closed_qty": qty, "remaining_qty": updated.effective_qty
                            if updated.status not in ("closed",) else 0}
                if any(t in status for t in ("cancel", "rejected", "expired")):
                    await self.trade.fsm.update_fields(plan_id, partial_exit=None)
                    raise ValueError(f"partial close order died: {status}")
                await asyncio.sleep(min(1.0, self.verify_poll_s))
            return {"mode": "partial", "status": "pending", "order_id": order_id,
                    "closed_qty": 0, "remaining_qty": held}

    async def absorb_partial_locked(self, plan_id: str, order) -> None:
        """A partial-close fill from the stream, under the exit lock so it
        cannot interleave with a ladder that is about to size the close."""
        lock = self._exit_locks.setdefault(plan_id, asyncio.Lock())
        async with lock:
            await self.trade.apply_partial_fill(plan_id, order)

    async def _cancel_partial_exit(self, plan: TradePlan) -> TradePlan:
        """Before a whole close: take a pending partial order down to a
        terminal verdict. A partial that filled first shrinks the plan (the
        ladder then closes what is actually held)."""
        pe = plan.partial_exit or {}
        order_id = pe.get("order_id")
        if not order_id:
            if pe:
                await self.trade.fsm.update_fields(plan.id, partial_exit=None)
            return await self.trade.get_plan(plan.id)
        try:
            await self.trade.cancel_order(order_id)
        except Exception as exc:
            log.warning("partial cancel errored for %s: %s", plan.id, exc)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                status = (await self.trade.order_status(order_id)).lower()
            except Exception:
                status = ""
            if status == "filled":
                fresh = await self.trade.alpaca.call(
                    self.trade.alpaca.trading.get_order_by_id, order_id, retries=1)
                return await self.trade.apply_partial_fill(plan.id, fresh)
            if any(t in status for t in ("cancel", "rejected", "expired")):
                await self.trade.fsm.update_fields(plan.id, partial_exit=None)
                return await self.trade.get_plan(plan.id)
            if time.monotonic() > deadline:
                raise RuntimeError(f"partial close order {order_id} not terminal after cancel ({status})")
            await asyncio.sleep(0.5)

    async def _reconcile_partial(self, plan: TradePlan) -> TradePlan:
        """Restart-safe resolution of an in-flight partial close."""
        pe = plan.partial_exit or {}
        order_id = pe.get("order_id")
        if not order_id:
            found = await self.trade._order_by_client_id(f"{plan.id}-x{pe.get('key', '')}")
            if found is None:
                await self.trade.fsm.update_fields(plan.id, partial_exit=None)
                return await self.trade.get_plan(plan.id)
            order_id = str(found.id)
            await self.trade.fsm.update_fields(plan.id, partial_exit={**pe, "order_id": order_id})
        status = (await self.trade.order_status(order_id)).lower()
        if status == "filled":
            fresh = await self.trade.alpaca.call(
                self.trade.alpaca.trading.get_order_by_id, order_id, retries=1)
            return await self.trade.apply_partial_fill(plan.id, fresh)
        if any(t in status for t in ("cancel", "rejected", "expired")):
            await self.trade.fsm.update_fields(plan.id, partial_exit=None)
            return await self.trade.get_plan(plan.id)
        return plan

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
        # A plan with NO stop (intrinsic cap / time-stop-only) may be GIVEN
        # one here - that is a tightening, from unbounded to bounded. What it
        # may not get is a target alone: the monitor spans TP-to-SL, and a
        # target without a stop is not an exit plan (place_trade's rule).
        if tp is not None and sl is None and plan.sl_premium is None:
            raise ValueError("set a stop first - a target without a stop is not an exit plan")
        if tp is not None:
            if tp == plan.tp_premium:
                raise ValueError("TP unchanged")
            if plan.sl_premium is not None and tp <= plan.sl_premium:
                raise ValueError("TP must stay above SL")
            fields["tp_premium"] = tp
        if sl is not None:
            if plan.sl_premium is not None and sl <= plan.sl_premium:
                raise ValueError("SL may only move up (tighten)")
            effective_tp = fields.get("tp_premium", plan.tp_premium)
            if effective_tp is not None and sl >= effective_tp:
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
