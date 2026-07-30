"""Reconcile-level time-stop backstop: an overdue time stop on a held
position must fire the exit ladder even when the per-plan monitor task is
dead or wedged — the monitor is the fast path, reconcile is the guarantee.

Context (2026-07-29): a position sailed through its 15:00 ET time stop
because the engine was down; the broker eventually liquidated it. The
backstop closes the in-engine half of that hole: any running engine now
fires an overdue stop within one reconcile interval, regardless of monitor
health.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.exit_enforcer import ExitEnforcer
from app.services.risk import RiskService
from app.services.trade_service import TradeService


class _Broadcast:
    def publish(self, topic, msg):
        pass


class _Market:
    def __init__(self):
        self.broadcast = _Broadcast()

    def latest_quote(self, symbol):
        return None


def _plan(status: str, stop_delta: timedelta) -> TradePlan:
    return TradePlan(
        underlying="SPY",
        strategy="long_call",
        legs=[{"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0,
               "expiry": "2026-07-31", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.3}],
        qty=1,
        entry_limit=2.0,
        tp_premium=4.0,
        sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + stop_delta,
        status=status,
        filled_qty=1,
        entry_order_id="entry-1",
        fill_premium=2.0,
    )


@pytest_asyncio.fixture
async def env(tmp_path):
    ns = SimpleNamespace()
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    alpaca = SimpleNamespace(configured=False)
    market = _Market()
    trade = TradeService(db, alpaca, market, RiskService(db))
    enforcer = ExitEnforcer(db, market, trade)

    ns.exits: list[tuple[str, str]] = []

    async def record_exit(plan_id, reason):
        ns.exits.append((plan_id, reason))

    enforcer._execute_exit = record_exit  # type: ignore[method-assign]
    # arm() would spawn real monitors; keep reconcile's decision isolated.
    armed = []

    async def record_arm(plan_id):
        armed.append(plan_id)

    enforcer.arm = record_arm  # type: ignore[method-assign]
    ns.db, ns.trade, ns.enforcer, ns.armed = db, trade, enforcer, armed
    yield ns
    await db.close()


async def _insert(db, plan: TradePlan) -> str:
    async with db.session() as session:
        session.add(plan)
        await session.commit()
        return plan.id


@pytest.mark.asyncio
async def test_overdue_time_stop_fires_without_a_monitor(env):
    plan_id = await _insert(env.db, _plan("filled", timedelta(minutes=-5)))
    plan = await env.trade.get_plan(plan_id)
    await env.enforcer._reconcile_plan(plan)
    await asyncio.sleep(0.05)  # backstop exit runs as its own task
    assert env.exits == [(plan_id, "time_stop")]
    assert env.armed == []  # exit path owns the plan; no monitor stacked on top


@pytest.mark.asyncio
async def test_time_stop_within_grace_leaves_monitor_in_charge(env):
    plan_id = await _insert(env.db, _plan("filled", timedelta(seconds=-10)))
    plan = await env.trade.get_plan(plan_id)
    await env.enforcer._reconcile_plan(plan)  # 10s overdue < 30s grace
    await asyncio.sleep(0.05)
    assert env.exits == []
    assert env.armed == [plan_id]  # normal path: monitor handles it


@pytest.mark.asyncio
async def test_future_time_stop_does_not_fire(env):
    plan_id = await _insert(env.db, _plan("filled", timedelta(hours=2)))
    plan = await env.trade.get_plan(plan_id)
    await env.enforcer._reconcile_plan(plan)
    await asyncio.sleep(0.05)
    assert env.exits == []
    assert env.armed == [plan_id]
