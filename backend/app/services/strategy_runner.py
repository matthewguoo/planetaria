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

from sqlalchemy import select

from app.models.strategies import (
    STRATEGY_STATES,
    StrategyDecisionRow,
    StrategyInstanceRow,
)
from app.models.trade import OPEN_STATUSES, TradePlan
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore
from app.services.supervision import supervise
from app.strategies import REGISTRY
from app.strategies.base import EVENT_TIMEOUT_S, Strategy, StrategyContext, TradeIntent

log = logging.getLogger("app.strategy")

# Instance queues are lossless: a dropped event is a missed trade. The size
# is generous because a wedged consumer is cut off by the event timeout long
# before the queue fills.
INSTANCE_QUEUE_SIZE = 256


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


class StrategyRunner:
    def __init__(self, db, bus: EventBus, store: SignalStore, trade, risk, market,
                 clock, settings):
        self.db = db
        self.bus = bus
        self.store = store
        self.trade = trade
        self.risk = risk
        self.market = market
        self.clock = clock
        self.settings = settings
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
            try:
                await asyncio.wait_for(
                    running.strategy.on_event(event, running.ctx), EVENT_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                running.errors += 1
                await self._journal(running.row_id, "error", detail={
                    "error": f"on_event timeout after {EVENT_TIMEOUT_S:.0f}s",
                    "event_type": event.type,
                }, signal_ids=_ids(event))
            except ValueError:
                # execute_intent already journaled the refusal; a strategy
                # letting it propagate is normal control flow, not an error.
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
        plans = await self._open_plans(row["name"])
        closed, failed = 0, []
        for plan in plans:
            try:
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

        async def refuse(action: str, why: str) -> ValueError:
            await self._journal(row_id, action, detail={
                "why": why, "reason": intent.reason,
                "underlying": intent.underlying, "qty": intent.qty,
            }, dedupe_key=intent.dedupe_key, signal_ids=signal_ids)
            return ValueError(why)

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

        # Dedupe across restarts/replays: one 'placed' per key, ever.
        if intent.dedupe_key:
            async with self.db.session() as session:
                dup = await session.scalar(
                    select(StrategyDecisionRow.id).where(
                        StrategyDecisionRow.strategy_id == row_id,
                        StrategyDecisionRow.dedupe_key == intent.dedupe_key,
                        StrategyDecisionRow.action == "placed",
                    )
                )
            if dup is not None:
                raise await refuse("skip", f"dedupe: {intent.dedupe_key!r} already placed")

        # Per-strategy budget (layered UNDER the global guards, both must pass).
        params = running.ctx.params if running else {}
        multiplier = 1 if intent.asset_class == "equity" else 100
        violations = await self.risk.validate_strategy_budget(
            strategy_name=name,
            budget=(params or {}).get("budget"),
            intent_notional=abs(intent.entry_limit) * multiplier * intent.qty,
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

    async def journal_note(self, row_id: str, detail: dict,
                           signal_ids: tuple[int, ...] = ()) -> None:
        await self._journal(row_id, "note", detail=detail, signal_ids=list(signal_ids))

    async def account(self) -> dict:
        return await self.trade.get_account()

    # ------------------------------------------------------------ queries

    async def _open_plans(self, name: str) -> list[TradePlan]:
        async with self.db.session() as session:
            return list(await session.scalars(
                select(TradePlan).where(
                    TradePlan.strategy == name,
                    TradePlan.status.in_(OPEN_STATUSES),
                )
            ))

    async def _journal(self, row_id: str, action: str, *, detail: dict | None = None,
                       dedupe_key: str | None = None, plan_id: str | None = None,
                       signal_ids: list[int] | None = None) -> None:
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

    async def instances(self) -> list[dict]:
        async with self.db.session() as session:
            rows = (await session.scalars(
                select(StrategyInstanceRow).order_by(StrategyInstanceRow.created_at)
            )).all()
        out = []
        for row in rows:
            data = row.to_dict()
            data["open_plans"] = len(await self._open_plans(row.name))
            running = self._running.get(row.id)
            data["runtime"] = self._runtime_status(running)
            out.append(data)
        return out

    async def get(self, row_id: str) -> dict:
        async with self.db.session() as session:
            row = await session.get(StrategyInstanceRow, row_id)
            if row is None:
                raise ValueError("no such strategy instance")
        data = row.to_dict()
        data["open_plans"] = len(await self._open_plans(row.name))
        data["runtime"] = self._runtime_status(self._running.get(row_id))
        data["decisions"] = await self.decisions(row_id, limit=20)
        return data

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
            "running": {
                r.name: self._runtime_status(r) for r in self._running.values()
            },
        }


def _ids(event) -> list[int]:
    return [event.signal_id] if event.signal_id is not None else []
