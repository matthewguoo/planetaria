"""day2_pop: the scan->enter clock, the second-session arithmetic, the
UP-only gate, slot ranking, and the time-stop-only intent shape. Engine
guards are tested where they live; these cover THIS strategy's logic."""

import logging
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.strategies import REGISTRY
from app.strategies.base import StrategyContext
from app.strategies.day2_pop import Day2Pop

ET = ZoneInfo("America/New_York")
TODAY = datetime.now(ET).date()


def sess(back: int) -> str:
    """A weekday-ish session `back` calendar days ago (tests build their
    own consistent close series; exact weekdays are irrelevant here)."""
    return (TODAY - timedelta(days=back)).isoformat()


class FakeMarket:
    def __init__(self, closes, px, dv=None):
        self.closes = closes      # symbol -> [{"date","close","volume"}]
        self.px = px              # symbol -> live quote price
        self.dv = dv or {}

    async def daily_dollar_volumes(self, symbols):
        return {s: self.dv.get(s, 5e8) for s in symbols}

    async def daily_closes(self, symbol, days=7):
        return self.closes.get(symbol, [])[-days:]

    async def fetch_latest_stock_quote(self, symbol):
        p = self.px[symbol]
        return {"bid": p - 0.02, "ask": p + 0.02, "ts": _time.time() * 1000}


class FakeRunner:
    def __init__(self, reporters):
        self.reporters = reporters      # date -> [payload dicts]
        self.notes, self.intents = [], []

    async def journal_note(self, _id, detail, signal_ids=()):
        self.notes.append(detail)

    async def execute_intent(self, _id, intent):
        self.intents.append(intent)
        return {"id": "plan-1"}

    async def account(self, _id):
        return {"equity": 10_000.0, "available": 10_000.0}

    async def reporters_for(self, date):
        return self.reporters.get(date, [])


def make(reporters, closes, px, params=None, dv=None):
    strat = Day2Pop()
    runner = FakeRunner(reporters)
    market = FakeMarket(closes, px, dv)
    ctx = StrategyContext(
        market=market, clock=None,
        params=Day2Pop.validate_params(params or {}),
        log=logging.getLogger("test"), _runner=runner, _instance_id="t",
    )
    return strat, ctx, runner, market


def closes_for(report_back: int, anchor_px: float, reaction_px: float):
    """Close series where the report lands `report_back` days ago and
    exactly one session (yesterday) has traded since."""
    return [
        {"date": sess(report_back + 2), "close": anchor_px, "volume": 1e7},
        {"date": sess(report_back), "close": anchor_px, "volume": 1e7},
        {"date": sess(1), "close": reaction_px, "volume": 2e7},
    ]


def amc(sym, back):
    return {"symbol": sym, "date": sess(back), "when": "amc"}


@pytest.mark.asyncio
async def test_second_session_pop_is_bought_at_0932_shape():
    reporters = {sess(2): [amc("POPS", 2)]}
    closes = {"POPS": closes_for(2, 100.0, 108.0)}     # +8% reaction
    strat, ctx, runner, _ = make(reporters, closes, {"POPS": 108.5})
    await strat._scan(ctx)
    assert [c["symbol"] for c in strat._cands] == ["POPS"]
    await strat._enter(ctx)
    note = runner.notes[-1]
    assert "would_trade" in note and note["would_trade"]["move_pct"] == 8.0
    (intent,) = runner.intents
    assert intent.legs[0]["side"] == 1 and "auction" not in intent.legs[0]
    assert intent.tp is None and intent.sl is None
    assert intent.qty == int(2500 // 108.5)
    assert intent.time_stop_utc.astimezone(ET).strftime("%H:%M") == "15:55"
    assert intent.dedupe_key == f"d2:POPS:{TODAY.isoformat()}"


@pytest.mark.asyncio
async def test_down_moves_and_small_moves_are_rejected():
    reporters = {sess(2): [amc("DOWN", 2), amc("MEHH", 2)]}
    closes = {"DOWN": closes_for(2, 100.0, 91.0),      # -9%: down side
              "MEHH": closes_for(2, 100.0, 103.0)}     # +3%: under the gate
    strat, ctx, runner, _ = make(reporters, closes,
                                 {"DOWN": 91.0, "MEHH": 103.0})
    await strat._scan(ctx)
    assert strat._cands == []
    rejects = runner.notes[-1]["scan"]["rejects"]
    assert "under the gate" in rejects["DOWN"]
    assert "under the gate" in rejects["MEHH"]


@pytest.mark.asyncio
async def test_reaction_day_is_not_day2():
    # Reported yesterday: zero sessions since -> today is the REACTION day.
    reporters = {sess(1): [amc("EARL", 1)]}
    closes = {"EARL": [{"date": sess(3), "close": 100.0, "volume": 1e7},
                       {"date": sess(1), "close": 100.0, "volume": 1e7}]}
    strat, ctx, runner, _ = make(reporters, closes, {"EARL": 100.0})
    await strat._scan(ctx)
    assert strat._cands == []
    assert "0 sessions" in runner.notes[-1]["scan"]["rejects"]["EARL"]


@pytest.mark.asyncio
async def test_slots_rank_by_dollar_volume():
    reporters = {sess(2): [amc(s, 2) for s in ("AA", "BB", "CC", "DD", "EE")]}
    closes = {s: closes_for(2, 100.0, 110.0) for s in reporters[sess(2)][0] and
              ("AA", "BB", "CC", "DD", "EE")}
    dv = {"AA": 9e8, "BB": 8e8, "CC": 7e8, "DD": 6e8, "EE": 5e8}
    strat, ctx, runner, _ = make(reporters, closes,
                                 {s: 110.0 for s in dv}, dv=dv)
    await strat._scan(ctx)
    assert [c["symbol"] for c in strat._cands] == ["AA", "BB", "CC", "DD"]
    assert runner.notes[-1]["scan"]["overflow"] == ["EE"]


@pytest.mark.asyncio
async def test_dv_floor_and_calendar_dark():
    reporters = {sess(2): [amc("THIN", 2)]}
    closes = {"THIN": closes_for(2, 100.0, 110.0)}
    strat, ctx, runner, _ = make(reporters, closes, {"THIN": 110.0},
                                 dv={"THIN": 1e7})     # $10M: under the floor
    await strat._scan(ctx)
    assert strat._cands == []

    strat2, ctx2, runner2, _ = make({}, {}, {})
    await strat2._scan(ctx2)
    assert "calendar dark" in runner2.notes[-1]["skip"]


def test_params_and_registry():
    assert REGISTRY["day2_pop"] is Day2Pop
    assert Day2Pop.registered["metric"] == "net_bp_per_trade"
    with pytest.raises(ValueError):
        Day2Pop.validate_params({"min_move_pct": 0.5})
    with pytest.raises(ValueError):
        Day2Pop.validate_params({"slots": 9})
    with pytest.raises(ValueError):
        Day2Pop.validate_params({"nonsense": 1})
