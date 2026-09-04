"""Chaos coverage for the strategy runtime: hung handlers, intent storms
against budgets, kill-all mid-storm, and supervise()-driven resurrection.
Complements test_strategy_runner.py (crash isolation) and test_chaos.py
(broker-side failure modes)."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.session import Database
from app.models.strategies import StrategyDecisionRow
from app.models.trade import TradePlan
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies import REGISTRY, register
from app.strategies.base import Strategy, TradeIntent


class ChaosStrategy(Strategy):
    """Configurable failure modes via event payload."""

    kind = "chaos"
    subscriptions = ("news",)
    default_params = {}
    start_crashes: int = 0  # class-level: crash on_start this many times

    def __init__(self):
        self.events = 0

    async def on_start(self, ctx):
        if type(self).start_crashes > 0:
            type(self).start_crashes -= 1
            raise RuntimeError("boot crash")

    async def on_event(self, event, ctx):
        self.events += 1
        if event.payload.get("hang"):
            await asyncio.sleep(3600)
        if event.payload.get("submit"):
            try:
                await ctx.submit(TradeIntent(
                    asset_class="option", underlying="SPY",
                    legs=[{"symbol": "SPY261218C00800000", "right": "C",
                           "strike": 800.0, "expiry": "2026-12-18", "side": 1,
                           "ratio": 1, "entry": 2.0, "iv": 0.2}],
                    qty=1, entry_limit=2.0, tp=4.0, sl=1.0,
                    time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
                    reason="storm",
                    dedupe_key=event.payload.get("dedupe_key"),
                ))
            except ValueError:
                pass  # budget refusals are the expected storm outcome


class DBFakeTrade:
    """place_trade that actually persists a TradePlan row, so DB-scoped
    budget guards (max_open_plans) see the storm."""

    def __init__(self, db):
        self.db = db
        self.placed = 0
        self.enforcer = SimpleNamespace(manual_close=self._noop)

    async def _noop(self, plan_id):
        pass

    async def place_trade(self, payload):
        self.placed += 1
        plan = TradePlan(
            underlying=payload["underlying"], strategy=payload["strategy"],
            strategy_id=payload.get("strategy_id"),
            legs=payload["legs"], qty=payload["qty"],
            entry_limit=payload["entry_limit"], tp_premium=payload["tp_premium"],
            sl_premium=payload["sl_premium"],
            time_stop_utc=datetime.fromisoformat(payload["time_stop_utc"]),
            status="filled",
        )
        async with self.db.session() as session:
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
        return {"id": plan.id}

    async def get_account(self):
        return {"equity": 100_000.0}


@pytest.fixture
def chaos_registered():
    if "chaos" not in REGISTRY:
        register(ChaosStrategy)
    ChaosStrategy.start_crashes = 0
    yield
    REGISTRY.pop("chaos", None)


@pytest_asyncio.fixture
async def rig(tmp_path, chaos_registered):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/chaos.db")
    bus = EventBus()
    trade = DBFakeTrade(db)
    runner = StrategyRunner(
        db, bus, SignalStore(db), trade, RiskService(db), market=None,
        clock=None, settings=SimpleNamespace(strategies_enabled=True),
    )
    yield SimpleNamespace(db=db, bus=bus, trade=trade, runner=runner)
    await runner.shutdown()
    await db.close()


def news(**payload) -> Event:
    return Event(type="news", ts=datetime.now(timezone.utc), source="chaos",
                 key=None, symbols=("SPY",), payload=payload)


async def wait_for(predicate, timeout=6.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_hung_handler_times_out_and_queue_keeps_draining(rig, monkeypatch):
    # The budget is a per-CLASS declaration now (LLM strategies raise it);
    # the runner reads it from the strategy's type at dispatch time.
    monkeypatch.setattr(ChaosStrategy, "event_timeout_s", 0.2)
    row = await rig.runner.create("chaos", "hang", {})
    await rig.runner.set_state(row["id"], "enabled")
    strategy = rig.runner._running[row["id"]].strategy

    rig.bus.publish(news(hang=True))
    rig.bus.publish(news(fine=True))
    assert await wait_for(lambda: strategy.events == 2)

    async with rig.db.session() as session:
        actions = [r.action for r in (await session.scalars(
            select(StrategyDecisionRow).where(
                StrategyDecisionRow.strategy_id == row["id"])
        )).all()]
    assert "error" in actions  # the timeout was journaled


@pytest.mark.asyncio
async def test_intent_storm_capped_by_budget(rig):
    row = await rig.runner.create("chaos", "storm", {"live": True, "budget": {"max_open_plans": 3}})
    await rig.runner.set_state(row["id"], "enabled")
    strategy = rig.runner._running[row["id"]].strategy

    for i in range(10):
        rig.bus.publish(news(submit=True, dedupe_key=f"storm-{i}"))
    assert await wait_for(lambda: strategy.events == 10)

    # events increments at handler ENTRY, so the 10th submit's journal write
    # can still be in flight here — settle on the journal itself.
    async def journal_actions() -> list[str]:
        async with rig.db.session() as session:
            return [r.action for r in (await session.scalars(
                select(StrategyDecisionRow).where(
                    StrategyDecisionRow.strategy_id == row["id"])
            )).all()]

    deadline = asyncio.get_event_loop().time() + 6.0
    actions = await journal_actions()
    while (actions.count("placed") + actions.count("rejected") < 10
           and asyncio.get_event_loop().time() < deadline):
        await asyncio.sleep(0.05)
        actions = await journal_actions()

    # Exactly the budgeted number of plans exist; the rest were refused.
    assert rig.trade.placed == 3
    assert actions.count("placed") == 3
    assert actions.count("rejected") == 7


@pytest.mark.asyncio
async def test_kill_all_mid_storm_stops_new_placements(rig):
    row = await rig.runner.create("chaos", "killstorm",
                                  {"live": True, "budget": {"max_open_plans": 100}})
    await rig.runner.set_state(row["id"], "enabled")

    for i in range(3):
        rig.bus.publish(news(submit=True, dedupe_key=f"pre-{i}"))
    assert await wait_for(lambda: rig.trade.placed == 3)

    await rig.runner.kill_all()
    placed_at_kill = rig.trade.placed
    for i in range(5):  # nobody is subscribed anymore
        rig.bus.publish(news(submit=True, dedupe_key=f"post-{i}"))
    await asyncio.sleep(0.3)
    assert rig.trade.placed == placed_at_kill


@pytest.mark.asyncio
async def test_boot_crash_resurrected_by_supervise(rig):
    ChaosStrategy.start_crashes = 1
    row = await rig.runner.create("chaos", "phoenix", {})
    await rig.runner.set_state(row["id"], "enabled")

    # First loop run dies in on_start; supervise restarts with ~2-3s backoff.
    # The restarted loop constructs no new strategy object (same instance in
    # _Running), so a processed event proves the LOOP is alive again.
    strategy = rig.runner._running[row["id"]].strategy

    async def keep_publishing():
        for _ in range(80):
            rig.bus.publish(news(ping=True))
            await asyncio.sleep(0.1)

    task = asyncio.create_task(keep_publishing())
    try:
        assert await wait_for(lambda: strategy.events > 0, timeout=8.0), \
            "strategy loop never came back after boot crash"
    finally:
        task.cancel()
