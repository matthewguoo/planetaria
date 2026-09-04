"""Regressions pinned by the 2026-09-03 code-health review.

Each test names the defect it guards against; none of them should need a
market, keys, or a wall clock.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.session import Database
from app.models.strategies import StrategyDecisionRow
from app.models.trade import TradePlan
from app.services import alpaca as alpaca_mod
from app.services.alpaca import PatientStockDataStream
from app.services.exit_enforcer import ExitEnforcer
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.services.trade_service import TradeService
from app.strategies import REGISTRY, register
from app.strategies.base import Strategy

NOW = datetime(2026, 9, 3, 18, 0, 0, tzinfo=timezone.utc)


class _Broadcast:
    def publish(self, topic, msg):
        pass


# ------------------------------------------------ reconcile vs place_trade


@pytest_asyncio.fixture
async def enforcer_env(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    alpaca = SimpleNamespace(configured=False)
    market = SimpleNamespace(broadcast=_Broadcast(), latest_quote=lambda s: None)
    trade = TradeService(db, alpaca, market, RiskService(db))
    enforcer = ExitEnforcer(db, market, trade)
    enforcer._now = lambda: NOW
    yield SimpleNamespace(db=db, trade=trade, enforcer=enforcer)
    await db.close()


async def _planned_row(env, age_s: float) -> str:
    async with env.db.session() as session:
        plan = TradePlan(
            underlying="SPY", strategy="manual", asset_class="equity",
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 500.0}],
            qty=1, entry_limit=500.0, tp_premium=None, sl_premium=450.0,
            time_stop_utc=NOW + timedelta(days=5), status="planned",
            created_at=NOW - timedelta(seconds=age_s),
            updated_at=NOW - timedelta(seconds=age_s),
        )
        session.add(plan)
        await session.commit()
        return plan.id


@pytest.mark.asyncio
async def test_reconcile_leaves_a_fresh_planned_row_alone(enforcer_env):
    """place_trade commits `planned` and THEN awaits the broker (up to ~15s).
    A reconcile pass inside that window must not cancel the plan, or the
    order lands at the broker under a terminal plan (the FSM would drop the
    later ENTRY_SUBMITTED)."""
    plan_id = await _planned_row(enforcer_env, age_s=5)
    plan = await enforcer_env.trade.get_plan(plan_id)
    await enforcer_env.enforcer._reconcile_plan(plan)
    assert (await enforcer_env.trade.get_plan(plan_id)).status == "planned"


@pytest.mark.asyncio
async def test_reconcile_cancels_a_stale_orphaned_planned_row(enforcer_env):
    plan_id = await _planned_row(
        enforcer_env, age_s=ExitEnforcer.ORPHAN_PLANNED_AGE_S + 30)
    plan = await enforcer_env.trade.get_plan(plan_id)
    await enforcer_env.enforcer._reconcile_plan(plan)
    assert (await enforcer_env.trade.get_plan(plan_id)).status == "cancelled"


# --------------------------------------------------- stream never spins


class _Sleeps:
    def __init__(self, stop_after: int):
        self.calls: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        if len(self.calls) >= self.stop_after:
            raise asyncio.CancelledError


async def _handler(_quote) -> None:
    pass


@pytest.mark.asyncio
async def test_generic_stream_error_backs_off_instead_of_spinning(monkeypatch):
    """A dead socket surfacing as OSError (not a WebSocketException) used to
    re-enter _consume() at zero delay: the same 100%-CPU class the mixin
    was written to fix."""
    s = PatientStockDataStream("PKTEST", "secret")
    s.subscribe_quotes(_handler, "SPY")

    async def ok():
        pass

    async def consume():
        raise OSError("connection reset by peer")

    closes: list[int] = []

    async def close():
        closes.append(1)

    monkeypatch.setattr(s, "_start_ws", ok)
    monkeypatch.setattr(s, "_send_subscribe_msg", ok)
    monkeypatch.setattr(s, "_consume", consume)
    monkeypatch.setattr(s, "close", close)
    rec = _Sleeps(stop_after=6)
    monkeypatch.setattr(alpaca_mod.asyncio, "sleep", rec)
    with pytest.raises(asyncio.CancelledError):
        await s._run_forever()
    backoffs = [d for d in rec.calls if d > 0]
    assert backoffs == [2.0, 4.0, 8.0]
    assert len(closes) == 3


# ------------------------------------- a strategy's own bug is an error


class _BuggyStrategy(Strategy):
    kind = "buggy"
    subscriptions = ("news",)
    default_params = {}

    async def on_event(self, event, ctx):
        # Not a refusal from ctx.submit(): a plain programming error.
        float("not a number")


@pytest_asyncio.fixture
async def runner_env(tmp_path):
    if "buggy" not in REGISTRY:
        register(_BuggyStrategy)
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/runner.db")
    bus = EventBus()
    trade = SimpleNamespace(
        placed=[], enforcer=SimpleNamespace(manual_close=None),
        get_account=None,
    )
    runner = StrategyRunner(
        db, bus, SignalStore(db), trade, RiskService(db), market=None,
        clock=None, settings=SimpleNamespace(strategies_enabled=True),
    )
    yield SimpleNamespace(db=db, bus=bus, runner=runner)
    await runner.shutdown()
    await db.close()
    REGISTRY.pop("buggy", None)


@pytest.mark.asyncio
async def test_strategy_value_error_is_journaled_as_error(runner_env):
    """The runner used to swallow every ValueError as an already-journaled
    refusal; a strategy's own float()/dict bug left no trace and the
    instance stayed green."""
    row = await runner_env.runner.create("buggy", "b1", {})
    await runner_env.runner.set_state(row["id"], "enabled")
    runner_env.bus.publish(Event(
        type="news", ts=datetime.now(timezone.utc), source="test",
        key=None, symbols=("SPY",), payload={},
    ))
    deadline = asyncio.get_event_loop().time() + 3.0
    actions: list[str] = []
    while asyncio.get_event_loop().time() < deadline:
        async with runner_env.db.session() as session:
            actions = list((await session.scalars(
                select(StrategyDecisionRow.action)
                .where(StrategyDecisionRow.strategy_id == row["id"])
            )).all())
        if "error" in actions:
            break
        await asyncio.sleep(0.02)
    assert "error" in actions
    assert runner_env.runner._running[row["id"]].errors == 1


def test_live_is_a_runner_param_defaulting_false():
    """A kind that forgets to declare `live` gets the note-mode twin, never
    a real order."""
    assert _BuggyStrategy.validate_params({})["live"] is False
    assert _BuggyStrategy.validate_params({"live": True})["live"] is True
