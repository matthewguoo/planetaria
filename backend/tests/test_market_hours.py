"""Market-hours gating of the exit path + machine power defenses.

Context (2026-07-30): the machine slept through a 14:25 ET time stop. On
wake at ~22:48 ET the backstop fired the exit ladder into a CLOSED market
and churned cancel/replace limit orders (plus rejected market orders) all
night — the position never closed. These tests pin the fixed behavior:

- An exit triggered while the market is closed PARKS: exactly ONE resting
  limit order, no churn, and the ladder resumes at the next open.
- Parking survives an engine restart (reconcile re-marks it).
- The monitor does not hot-spin (10Hz REST polling) while an exit is
  pending past its time stop.
- KeepAwake holds the Windows execution state while plans are open;
  wake_watchdog reconciles immediately after a detected suspension.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services import power
from app.services.exit_enforcer import ExitEnforcer
from app.services.market_clock import MarketClock
from app.services.power import KeepAwake, wake_watchdog
from app.services.risk import RiskService
from app.services.trade_service import TradeService
from tests.test_chaos import (
    SYM,
    FakeAlpaca,
    FakeMarket,
    FlakyBroker,
    auto_filler,
    make_plan,
    wait_status,
)


class FakeClock:
    """Deterministic stand-in for MarketClock."""

    def __init__(self, is_open: bool):
        self._open = is_open
        self.next_open_dt = datetime.now(timezone.utc) + timedelta(hours=10)

    async def is_open(self) -> bool:
        return self._open

    async def next_open(self):
        return None if self._open else self.next_open_dt

    def status(self) -> dict:
        return {"known": True, "is_open": self._open}


@pytest_asyncio.fixture
async def stack(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/hours.db")
    broker = FlakyBroker()
    alpaca = FakeAlpaca(broker)
    market = FakeMarket()
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)
    enforcer.clock = FakeClock(is_open=False)
    # Compressed timing knobs (same idea as the chaos suite).
    enforcer.escalation = [(0.02, 0.05), (0.06, 0.05), (None, 0.0)]
    enforcer.verify_poll_s = 0.04
    enforcer.verify_attempts = 50
    enforcer.rearm_delay_s = 0.05
    enforcer.reconcile_interval_s = 0.15
    enforcer.quote_poll_s = 0.1
    enforcer.quote_poll_near_s = 0.05
    enforcer.resting_tp = False
    await risk.update_settings({"sl_confirm_s": 0.0})
    yield SimpleNamespace(
        db=db, broker=broker, alpaca=alpaca, market=market, trade=trade,
        enforcer=enforcer, clock=enforcer.clock,
    )
    await enforcer.shutdown()
    await db.close()


def _exit_orders(broker, plan_id):
    with broker.lock:
        return [o for o in broker.orders.values()
                if o.client_order_id.startswith(f"{plan_id}-x")]


# ------------------------------------------------------------- parking


@pytest.mark.asyncio
async def test_exit_while_closed_parks_one_resting_limit(stack):
    """Closed market: one resting limit, no ladder churn, no market order."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 2.0)

    await stack.enforcer._execute_exit(plan.id, "time_stop")

    orders = _exit_orders(stack.broker, plan.id)
    assert len(orders) == 1
    assert orders[0].limit_price is not None  # never a market order overnight
    # Rung-1 marketability off the live mid (2.0 * 0.98).
    assert orders[0].limit_price == pytest.approx(1.96)
    refreshed = await stack.trade.get_plan(plan.id)
    assert refreshed.status == "exiting"
    assert plan.id in stack.enforcer._parked
    assert "exit parked" in (refreshed.notes or "")
    cancels = [h for h in stack.broker.history if h[1].startswith("cancel")]
    assert cancels == []


@pytest.mark.asyncio
async def test_parked_exit_does_not_churn_across_reconciles(stack):
    """The incident signature: repeated reconcile passes while closed must
    not stack orders or cancel/replace the parked one."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 2.0)
    await stack.enforcer._execute_exit(plan.id, "time_stop")

    for _ in range(4):
        await stack.enforcer.reconcile_once()
        await asyncio.sleep(0.05)

    assert len(_exit_orders(stack.broker, plan.id)) == 1
    assert [h for h in stack.broker.history if h[1].startswith("cancel")] == []
    assert (await stack.trade.get_plan(plan.id)).status == "exiting"


@pytest.mark.asyncio
async def test_parked_exit_resumes_ladder_and_closes_at_open(stack):
    """Clock flips open: the monitor resumes the real ladder and the
    position closes."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 2.0)
    await stack.enforcer._execute_exit(plan.id, "time_stop")
    assert plan.id in stack.enforcer._parked

    await stack.enforcer.arm(plan.id)
    filler = asyncio.create_task(auto_filler(stack.broker, stack.trade, price=1.9))
    try:
        stack.clock._open = True
        status = await wait_status(stack.trade, plan.id, deadline=5.0)
    finally:
        filler.cancel()
    assert status == "closed"
    assert plan.id not in stack.enforcer._parked
    refreshed = await stack.trade.get_plan(plan.id)
    assert refreshed.exit_premium is not None


@pytest.mark.asyncio
async def test_reconcile_backstop_parks_overdue_stop_while_closed(stack):
    """Machine-wake scenario end to end: overdue time stop + closed market
    -> reconcile fires the backstop, which parks instead of churning."""
    plan = await make_plan(stack.db, broker=stack.broker, stop_in_h=-2.0)
    stack.market.pump(SYM, 2.0)

    await stack.enforcer.reconcile_once()
    await asyncio.sleep(0.1)  # backstop exit runs as its own task

    assert len(_exit_orders(stack.broker, plan.id)) == 1
    assert (await stack.trade.get_plan(plan.id)).status == "exiting"
    assert plan.id in stack.enforcer._parked


@pytest.mark.asyncio
async def test_restart_remarks_parked_exit(stack):
    """A fresh enforcer (post-restart) must re-learn that an exiting plan's
    live order against a closed market is parked."""
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 2.0)
    await stack.enforcer._execute_exit(plan.id, "time_stop")

    fresh = ExitEnforcer(stack.db, stack.market, stack.trade)
    fresh.clock = FakeClock(is_open=False)
    try:
        await fresh.reconcile_once()
        assert plan.id in fresh._parked
    finally:
        await fresh.shutdown()


@pytest.mark.asyncio
async def test_open_market_ladder_unchanged(stack):
    """While open, the escalation ladder behaves exactly as before."""
    stack.clock._open = True
    plan = await make_plan(stack.db, broker=stack.broker)
    stack.market.pump(SYM, 2.0)
    filler = asyncio.create_task(auto_filler(stack.broker, stack.trade, price=1.9))
    try:
        await stack.enforcer._execute_exit(plan.id, "time_stop")
        status = await wait_status(stack.trade, plan.id, deadline=5.0)
    finally:
        filler.cancel()
    assert status == "closed"
    assert plan.id not in stack.enforcer._parked


@pytest.mark.asyncio
async def test_parked_monitor_does_not_hot_spin(stack):
    """Past the time stop with a parked exit, the monitor must idle at the
    far poll cadence — not clamp the (negative) time-stop deadline to 0.1s
    and REST-poll quotes at 10Hz all night."""
    stack.enforcer.quote_poll_s = 1.0  # far cadence >> test window
    plan = await make_plan(stack.db, broker=stack.broker, stop_in_h=-1.0)
    stack.market.pump(SYM, 2.0)
    await stack.enforcer._execute_exit(plan.id, "time_stop")

    stack.market.refresh_calls = 0
    await stack.enforcer.arm(plan.id)
    await asyncio.sleep(0.6)
    stack.enforcer.disarm(plan.id)
    # One initial seed refresh; the 1s far cadence produces no timeouts in
    # 0.6s. The unfixed loop would have logged ~7 (10Hz wakes).
    assert stack.market.refresh_calls <= 2


# ----------------------------------------------------------- MarketClock


class _ClockBroker:
    def __init__(self):
        self.results: list = []
        self.calls = 0

    def get_clock(self):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _ClockAlpaca:
    def __init__(self, broker, configured=True):
        self.configured = configured
        self.trading = broker

    async def call(self, fn, /, *args, **kwargs):
        return fn(*args)


def _snap(is_open: bool, next_open_min: float, next_close_min: float):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        is_open=is_open,
        next_open=now + timedelta(minutes=next_open_min),
        next_close=now + timedelta(minutes=next_close_min),
    )


@pytest.mark.asyncio
async def test_clock_extrapolates_between_fetches():
    broker = _ClockBroker()
    broker.results = [_snap(True, 18 * 60, 60)]
    clock = MarketClock(_ClockAlpaca(broker))
    assert await clock.is_open() is True
    assert await clock.is_open() is True
    assert broker.calls == 1  # answered from the snapshot boundaries


@pytest.mark.asyncio
async def test_clock_refetches_after_boundary():
    broker = _ClockBroker()
    # Session "closes" almost immediately; the next answer must re-fetch.
    broker.results = [_snap(True, 18 * 60, 0.0001), _snap(False, 17 * 60, 24 * 60)]
    clock = MarketClock(_ClockAlpaca(broker))
    assert await clock.is_open() is True
    await asyncio.sleep(0.02)  # cross next_close
    assert await clock.is_open() is False
    assert broker.calls == 2
    assert (await clock.next_open()) is not None


@pytest.mark.asyncio
async def test_clock_fails_open():
    broker = _ClockBroker()
    broker.results = [ConnectionError("api down")] * 5
    clock = MarketClock(_ClockAlpaca(broker))
    assert await clock.is_open() is True
    # Failure is cached: hammering is_open must not hammer the broker.
    assert await clock.is_open() is True
    assert broker.calls == 1
    assert clock.status() == {"known": False}


@pytest.mark.asyncio
async def test_clock_keyless_is_open():
    broker = _ClockBroker()
    clock = MarketClock(_ClockAlpaca(broker, configured=False))
    assert await clock.is_open() is True
    assert broker.calls == 0


# ---------------------------------------------------------------- power


class _FakeRisk:
    def __init__(self):
        self.plans: list = []

    async def open_plans(self):
        return self.plans


@pytest.mark.asyncio
async def test_keep_awake_tracks_open_plans_and_running_strategies(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(power, "_set_execution_state", lambda keep: calls.append(keep) or True)
    risk = _FakeRisk()
    runner = SimpleNamespace(_running={})
    keeper = KeepAwake(risk, interval_s=0.01, runner=runner)
    keeper.supported = True  # force the Windows path regardless of test OS

    task = asyncio.create_task(keeper.run())
    try:
        risk.plans = ["plan"]
        await asyncio.sleep(0.05)
        assert keeper.active is True
        risk.plans = []
        await asyncio.sleep(0.05)
        assert keeper.active is False
        # Zero plans but a running strategy instance (note-mode watcher):
        # the box must stay awake for feeds and timer ticks.
        runner._running["row-1"] = object()
        await asyncio.sleep(0.05)
        assert keeper.active is True
        runner._running.clear()
        await asyncio.sleep(0.05)
        assert keeper.active is False
    finally:
        task.cancel()
    assert True in calls and False in calls


@pytest.mark.asyncio
async def test_wake_watchdog_reconciles_after_suspension(monkeypatch):
    reconciles = []

    class _Enforcer:
        async def reconcile_once(self):
            reconciles.append(1)

    # Wall clock jumps 600s across the first sleep: a machine suspend.
    ticks = iter([1000.0, 1600.0] + [1600.0 + i for i in range(1, 200)])
    monkeypatch.setattr(power.time, "time", lambda: next(ticks))

    task = asyncio.create_task(
        wake_watchdog(_Enforcer(), interval_s=0.01, threshold_s=30.0)
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if reconciles:
            break
    task.cancel()
    assert reconciles, "watchdog did not reconcile after a detected suspension"
