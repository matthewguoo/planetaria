"""ref_tick: params validation, trigger -> intent -> placement, one-per-day
dedupe, and timer scheduling."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.session import Database
from app.models.strategies import StrategyDecisionRow
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies import REGISTRY
from app.strategies.ref_tick import RefTick


class FakeTrade:
    def __init__(self):
        self.placed = []

    async def place_trade(self, payload):
        self.placed.append(payload)
        return {"id": f"plan{len(self.placed)}"}

    async def get_account(self):
        return {"equity": 100_000.0}


def fake_market(ask=700.10, bid=699.90, age_s=0.0):
    """Both quote entry points serve the same book (the real overnight_price
    delegates to fetch_latest_stock_quote outside the overnight session)."""
    import time

    async def quote(symbol):
        return {"bid": bid, "ask": ask, "mid": (ask + bid) / 2,
                "ts": (time.time() - age_s) * 1000}

    return SimpleNamespace(fetch_latest_stock_quote=quote, overnight_price=quote)


@pytest_asyncio.fixture
async def rig(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/ref.db")
    bus = EventBus()
    trade = FakeTrade()
    runner = StrategyRunner(
        db, bus, SignalStore(db), trade, RiskService(db),
        market=fake_market(), clock=None,
        settings=SimpleNamespace(strategies_enabled=True),
    )
    yield SimpleNamespace(db=db, bus=bus, trade=trade, runner=runner)
    await runner.shutdown()
    await db.close()


def manual_event(strategy_id: str) -> Event:
    return Event(type="manual", ts=datetime.now(timezone.utc), source="api-trigger",
                 key=None, symbols=(), payload={"strategy_id": strategy_id})


def test_registered():
    assert REGISTRY["ref_tick"] is RefTick


def test_params_validation():
    clean = RefTick.validate_params({"symbol": "amd"})
    assert clean["qty"] == 1 and clean["tp_pct"] == 0.003
    with pytest.raises(ValueError):
        RefTick.validate_params({"qty": 5000})
    with pytest.raises(ValueError):
        RefTick.validate_params({"tp_pct": 0.5})
    with pytest.raises(ValueError):
        RefTick.validate_params({"schedule_et": "25:99"})
    with pytest.raises(ValueError):
        RefTick.validate_params({"nonsense": 1})


@pytest.mark.asyncio
async def test_manual_trigger_places_bracketed_equity_plan(rig):
    row = await rig.runner.create("ref_tick", "ref-spy", {})
    await rig.runner.set_state(row["id"], "enabled")
    running = rig.runner._running[row["id"]]

    await running.strategy.on_event(manual_event(row["id"]), running.ctx)

    payload = rig.trade.placed[0]
    assert payload["asset_class"] == "equity"
    assert payload["extended_hours"] is True
    assert payload["underlying"] == "SPY"
    assert payload["entry_limit"] == pytest.approx(700.10)
    assert payload["tp_premium"] == pytest.approx(round(700.10 * 1.003, 2))
    assert payload["sl_premium"] == pytest.approx(round(700.10 * 0.995, 2))
    assert payload["qty"] == 1


@pytest.mark.asyncio
async def test_one_placement_per_day(rig):
    row = await rig.runner.create("ref_tick", "ref-once", {})
    await rig.runner.set_state(row["id"], "enabled")
    running = rig.runner._running[row["id"]]

    await running.strategy.on_event(manual_event(row["id"]), running.ctx)
    with pytest.raises(ValueError, match="dedupe"):
        await running.strategy.on_event(manual_event(row["id"]), running.ctx)
    assert len(rig.trade.placed) == 1

    async with rig.db.session() as session:
        actions = [r.action for r in (await session.scalars(
            select(StrategyDecisionRow).where(
                StrategyDecisionRow.strategy_id == row["id"])
        )).all()]
    assert actions.count("placed") == 1 and "skip" in actions


@pytest.mark.asyncio
async def test_stale_quote_refused(rig, tmp_path):
    rig.runner.market = fake_market(age_s=600)  # 10-minute-old book
    row = await rig.runner.create("ref_tick", "ref-stale", {})
    await rig.runner.set_state(row["id"], "enabled")
    running = rig.runner._running[row["id"]]

    await running.strategy.on_event(manual_event(row["id"]), running.ctx)
    assert rig.trade.placed == []
    async with rig.db.session() as session:
        notes = [r.detail for r in (await session.scalars(
            select(StrategyDecisionRow).where(
                StrategyDecisionRow.strategy_id == row["id"],
                StrategyDecisionRow.action == "note")
        )).all()]
    assert any("stale" in str(n) for n in notes)


@pytest.mark.asyncio
async def test_overnight_prices_off_tape_not_dead_book(rig):
    """The 2026-08-04 e2e failure: overnight, REST latest-quote serves the
    17:00 book (791.22 ask) while Blue Ocean prints 772.93. The strategy must
    price off overnight_price (the tape), never the dead REST book."""
    import time

    async def dead_book(symbol):  # what fetch_latest_stock_quote returned
        return {"bid": 790.90, "ask": 791.22, "mid": 791.06,
                "ts": (time.time() - 4 * 3600) * 1000}

    async def tape(symbol):  # what the overnight poller synthesizes
        return {"bid": 772.93, "ask": 772.93, "mid": 772.93,
                "ts": time.time() * 1000, "src": "overnight"}

    rig.runner.market = SimpleNamespace(
        fetch_latest_stock_quote=dead_book, overnight_price=tape)
    row = await rig.runner.create("ref_tick", "ref-on", {})
    await rig.runner.set_state(row["id"], "enabled")
    running = rig.runner._running[row["id"]]

    await running.strategy.on_event(manual_event(row["id"]), running.ctx)
    assert len(rig.trade.placed) == 1
    assert rig.trade.placed[0]["entry_limit"] == pytest.approx(772.93)


@pytest.mark.asyncio
async def test_timer_fires_only_on_schedule(rig):
    row = await rig.runner.create("ref_tick", "ref-sched",
                                  {"schedule_et": "16:05"})
    await rig.runner.set_state(row["id"], "enabled")
    running = rig.runner._running[row["id"]]

    def tick(et_hhmm: str) -> Event:
        return Event(type="timer", ts=datetime.now(timezone.utc), source="timer",
                     key=None, symbols=(),
                     payload={"kind": "tick", "session": "postmarket",
                              "et": f"2026-08-04T{et_hhmm}:00-04:00"})

    await running.strategy.on_event(tick("16:04"), running.ctx)
    assert rig.trade.placed == []
    await running.strategy.on_event(tick("16:05"), running.ctx)
    assert len(rig.trade.placed) == 1
