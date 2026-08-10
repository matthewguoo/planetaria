"""The self-paper tier: note-mode intents become simulated plans filled
conservatively against the live book, exited by the sim sweep at their
time stops, invisible to every broker-facing consumer by construction."""

import types
from datetime import timedelta

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import OPEN_STATUSES, SIM_CLOSED, SIM_OPEN, TradePlan, utcnow
from app.services.strategy_runner import StrategyRunner
from app.strategies.base import TradeIntent


class FakeMarket:
    def __init__(self):
        self.quotes = {}

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)


@pytest_asyncio.fixture
async def rig(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/sim.db")
    market = FakeMarket()
    fake = types.SimpleNamespace(
        db=db, market=market, _running={},
        settings=types.SimpleNamespace(alpaca_account_name="test"),
    )
    for name in ("_sim_fill", "_place_sim", "_sim_close", "_journal"):
        setattr(fake, name, types.MethodType(getattr(StrategyRunner, name), fake))
    yield fake, market, db
    await db.close()


def equity_intent(side: int, px: float, qty: int = 10) -> TradeIntent:
    return TradeIntent(
        asset_class="equity", underlying="TEST",
        legs=[{"symbol": "TEST", "side": side, "ratio": 1, "entry": side * px}],
        qty=qty, entry_limit=side * px, tp=None, sl=None,
        time_stop_utc=utcnow() + timedelta(minutes=2),
        reason="test", dedupe_key=None,
    )


def test_sim_statuses_are_invisible_to_broker_paths():
    # The whole containment: enforcer scans, reconciliation, and global
    # risk all filter on OPEN_STATUSES and never learn the sim strings.
    assert SIM_OPEN not in OPEN_STATUSES and SIM_CLOSED not in OPEN_STATUSES


@pytest.mark.asyncio
async def test_long_fills_at_the_ask_and_exits_at_the_bid(rig):
    fake, market, db = rig
    market.quotes["TEST"] = {"bid": 99.98, "ask": 100.02}
    placed = await fake._place_sim("row1", "strat", equity_intent(+1, 100.0))
    assert placed["simulated"] and placed["fill_src"] == "quote"
    assert placed["entry_fill"] == 100.02          # conservative: the ask
    async with db.session() as session:
        plan = await session.get(TradePlan, placed["id"])
        assert plan.status == SIM_OPEN and plan.strategy_id == "row1"

    market.quotes["TEST"] = {"bid": 100.48, "ask": 100.52}
    await fake._sim_close(placed["id"])
    async with db.session() as session:
        plan = await session.get(TradePlan, placed["id"])
    assert plan.status == SIM_CLOSED
    # exit long at the BID: (100.48 - 100.02) x 10 shares
    assert plan.realized_pnl == pytest.approx(4.6, abs=0.01)


@pytest.mark.asyncio
async def test_short_profits_when_price_falls(rig):
    fake, market, db = rig
    market.quotes["TEST"] = {"bid": 99.98, "ask": 100.02}
    placed = await fake._place_sim("row1", "strat", equity_intent(-1, 100.0))
    assert placed["entry_fill"] == -99.98          # short sells at the bid
    market.quotes["TEST"] = {"bid": 99.48, "ask": 99.52}
    await fake._sim_close(placed["id"])
    async with db.session() as session:
        plan = await session.get(TradePlan, placed["id"])
    # cover at the ASK: (-99.52 - (-99.98)) x 10
    assert plan.realized_pnl == pytest.approx(4.6, abs=0.01)


@pytest.mark.asyncio
async def test_no_quote_falls_back_to_the_intent_price(rig):
    fake, market, db = rig
    placed = await fake._place_sim("row1", "strat", equity_intent(+1, 50.0))
    assert placed["fill_src"] == "intent-fallback"
    assert placed["entry_fill"] == 50.0


@pytest.mark.asyncio
async def test_option_structure_fills_at_leg_mids(rig):
    fake, market, db = rig
    legs = [
        {"symbol": "TC0", "right": "C", "strike": 100, "expiry": "2026-12-18",
         "side": -1, "ratio": 1, "entry": 1.6},
        {"symbol": "TP0", "right": "P", "strike": 100, "expiry": "2026-12-18",
         "side": -1, "ratio": 1, "entry": 1.3},
        {"symbol": "TCW", "right": "C", "strike": 106, "expiry": "2026-12-18",
         "side": 1, "ratio": 1, "entry": 0.35},
        {"symbol": "TPW", "right": "P", "strike": 94, "expiry": "2026-12-18",
         "side": 1, "ratio": 1, "entry": 0.30},
    ]
    for sym, mid in (("TC0", 1.6), ("TP0", 1.3), ("TCW", 0.35), ("TPW", 0.30)):
        market.quotes[sym] = {"bid": mid - 0.02, "ask": mid + 0.02}
    intent = TradeIntent(
        asset_class="option", underlying="TST", legs=legs, qty=3,
        entry_limit=-2.25, tp=None, sl=None,
        time_stop_utc=utcnow() + timedelta(minutes=2), reason="fly")
    placed = await fake._place_sim("row1", "strat", intent)
    assert placed["fill_src"] == "mids"
    assert placed["entry_fill"] == pytest.approx(-2.25, abs=0.01)
    # settle with everything worthless: seller keeps the credit x100 x qty
    market.quotes = {s: {"bid": 0.01, "ask": 0.01} for s in
                     ("TC0", "TP0", "TCW", "TPW")}
    await fake._sim_close(placed["id"])
    async with db.session() as session:
        plan = await session.get(TradePlan, placed["id"])
    # exit value = -0.01 -0.01 +0.01 +0.01 = 0; realized = (0-(-2.25))x3x100
    assert plan.realized_pnl == pytest.approx(675.0, abs=1.0)
