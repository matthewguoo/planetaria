"""Strategy runtime: one supervised task per enabled strategy instance,
consuming Events from the bus and executing TradeIntents through the same
pipeline human clicks use.

Crash isolation is structural, not aspirational: an on_event exception is
caught and journaled per-event; a loop-level crash restarts under
supervise() with backoff; a hung handler is cut off by EVENT_TIMEOUT_S. No
strategy failure mode can take down the engine or a sibling strategy.

State lives in strategy_instances rows — the runner reconstructs its tasks
from the DB at startup (the exit-enforcer pattern). The event bus is never
replayed across restarts; the stale-event guard in execute_intent is the
second lock on that door.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.models.strategies import (
    STRATEGY_STATES,
    StrategyDecisionRow,
    StrategyInstanceRow,
    validate_allocation,
    validate_breaker,
)
from app.models.trade import OPEN_STATUSES, SIM_CLOSED, SIM_OPEN, TradePlan, utcnow
from app.services.llm import LLMAnalyst
from app.services.registered import ladder_state
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore
from app.services.supervision import supervise
from app.services.trade_service import position_mid_from_quotes
from app.strategies import REGISTRY
from app.strategies.base import IntentRefused, Strategy, StrategyContext, TradeIntent

log = logging.getLogger("app.strategy")

# Instance queues are lossless: a dropped event is a missed trade. The size
# is generous because a wedged consumer is cut off by the event timeout long
# before the queue fills.
INSTANCE_QUEUE_SIZE = 256


def options_required_capital(legs: list[dict], qty: int,
                             entry_limit: float) -> tuple[float, str | None]:
    """Capital the broker actually HOLDS for an options structure, plus a
    hard refusal when the structure is unfundable inside a fleet allocation.

    The engine's cost checks are premium-based, which is right for debit
    structures and catastrophically wrong for short collateral: an uncovered
    short put is CASH-SECURED at this account level — ~strike x 100 per set
    (verified 2026-07-29: ~$70,087 held for one 700P against a ~$500
    premium). A $10k-allocated strategy that prices that trade at its
    premium commits the whole account's cash without ever exceeding its
    allocation on paper. This function prices what is held.

    Rules, per contract set, deliberately conservative (a covered pair is
    counted at its strike width even when the pair is a net-debit vertical
    whose true max loss is smaller — the guard may overcount, it must never
    undercount):

      uncovered short call  -> refusal (the broker rejects it anyway;
                               refuse here with the fix in the message)
      uncovered short put   -> strike x 100 collateral
      covered short         -> |short strike - long strike| x 100, longs
                               paired nearest-first within the same right
      net-debit structure   -> plus the debit itself

    Returns (required_dollars_total, fatal_reason_or_None).
    """
    shorts: dict[str, list[float]] = {"C": [], "P": []}
    longs: dict[str, list[float]] = {"C": [], "P": []}
    for leg in legs or []:
        right = leg.get("right")
        if right not in ("C", "P"):
            return 0.0, f"option leg without a right: {leg.get('symbol', leg)!r}"
        strike = float(leg.get("strike") or 0.0)
        if strike <= 0:
            return 0.0, f"option leg without a strike: {leg.get('symbol', leg)!r}"
        bucket = shorts if int(leg.get("side", 0)) < 0 else longs
        for _ in range(int(leg.get("ratio", 1))):
            bucket[right].append(strike)
    per_set = 0.0
    for right in ("C", "P"):
        available = sorted(longs[right])
        for k_short in sorted(shorts[right]):
            if not available:
                if right == "C":
                    return 0.0, (
                        "uncovered short call: unplaceable at this account "
                        "level — add a long call wing (defined-risk only "
                        "inside a strategy allocation)")
                per_set += k_short * 100.0        # cash-secured put
                continue
            j = min(range(len(available)),
                    key=lambda i: abs(available[i] - k_short))
            per_set += abs(available.pop(j) - k_short) * 100.0
    if entry_limit > 0:                            # signed: positive = debit
        per_set += entry_limit * 100.0
    return per_set * max(int(qty), 0), None


def _pct_changes(series: dict[str, float]) -> dict[str, float]:
    dates = sorted(series)
    out: dict[str, float] = {}
    for a, b in zip(dates, dates[1:]):
        if series[a]:
            out[b] = series[b] / series[a] - 1
    return out


def _corr_and_betas(closes: dict[str, dict[str, float]]
                    ) -> tuple[list[str], list[list[float | None]], dict]:
    """Pairwise daily-return correlations and SPY betas over whatever dates
    each pair shares. Plain python on purpose — the app does not depend on
    numpy, and n here is a handful of held symbols."""
    pct = {s: _pct_changes(v) for s, v in closes.items() if len(v) >= 21}
    syms = sorted(pct)

    def pair(a: str, b: str) -> tuple[float | None, float | None]:
        common = sorted(set(pct[a]) & set(pct[b]))
        if len(common) < 20:
            return None, None
        xa = [pct[a][d] for d in common]
        xb = [pct[b][d] for d in common]
        n = len(common)
        ma, mb = sum(xa) / n, sum(xb) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(xa, xb)) / n
        va = sum((x - ma) ** 2 for x in xa) / n
        vb = sum((y - mb) ** 2 for y in xb) / n
        corr = cov / ((va * vb) ** 0.5) if va > 0 and vb > 0 else None
        beta = cov / va if va > 0 else None        # b's beta on a
        return corr, beta

    matrix: list[list[float | None]] = [
        [1.0 if i == j else None for j in range(len(syms))]
        for i in range(len(syms))]
    for i, a in enumerate(syms):
        for j in range(i + 1, len(syms)):
            corr, _ = pair(a, syms[j])
            matrix[i][j] = matrix[j][i] = (round(corr, 3)
                                           if corr is not None else None)
    betas: dict[str, float | None] = {}
    for s in syms:
        if s == "SPY":
            betas[s] = 1.0
        elif "SPY" in pct:
            _, beta = pair("SPY", s)
            betas[s] = round(beta, 2) if beta is not None else None
        else:
            betas[s] = None
    return syms, matrix, betas


@dataclass
class _Running:
    row_id: str
    name: str
    strategy: Strategy
    ctx: StrategyContext
    queue: asyncio.Queue
    subscriptions: tuple[str, ...]
    task: asyncio.Task | None = None
    events_seen: int = 0
    errors: int = 0
    last_event_mono: float | None = None



def _options_capability(caps) -> dict:
    """Multi-leg options need a verified level 3; unprobed accounts report
    the broker's word (or the default) as such, never as a green light."""
    if caps is None:
        return {"met": True, "detail": "capabilities service absent - assuming multi-leg options"}
    derived = (caps.summary() or {}).get("derived") or {}
    level = derived.get("options_level")
    provenance = caps.level_provenance()
    if level is None:
        return {"met": False, "detail": "options level unknown - run the capabilities probe"}
    return {"met": int(level) >= 3,
            "detail": f"options level {level} ({provenance})"
                      + ("" if int(level) >= 3 else " - multi-leg structures will be refused")}

class StrategyRunner:
    def __init__(self, db, bus: EventBus, store: SignalStore, trade, risk, market,
                 clock, settings, llm: LLMAnalyst | None = None):
        self.db = db
        self.bus = bus
        self.store = store
        self.trade = trade
        self.risk = risk
        self.market = market
        self.clock = clock
        self.settings = settings
        self.llm = llm if llm is not None else LLMAnalyst(settings, store)
        self._running: dict[str, _Running] = {}
        self._lock = asyncio.Lock()  # serializes start/stop/state transitions

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Reconstruct running instances from DB rows (source of truth)."""
        if not getattr(self.settings, "strategies_enabled", True):
            log.warning("strategy runtime disabled by config (STRATEGIES_ENABLED=false)")
            return
        async with self.db.session() as session:
            rows = (await session.scalars(select(StrategyInstanceRow))).all()
        for row in rows:
            if row.state == "enabled":
                if row.kind not in REGISTRY:
                    log.error("instance %s references unknown kind %r - leaving stopped",
                              row.name, row.kind)
                    continue
                await self._spawn(row)
        self._sim_task = asyncio.create_task(
            supervise("sim-exits", self._sim_exit_loop), name="sim-exits")
        log.info("strategy runner up: %d enabled of %d instances",
                 len(self._running), len(rows))

    async def shutdown(self) -> None:
        for running in list(self._running.values()):
            await self._despawn(running.row_id)

    async def _spawn(self, row: StrategyInstanceRow) -> None:
        cls = REGISTRY[row.kind]
        strategy = cls()
        params = cls.validate_params(row.params or {})
        ctx = StrategyContext(
            market=self.market, clock=self.clock, params=params,
            log=logging.getLogger(f"app.strategy.{row.name}"),
            _runner=self, _instance_id=row.id,
        )
        queue: asyncio.Queue = asyncio.Queue(maxsize=INSTANCE_QUEUE_SIZE)
        for event_type in cls.subscriptions:
            self.bus.subscribe(event_type, queue=queue)
        running = _Running(
            row_id=row.id, name=row.name, strategy=strategy, ctx=ctx,
            queue=queue, subscriptions=tuple(cls.subscriptions),
        )
        self._running[row.id] = running
        running.task = asyncio.create_task(
            supervise(f"strategy-{row.name}", lambda: self._instance_loop(running)),
            name=f"strategy-{row.name}",
        )

    async def _despawn(self, row_id: str) -> None:
        running = self._running.pop(row_id, None)
        if running is None:
            return
        if running.task is not None:
            running.task.cancel()
            try:
                await running.task
            except (asyncio.CancelledError, Exception):
                pass
        for event_type in running.subscriptions:
            self.bus.unsubscribe(event_type, running.queue)
        try:
            await running.strategy.on_stop(running.ctx)
        except Exception:
            log.exception("on_stop failed for %s (ignored)", running.name)

    async def _instance_loop(self, running: _Running) -> None:
        await running.strategy.on_start(running.ctx)
        while True:
            event = await running.queue.get()
            # Manual triggers are bus-wide; deliver only to the target.
            if event.type == "manual" and event.payload.get("strategy_id") not in (
                running.row_id, running.name,
            ):
                continue
            running.events_seen += 1
            running.last_event_mono = time.monotonic()
            # Per-CLASS budget: LLM strategies declare a bigger one (their
            # queue is their own, so only their later events wait).
            timeout_s = float(type(running.strategy).event_timeout_s)
            try:
                await asyncio.wait_for(
                    running.strategy.on_event(event, running.ctx), timeout_s
                )
            except asyncio.TimeoutError:
                running.errors += 1
                await self._journal(running.row_id, "error", detail={
                    "error": f"on_event timeout after {timeout_s:.0f}s",
                    "event_type": event.type,
                }, signal_ids=_ids(event))
            except IntentRefused:
                # execute_intent already journaled the refusal; a strategy
                # letting it propagate is normal control flow, not an error.
                # (Only THAT type: a strategy's own ValueError — a bad
                # float(), a dict-shape slip — is a bug and is journaled
                # below like any other exception.)
                pass
            except Exception as exc:
                running.errors += 1
                log.exception("strategy %s on_event failed", running.name)
                await self._journal(running.row_id, "error", detail={
                    "error": repr(exc), "event_type": event.type,
                }, signal_ids=_ids(event))

    # ------------------------------------------------------------ registry

    async def create(self, kind: str, name: str, params: dict) -> dict:
        if kind not in REGISTRY:
            raise ValueError(f"unknown strategy kind {kind!r} "
                             f"(available: {sorted(REGISTRY)})")
        if not name or len(name) > 24:
            raise ValueError("name must be 1-24 chars (it doubles as the "
                             "TradePlan.strategy label)")
        clean = REGISTRY[kind].validate_params(params or {})
        row = StrategyInstanceRow(kind=kind, name=name, params=clean)
        async with self.db.session() as session:
            existing = await session.scalar(
                select(StrategyInstanceRow).where(StrategyInstanceRow.name == name)
            )
            if existing is not None:
                raise ValueError(f"instance named {name!r} already exists")
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row.to_dict()

    async def set_state(self, row_id: str, state: str) -> dict:
        """enable | paused | disabled. Persist FIRST, then reconcile tasks —
        a crash between the two heals at next startup from the row."""
        if state not in STRATEGY_STATES:
            raise ValueError(f"state must be one of {sorted(STRATEGY_STATES)}")
        async with self._lock:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
                if row is None:
                    raise ValueError("no such strategy instance")
                if state == "enabled" and row.kind not in REGISTRY:
                    raise ValueError(f"kind {row.kind!r} not in registry")
                row.state = state
                await session.commit()
                await session.refresh(row)
            if state == "enabled" and row_id not in self._running:
                await self._spawn(row)
            elif state != "enabled" and row_id in self._running:
                await self._despawn(row_id)
        return row.to_dict()

    async def delete(self, row_id: str) -> dict:
        """Remove an instance for good. Stops its task first, then the row.

        Refused while it has OPEN PLANS: the plans carry the instance's name
        as their only link back to it, so deleting the row would strand
        positions whose provenance no longer resolves. Flatten first.

        The decision journal is deleted with it — those rows have a foreign
        key to this id and would otherwise become unreadable orphans. That is
        a real loss, so it is stated rather than silent, and it is why the
        console asks before calling this.
        """
        async with self._lock:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
                if row is None:
                    raise ValueError("no such strategy instance")
                name, kind = row.name, row.kind
            open_plans = await self._open_plans(row_id)
            if open_plans:
                raise ValueError(
                    f"{name!r} has {len(open_plans)} open plan(s) — flatten it "
                    "first, or the positions lose their link back to the "
                    "strategy that opened them")
            await self._despawn(row_id)
            async with self.db.session() as session:
                journalled = await session.scalar(
                    select(func.count()).select_from(StrategyDecisionRow)
                    .where(StrategyDecisionRow.strategy_id == row_id)
                )
                await session.execute(
                    delete(StrategyDecisionRow).where(
                        StrategyDecisionRow.strategy_id == row_id))
                await session.execute(
                    delete(StrategyInstanceRow).where(
                        StrategyInstanceRow.id == row_id))
                await session.commit()
        log.warning("deleted strategy instance %s (%s) and %d journal rows",
                    name, kind, journalled or 0)
        return {"ok": True, "name": name, "kind": kind,
                "decisions_deleted": int(journalled or 0)}

    async def update_params(self, row_id: str, patch: dict) -> dict:
        async with self._lock:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
                if row is None:
                    raise ValueError("no such strategy instance")
                clean = REGISTRY[row.kind].validate_params({**(row.params or {}), **patch})
                row.params = clean
                await session.commit()
                await session.refresh(row)
            if row_id in self._running:  # live restart picks up new params
                await self._despawn(row_id)
                await self._spawn(row)
        return row.to_dict()

    async def flatten(self, row_id: str) -> dict:
        """Pause the instance, then close its open plans via the enforcer's
        manual-close path (which owns escalation and market-hours parking)."""
        row = await self.set_state(row_id, "paused")
        plans = await self._open_plans(row["id"])
        closed, failed = 0, []
        for plan in plans:
            try:
                if plan.status == SIM_OPEN:
                    await self._sim_close(plan.id, why="flatten")
                else:
                    await self.trade.enforcer.manual_close(plan.id)
                closed += 1
            except Exception as exc:
                failed.append({"plan_id": plan.id, "error": str(exc)})
        await self._journal(row_id, "note", detail={
            "flatten": {"closed": closed, "failed": failed}})
        return {"ok": True, "state": row["state"], "closed": closed, "failed": failed}

    async def kill_all(self, flatten: bool = False) -> dict:
        """The big red button: pause everything; optionally flatten."""
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(StrategyInstanceRow).where(StrategyInstanceRow.state == "enabled")
            )).all()
        paused, closed = 0, 0
        for row in rows:
            if flatten:
                result = await self.flatten(row.id)
                closed += result["closed"]
            else:
                await self.set_state(row.id, "paused")
            paused += 1
        log.warning("kill-all: paused %d strategies%s", paused,
                    f", closed {closed} plans" if flatten else "")
        return {"ok": True, "paused": paused, "closed": closed}

    # ------------------------------------------------------------ intents

    async def execute_intent(self, row_id: str, intent: TradeIntent) -> dict:
        """The gate every intent passes: stale-event guard -> dedupe ->
        per-strategy budget -> global risk (inside place_trade). Raises
        ValueError with the refusal; journals every outcome."""
        running = self._running.get(row_id)
        name = running.name if running else row_id
        signal_ids = list(intent.signal_ids)

        async def refuse(action: str, why: str) -> IntentRefused:
            await self._journal(row_id, action, detail={
                "why": why, "reason": intent.reason,
                "underlying": intent.underlying, "qty": intent.qty,
            }, dedupe_key=intent.dedupe_key, signal_ids=signal_ids)
            return IntentRefused(why)

        # Stale-event guard: never trade on old news after a restart or a
        # backed-up queue. Uses journal timestamps, not wall-clock trust.
        if signal_ids:
            rows = await self.store.by_ids(signal_ids)
            if rows:
                from datetime import datetime, timezone

                newest = max(datetime.fromisoformat(r["ts"]) for r in rows if r["ts"])
                age = (datetime.now(timezone.utc) - newest).total_seconds()
                if age > intent.max_event_age_s:
                    raise await refuse(
                        "skip", f"stale events: newest signal is {age:.0f}s old "
                                f"(max {intent.max_event_age_s:.0f}s)")

        # Dedupe across restarts/replays: one placement per key, ever —
        # simulated placements count, or a live flip mid-day would re-trade
        # everything the sim book already holds.
        if intent.dedupe_key:
            async with self.db.session() as session:
                dup = await session.scalar(
                    select(StrategyDecisionRow.id).where(
                        StrategyDecisionRow.strategy_id == row_id,
                        StrategyDecisionRow.dedupe_key == intent.dedupe_key,
                        StrategyDecisionRow.action.in_(("placed", "placed_sim")),
                    )
                )
            if dup is not None:
                raise await refuse("skip", f"dedupe: {intent.dedupe_key!r} already placed")

        multiplier = 1 if intent.asset_class == "equity" else 100
        notional = abs(intent.entry_limit) * multiplier * intent.qty

        # OPTIONS COLLATERAL. Premium notional understates what the broker
        # holds for short legs (an uncovered put is cash-secured at ~strike
        # x 100). Price the held capital, refuse unplaceable shapes early.
        required = notional
        if intent.asset_class == "option":
            held, fatal = options_required_capital(
                intent.legs, intent.qty, intent.entry_limit)
            if fatal:
                raise await refuse("rejected", f"options structure: {fatal}")
            required = max(required, held)

        # CIRCUIT BREAKER, checked before the trade rather than only on the
        # loop: a strategy that has just blown through its drawdown limit must
        # not get one more position in before the next sweep notices.
        try:
            breaker = await self.breaker_state(row_id)
        except Exception:                                     # noqa: BLE001
            breaker = {"tripped": False}
        if breaker.get("tripped"):
            raise await refuse(
                "rejected",
                f"circuit breaker: drawdown ${breaker['drawdown']:,.0f} has "
                f"reached the ${breaker['limit']:,.0f} limit")

        # ALLOCATION. The operator's answer to "how much of the account does
        # this strategy get", enforced rather than advertised: ctx.account()
        # already told the strategy its `available`, and this refuses the
        # trade if it sized past it anyway. Layered under the global guards —
        # all of them must pass.
        alloc = await self.allocation_state(row_id)
        if required > alloc["available"] + 0.01:
            what = (f"${required:,.0f} of collateral"
                    if required > notional else f"${required:,.0f}")
            raise await refuse(
                "rejected",
                f"allocation: {what} exceeds ${alloc['available']:,.0f} "
                f"left of this strategy's ${alloc['allocated']:,.0f} "
                f"({alloc['allocation']['value']:g}"
                f"{'%' if alloc['allocation']['mode'] == 'pct' else ' USD'}"
                f"), ${alloc['deployed']:,.0f} already deployed")

        # Per-strategy budget (layered UNDER the global guards, both must pass).
        # Not running (a direct call, or an intent racing a despawn): read
        # the row's params rather than assuming an empty set — `live` must
        # come from what the operator stored, never from a default.
        if running is not None:
            params = running.ctx.params
        else:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
            params = dict(row.params or {}) if row is not None else {}
        violations = await self.risk.validate_strategy_budget(
            strategy_id=row_id,
            budget=(params or {}).get("budget"),
            intent_notional=notional,
            symbol=intent.underlying,
        )
        if violations:
            raise await refuse("rejected", "; ".join(violations))

        payload = {
            "underlying": intent.underlying,
            "strategy": name[:24],
            "strategy_id": row_id,
            "asset_class": intent.asset_class,
            "extended_hours": intent.extended_hours,
            "legs": intent.legs,
            "qty": intent.qty,
            "entry_limit": intent.entry_limit,
            "tp_premium": intent.tp,
            "sl_premium": intent.sl,
            "time_stop_utc": intent.time_stop_utc.isoformat(),
        }
        await self._journal(row_id, "intent", detail={
            "reason": intent.reason, "payload": payload,
        }, dedupe_key=intent.dedupe_key, signal_ids=signal_ids)

        # SELF-PAPER. live=false, formalized: the same intent that just
        # passed every guard becomes a SIMULATED plan filled conservatively
        # against the live book (longs at the ask, shorts at the bid,
        # options at leg mids) instead of an order. It occupies the
        # strategy's allocation, counts against its breaker, and shows in
        # PERFORMANCE and the FUND page under its sim status — the
        # before-paper paper test. Only the broker is missing.
        # Fail CLOSED: an instance whose params cannot be read, or a kind
        # whose defaults omit `live`, places a sim plan, never a real one.
        if not bool((params or {}).get("live", False)):
            plan_dict = await self._place_sim(row_id, name, intent)
            await self._journal(row_id, "placed_sim", detail={
                "reason": intent.reason,
                "entry_fill": plan_dict["entry_fill"],
                "fill_src": plan_dict["fill_src"],
            }, dedupe_key=intent.dedupe_key, plan_id=plan_dict["id"],
                signal_ids=signal_ids)
            return plan_dict

        try:
            result = await self.trade.place_trade(payload)
        except ValueError as exc:  # global risk refusal
            raise await refuse("rejected", f"global risk: {exc}")
        except Exception as exc:
            await self._journal(row_id, "error", detail={
                "error": repr(exc), "stage": "place_trade",
            }, dedupe_key=intent.dedupe_key, signal_ids=signal_ids)
            raise

        await self._journal(row_id, "placed", detail={"reason": intent.reason},
                            dedupe_key=intent.dedupe_key,
                            plan_id=result.get("id"), signal_ids=signal_ids)
        return result

    # ---------------------------------------------------------- self-paper

    def _sim_fill(self, legs: list[dict], asset_class: str,
                  fallback_signed: float, entering: bool) -> tuple[float, str]:
        """Conservative signed fill against the live book. Equity: cross the
        spread on the side that hurts (enter long at the ask, exit long at
        the bid; mirrored for shorts). Options: the structure at leg mids —
        the study's own basis. No usable quote falls back to the intent's
        price with the provenance journaled, never silently."""
        if asset_class == "equity":
            leg = legs[0]
            side = int(leg.get("side", 1))
            quote = (self.market.latest_quote(leg["symbol"])
                     if self.market else None) or {}
            bid = float(quote.get("bid") or 0.0)
            ask = float(quote.get("ask") or 0.0)
            if bid > 0 and ask >= bid:
                px = ask if (side > 0) == entering else bid
                return side * px, "quote"
            return fallback_signed, "intent-fallback"
        total, live = 0.0, 0
        for leg in legs:
            quote = (self.market.latest_quote(leg["symbol"])
                     if self.market else None) or {}
            bid = float(quote.get("bid") or 0.0)
            ask = float(quote.get("ask") or 0.0)
            if bid > 0 and ask >= bid:
                total += int(leg.get("side", 1)) * (bid + ask) / 2 \
                    * int(leg.get("ratio", 1))
                live += 1
            else:
                total += int(leg.get("side", 1)) \
                    * float(leg.get("entry") or 0.0) * int(leg.get("ratio", 1))
        return total, ("mids" if live == len(legs) else
                       f"mids+{len(legs) - live}stale")

    async def _place_sim(self, row_id: str, name: str,
                         intent: TradeIntent) -> dict:
        fill, src = self._sim_fill(intent.legs, intent.asset_class,
                                   intent.entry_limit, entering=True)
        plan = TradePlan(
            underlying=intent.underlying.upper(),
            strategy=name[:24],
            strategy_id=row_id,
            account=getattr(self.settings, "alpaca_account_name", None),
            asset_class=intent.asset_class,
            extended_hours=intent.extended_hours,
            legs=intent.legs,
            qty=intent.qty,
            entry_limit=round(fill, 4),
            tp_premium=intent.tp,
            sl_premium=intent.sl,
            time_stop_utc=intent.time_stop_utc,
            status=SIM_OPEN,
        )
        async with self.db.session() as session:
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
        return {"id": plan.id, "simulated": True,
                "entry_fill": round(fill, 4), "fill_src": src,
                "status": SIM_OPEN}

    async def _sim_close(self, plan_id: str, why: str = "time stop") -> None:
        async with self.db.session() as session:
            plan = await session.get(TradePlan, plan_id)
            if plan is None or plan.status != SIM_OPEN:
                return
            exit_signed, src = self._sim_fill(
                plan.legs or [], plan.asset_class,
                float(plan.entry_limit or 0.0), entering=False)
            mult = 100 if plan.asset_class == "option" else 1
            realized = (exit_signed - float(plan.entry_limit or 0.0)) \
                * (plan.qty or 0) * mult
            plan.realized_pnl = round(realized, 2)
            plan.status = SIM_CLOSED
            strategy_id = plan.strategy_id
            await session.commit()
        if strategy_id:
            await self._journal(strategy_id, "sim_exit", detail={
                "plan_id": plan_id, "why": why,
                "exit_fill": round(exit_signed, 4), "fill_src": src,
                "realized_pnl": round(realized, 2),
            }, plan_id=plan_id)

    async def _sim_exit_loop(self, interval_s: float = 20.0) -> None:
        """The sim book's exit enforcer: time stops fire against the live
        quote, conservatively, on the same clock discipline the real
        enforcer keeps. tp/sl thresholds are not simulated in v1 — every
        fund strategy is time-stop-only, and pretending to know intrabar
        touch order without the tape would be the bad kind of simulation."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                now = utcnow()
                async with self.db.session() as session:
                    due = list(await session.scalars(
                        select(TradePlan.id).where(
                            TradePlan.status == SIM_OPEN,
                            TradePlan.time_stop_utc <= now,
                        )))
                for plan_id in due:
                    await self._sim_close(plan_id)
            except Exception:                                 # noqa: BLE001
                log.exception("sim exit sweep failed")

    async def execute_analysis(self, row_id: str, *, parent_signal_ids=(), **kwargs):
        """ctx.analyze() lands here: resolve the instance name for the
        journal's provenance chain, then delegate to the LLM gateway. The
        analysis itself is a SIGNAL (data plane), not a decision — the
        decision journal only sees what the strategy chooses to do with it."""
        running = self._running.get(row_id)
        name = running.name if running else row_id
        return await self.llm.analyze(
            strategy=name, parent_signal_ids=tuple(parent_signal_ids), **kwargs
        )

    async def journal_note(self, row_id: str, detail: dict,
                           signal_ids: tuple[int, ...] = ()) -> None:
        await self._journal(row_id, "note", detail=detail, signal_ids=list(signal_ids))

    async def reporters_for(self, date: str) -> list[dict]:
        """Earnings-calendar reporters journaled for a calendar date — the
        signal store's restart-proof estimate history (ctx.reporters)."""
        return await self.store.reporters_for(date)

    async def allocation_state(self, row_id: str) -> dict:
        """This instance's capital envelope: what it was given, what its open
        plans already commit, and what is left.

        `deployed` is entry notional, not mark — a strategy's claim on capital
        is what it committed, and marking to market would let a losing book
        quietly free up room to double down.
        """
        async with self.db.session() as session:
            row = await session.get(StrategyInstanceRow, row_id)
        alloc = validate_allocation(row.allocation if row is not None else None)
        # A broker we cannot reach means an allocation of zero, not an
        # unbounded one: `available` gates real orders, so it must fail closed.
        try:
            account = await self.trade.get_account()
            equity = float(account.get("equity") or 0.0)
        except Exception:                                     # noqa: BLE001
            equity = 0.0
        allocated = (equity * alloc["value"] / 100.0 if alloc["mode"] == "pct"
                     else alloc["value"])
        # A dollar allocation larger than the account is not capital, it is a
        # typo. Cap it at equity so `available` can never invite a trade the
        # account cannot fund.
        allocated = min(allocated, equity) if equity > 0 else 0.0
        deployed = 0.0
        if row is not None:
            for plan in await self._open_plans(row.id):
                if plan.legs and plan.legs[0].get("right"):
                    # Options claim the capital the broker HOLDS (collateral
                    # / defined-risk width), not the premium that changed
                    # hands — same math the intent gate charges, so a book
                    # of flies shrinks `available` by what it really costs.
                    held, fatal = options_required_capital(
                        plan.legs, plan.qty or 0, plan.entry_limit or 0.0)
                    premium = abs(plan.entry_limit or 0.0) * 100 * (plan.qty or 0)
                    deployed += premium if fatal else max(held, premium)
                else:
                    deployed += abs(plan.entry_limit or 0.0) * (plan.qty or 0)
        return {
            "allocation": alloc,
            "allocated": round(allocated, 2),
            "deployed": round(deployed, 2),
            "available": round(max(allocated - deployed, 0.0), 2),
            "account_equity": round(equity, 2),
            "pct_of_account": (round(allocated / equity * 100, 2) if equity > 0 else None),
        }

    async def account(self, row_id: str | None = None) -> dict:
        """The broker account, or — given an instance — that instance's own
        book. See StrategyContext.account for why the view is scoped."""
        account = await self.trade.get_account()
        if row_id is None:
            return account
        state = await self.allocation_state(row_id)
        return {
            **account,
            # The scoped view the strategy sizes against...
            "equity": state["allocated"],
            "cash": min(float(account.get("cash") or 0.0), state["available"]),
            "buying_power": min(float(account.get("buying_power") or 0.0),
                                state["available"]),
            # ...and the truth, still readable.
            "account_equity": state["account_equity"],
            "account_cash": account.get("cash"),
            "account_buying_power": account.get("buying_power"),
            "allocation": state["allocation"],
            "deployed": state["deployed"],
            "available": state["available"],
        }

    async def breaker_state(self, row_id: str) -> dict:
        """This strategy's drawdown against its own high-water mark.

        THE HIGH-WATER MARK IS RECOMPUTED, NEVER STORED. Realised P&L is
        replayed in close order and the running maximum taken, then current
        unrealised is added to the tail. A stored peak would drift on restart
        and could be silently reset by anyone who edited a row — this cannot,
        and it costs one query.

        Unrealised is marked per LEG from live quotes through pnl_at — the
        same formula every realized number comes from. A plan any of whose
        legs is unquoted (or whose entry has not filled) is marked at entry
        (contributing zero) rather than dropped: an unmarkable position must
        not be able to make a drawdown look smaller than it is. Never mark a
        structure at its underlying's price — that reads a $545-max-loss
        option fly as a $70k loss and shoots it (fly-1, 2026-09-01).
        """
        async with self.db.session() as session:
            row = await session.get(StrategyInstanceRow, row_id)
            if row is None:
                raise ValueError("no such strategy instance")
            plans = list(await session.scalars(
                select(TradePlan).where(TradePlan.strategy_id == row_id)
            ))
        breaker = validate_breaker(row.circuit_breaker)
        alloc = await self.allocation_state(row_id)

        closed = sorted(
            (p for p in plans if p.realized_pnl is not None),
            key=lambda p: (p.exited_at or p.created_at or datetime.min.replace(
                tzinfo=timezone.utc)),
        )
        equity, peak, worst = 0.0, 0.0, 0.0
        for plan in closed:
            equity += float(plan.realized_pnl or 0.0)
            peak = max(peak, equity)
            worst = max(worst, peak - equity)   # historical, reported not acted on

        unrealized = 0.0
        for plan in plans:
            if (plan.status not in OPEN_STATUSES and plan.status != SIM_OPEN) \
                    or plan.realized_pnl is not None:
                continue
            if self.market is None or not plan.legs:
                continue                      # marked at entry: contributes 0
            quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"])
                      for leg in plan.legs}
            pnl = plan.pnl_at(position_mid_from_quotes(plan.legs, quotes))
            if pnl is None:
                continue                      # marked at entry: contributes 0
            unrealized += pnl

        live = equity + unrealized
        peak = max(peak, live)            # the live point can BE the new peak
        # CURRENT drawdown from the high-water mark, not the historical worst.
        # Tripping on the worst-ever would mean a strategy that dipped and
        # recovered stays tripped for good, which is a different (and useless)
        # instrument. `max_drawdown` is reported beside it for the operator.
        drawdown = max(peak - live, 0.0)
        limit = (alloc["allocated"] * breaker["value"] / 100.0
                 if breaker["mode"] == "pct" else breaker["value"])
        return {
            "breaker": breaker,
            "realized": round(equity, 2),
            "unrealized": round(unrealized, 2),
            "pnl": round(live, 2),
            "peak": round(peak, 2),
            "drawdown": round(drawdown, 2),
            "max_drawdown": round(max(worst, drawdown), 2),
            "limit": round(limit, 2),
            "tripped": bool(breaker["enabled"] and limit > 0
                            and drawdown >= limit),
            "headroom": round(max(limit - drawdown, 0.0), 2),
        }

    async def check_breakers(self) -> list[dict]:
        """Trip any strategy past its drawdown limit: flatten its book, pause
        it, journal why. Called on a loop AND before every intent, because a
        drawdown develops between trades as much as at one."""
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(StrategyInstanceRow).where(
                    StrategyInstanceRow.state == "enabled")
            )).all()
        tripped = []
        for row in rows:
            try:
                state = await self.breaker_state(row.id)
            except Exception:                                 # noqa: BLE001
                log.exception("breaker check failed for %s", row.name)
                continue
            if not state["tripped"]:
                continue
            log.error("CIRCUIT BREAKER: %s drew down $%.0f against a $%.0f "
                      "limit — flattening and pausing",
                      row.name, state["drawdown"], state["limit"])
            await self._journal(row.id, "error", detail={
                "circuit_breaker": state,
                "why": f"drawdown ${state['drawdown']:,.0f} reached the "
                       f"${state['limit']:,.0f} limit "
                       f"({state['breaker']['value']:g}"
                       f"{'%' if state['breaker']['mode'] == 'pct' else ' USD'})"
                       f" — book flattened, instance paused",
            })
            result = await self.flatten(row.id)
            tripped.append({"name": row.name, "id": row.id,
                            "closed": result["closed"], **state})
        return tripped

    async def breaker_loop(self, interval_s: float = 30.0) -> None:
        """A drawdown does not wait for the next trade, so neither does this."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.check_breakers()
            except Exception:                                 # noqa: BLE001
                log.exception("breaker loop iteration failed")

    async def set_breaker(self, row_id: str, breaker: dict) -> dict:
        clean = validate_breaker(breaker)
        async with self._lock:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
                if row is None:
                    raise ValueError("no such strategy instance")
                row.circuit_breaker = clean
                await session.commit()
                await session.refresh(row)
        return row.to_dict()

    async def set_allocation(self, row_id: str, alloc: dict) -> dict:
        clean = validate_allocation(alloc)
        async with self._lock:
            async with self.db.session() as session:
                row = await session.get(StrategyInstanceRow, row_id)
                if row is None:
                    raise ValueError("no such strategy instance")
                row.allocation = clean
                await session.commit()
                await session.refresh(row)
        # Deliberately NOT a respawn: allocation is read per-decision through
        # ctx.account(), not captured at spawn, so a change takes effect on the
        # next event without interrupting a strategy mid-night.
        return row.to_dict()

    # ------------------------------------------------------------ queries

    async def _open_plans(self, strategy_id: str,
                          include_sim: bool = True) -> list[TradePlan]:
        """Open plans, sim book included by default — occupancy and the
        breaker treat simulated positions as real, which is what makes
        note-mode a test instead of a diary. Pass include_sim=False for
        broker-facing paths (there are none in this class; flatten splits
        the two itself)."""
        statuses = OPEN_STATUSES | ({SIM_OPEN} if include_sim else set())
        async with self.db.session() as session:
            return list(await session.scalars(
                select(TradePlan).where(
                    TradePlan.strategy_id == strategy_id,
                    TradePlan.status.in_(statuses),
                )
            ))

    async def _journal(self, row_id: str, action: str, *, detail: dict | None = None,
                       dedupe_key: str | None = None, plan_id: str | None = None,
                       signal_ids: list[int] | None = None) -> None:
        from app.services.call_log import CALL_LOG

        running = self._running.get(row_id)
        CALL_LOG.record(
            "engine", f"decision:{action}",
            detail=f"{running.name if running else row_id}"
                   f"{' plan ' + plan_id if plan_id else ''}",
            ok=action not in ("error", "rejected"),
        )
        try:
            async with self.db.session() as session:
                session.add(StrategyDecisionRow(
                    strategy_id=row_id, action=action, detail=detail,
                    dedupe_key=dedupe_key, plan_id=plan_id,
                    signal_ids=signal_ids or None,
                ))
                await session.commit()
        except Exception:
            # The journal must never break the pipeline, but a silent journal
            # outage would break replayability - log loudly.
            log.exception("DECISION JOURNAL WRITE FAILED (%s/%s)", row_id, action)

    async def capability_state(self) -> dict:  # noqa: D401
        """What the platform currently provides, against what strategies
        declare they need (Strategy.requires).

        Reported, never enforced. A strategy whose requirements are unmet
        still runs — that is exactly how a shadow night tells you what the
        missing entitlement costs. What must not happen is the engine
        journalling "no fresh quote" for six hours while the console shows
        a green light.
        """
        feed = str(getattr(self.settings, "alpaca_stock_feed", "iex"))
        try:
            risk_cfg = await self.risk.get_settings()
        except Exception:                                     # noqa: BLE001
            risk_cfg = {}
        return {
            "sip": {
                "met": feed == "sip",
                "detail": f"stock feed is {feed}"
                          + ("" if feed == "sip" else
                             " — IEX is dead outside 08:00–17:00 ET, so "
                             "after-hours entries cannot price"),
            },
            "shorts": {
                "met": not risk_cfg.get("equity_long_only", True),
                "detail": ("enabled" if not risk_cfg.get("equity_long_only", True)
                           else "equity_long_only is on — sells with no "
                                "position are refused engine-wide"),
            },
            "options": _options_capability(getattr(self.trade, "capabilities", None)),
            "llm": {
                "met": bool(self.llm.configured),
                "detail": (f"backend {getattr(self.settings, 'llm_backend', '?')}"
                           if self.llm.configured else "no model backend configured"),
            },
        }

    async def instances(self) -> list[dict]:
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(StrategyInstanceRow).order_by(StrategyInstanceRow.created_at)
            )).all()
        out = []
        for row in rows:
            data = row.to_dict()
            data["open_plans"] = len(await self._open_plans(row.id))
            running = self._running.get(row.id)
            data["runtime"] = self._runtime_status(running)
            cls = REGISTRY.get(row.kind)
            data["requires"] = sorted(cls.requires) if cls else []
            # Allocation AND drawdown, because the console shows both on the
            # row. A handful of extra queries per poll for a handful of
            # strategies; the alternative is a list that cannot show whether
            # a strategy is near its breaker without clicking into it.
            data["capital"] = {**await self.allocation_state(row.id),
                               "breaker": await self.breaker_state(row.id)}
            out.append(data)
        return out

    async def get(self, row_id: str) -> dict:
        async with self.db.session() as session:
            row = await session.get(StrategyInstanceRow, row_id)
            if row is None:
                raise ValueError("no such strategy instance")
        data = row.to_dict()
        data["open_plans"] = len(await self._open_plans(row.id))
        data["runtime"] = self._runtime_status(self._running.get(row_id))
        data["decisions"] = await self.decisions(row_id, limit=20)
        return data

    async def fund_state(self) -> dict:
        """The fund page in one payload: the account, every instance's
        envelope and open positions, and the account-level exposure rollup —
        directional for shares (signed entry notional), margin-at-risk for
        options (the collateral math, not the premium), and beta/pairwise
        correlation over the trailing ~90 sessions for whatever is held."""
        try:
            account = await self.trade.get_account()
        except Exception:                                     # noqa: BLE001
            account = {}
        equity = float(account.get("equity") or 0.0)

        instances = await self.instances()
        out_instances: list[dict] = []
        allocated_sum = 0.0
        underl_net: dict[str, float] = {}
        underl_gross: dict[str, float] = {}
        gross_long = gross_short = 0.0
        opt_margin = opt_premium = 0.0
        for inst in instances:
            row_id = inst["id"]
            # instances() already computed allocation + breaker per row —
            # reuse its payload instead of re-hitting the broker and DB.
            alloc = inst.get("capital") or {}
            breaker = alloc.get("breaker") or {}
            async with self.db.session() as session:
                plans = list(await session.scalars(
                    select(TradePlan).where(
                        TradePlan.strategy_id == inst["id"],
                        TradePlan.status.in_(OPEN_STATUSES | {SIM_OPEN}))))
            positions = []
            for p in plans:
                is_option = bool(p.legs and p.legs[0].get("right"))
                is_sim = p.status == SIM_OPEN
                if is_option:
                    held, fatal = options_required_capital(
                        p.legs, p.qty or 0, p.entry_limit or 0.0)
                    premium = (p.entry_limit or 0.0) * 100 * (p.qty or 0)
                    margin = abs(premium) if fatal else max(held, abs(premium))
                    if not is_sim:        # account exposure is REAL money
                        opt_margin += margin
                        opt_premium += premium
                    exposure = None       # no greeks in the console — margin
                else:                     # is the honest number for options
                    exposure = (p.entry_limit or 0.0) * (p.qty or 0)
                    if not is_sim:
                        sym = p.underlying
                        underl_net[sym] = underl_net.get(sym, 0.0) + exposure
                        underl_gross[sym] = underl_gross.get(sym, 0.0) + abs(exposure)
                        if exposure >= 0:
                            gross_long += exposure
                        else:
                            gross_short += -exposure
                    margin = abs(exposure)
                positions.append({
                    "plan_id": p.id, "underlying": p.underlying,
                    "asset_class": "option" if is_option else "equity",
                    "qty": p.qty, "entry_limit": p.entry_limit,
                    "legs": len(p.legs or []),
                    "margin_usd": round(margin, 2),
                    "net_usd": None if exposure is None else round(exposure, 2),
                    "status": p.status,
                    "created_at": str(p.created_at) if p.created_at else None,
                })
            allocated_sum += alloc["allocated"]
            out_instances.append({
                "id": row_id, "name": inst["name"], "kind": inst["kind"],
                "state": inst["state"],
                "live": bool((inst.get("params") or {}).get("live", False)),
                "allocated": alloc["allocated"], "deployed": alloc["deployed"],
                "available": alloc["available"],
                "breaker": {key: breaker.get(key)
                            for key in ("limit", "drawdown", "tripped")},
                "positions": positions,
            })

        symbols = sorted(set(underl_net) | {"SPY"})
        closes_by_sym: dict[str, dict[str, float]] = {}
        for sym in symbols:
            try:
                closes = await self.market.daily_closes(sym, days=90)
            except Exception:                                 # noqa: BLE001
                closes = []
            closes_by_sym[sym] = {str(c["date"]): float(c["close"])
                                  for c in (closes or [])}
        corr_syms, matrix, betas = _corr_and_betas(closes_by_sym)
        beta_net = sum(underl_net.get(s, 0.0) * (betas.get(s) or 0.0)
                       for s in underl_net)
        return {
            "account": {
                "equity": round(equity, 2),
                "cash": (round(float(account.get("cash") or 0.0), 2)
                         if account else None),
                "buying_power": (round(float(account.get("buying_power") or 0.0), 2)
                                 if account else None)},
            "instances": out_instances,
            "allocated_total": round(allocated_sum, 2),
            "unallocated": round(max(equity - allocated_sum, 0.0), 2),
            "exposure": {
                "gross_long_usd": round(gross_long, 2),
                "gross_short_usd": round(gross_short, 2),
                "net_usd": round(gross_long - gross_short, 2),
                "beta_weighted_net_usd": round(beta_net, 2),
                "options_margin_usd": round(opt_margin, 2),
                "options_premium_usd": round(opt_premium, 2),
                "by_underlying": [
                    {"symbol": s, "net_usd": round(underl_net[s], 2),
                     "gross_usd": round(underl_gross[s], 2),
                     "beta_spy": betas.get(s)}
                    for s in sorted(underl_net, key=lambda x: -underl_gross[x])],
                "corr": {"symbols": corr_syms, "matrix": matrix},
            },
        }

    async def performance(self, row_id: str) -> dict:
        """Per-strategy execution rollup from the plans that carry its name:
        open/closed counts, realized P&L, win rate, and the recent plans
        themselves (the strategy label on TradePlan is the join key the
        whole engine already maintains)."""
        async with self.db.session() as session:
            row = await session.get(StrategyInstanceRow, row_id)
            if row is None:
                raise ValueError("no such strategy instance")
            plans = (await session.scalars(
                select(TradePlan)
                .where(TradePlan.strategy_id == row_id)
                .order_by(TradePlan.created_at.desc())
                .limit(200)
            )).all()
            # Timestamps only: sessions_observed is a distinct-ET-dates count
            # and the decision bodies would be dead weight here.
            decision_ts = (await session.scalars(
                select(StrategyDecisionRow.ts)
                .where(StrategyDecisionRow.strategy_id == row_id)
            )).all()
        closed = [p for p in plans if p.realized_pnl is not None]
        wins = [p.realized_pnl for p in closed if p.realized_pnl > 0]
        losses = [p.realized_pnl for p in closed if p.realized_pnl <= 0]
        cls = REGISTRY.get(row.kind)
        registered = getattr(cls, "registered", None) if cls else None
        return {
            "name": row.name,
            "registered": registered,
            "ladder": ladder_state(registered, row.params or {}, plans,
                                   decision_ts),
            "open": sum(1 for p in plans
                        if p.status in OPEN_STATUSES or p.status == SIM_OPEN),
            "closed": len(closed),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realized_pnl": round(sum(p.realized_pnl for p in closed), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "plans": [p.to_dict() for p in plans[:50]],
        }

    async def decisions(self, row_id: str, limit: int = 100) -> list[dict]:
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(StrategyDecisionRow)
                .where(StrategyDecisionRow.strategy_id == row_id)
                .order_by(StrategyDecisionRow.id.desc())
                .limit(min(limit, 500))
            )).all()
        return [r.to_dict() for r in rows]

    @staticmethod
    def _runtime_status(running: _Running | None) -> dict:
        if running is None:
            return {"task": "not running"}
        task = running.task
        if task is None:
            state = "not started"
        elif task.cancelled():
            state = "cancelled"
        elif task.done():
            exc = task.exception()
            state = f"DEAD ({exc})" if exc else "finished"
        else:
            state = "running"
        return {
            "task": state,
            "queue_size": running.queue.qsize(),
            "events_seen": running.events_seen,
            "errors": running.errors,
            "last_event_age_s": (
                round(time.monotonic() - running.last_event_mono, 1)
                if running.last_event_mono else None
            ),
        }

    def status(self) -> dict:
        return {
            "enabled": getattr(self.settings, "strategies_enabled", True),
            "llm": {
                "configured": self.llm.configured,
                "model": getattr(self.settings, "llm_model", None),
            },
            "running": {
                r.name: self._runtime_status(r) for r in self._running.values()
            },
        }


def _ids(event) -> list[int]:
    return [event.signal_id] if event.signal_id is not None else []
