"""mech_carry: the tape decides, the open pays. Tests drive _decide_text
directly (the inherited watchlist/queue plumbing is the flagship's and is
tested there); what is THIS class's own is the verdictless decision, the
long-only gate, and the forced exit-at-open."""

import logging
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.strategies import REGISTRY
from app.strategies.base import StrategyContext
from app.strategies.mech_carry import MechCarry

ET = ZoneInfo("America/New_York")


class FakeMarket:
    def __init__(self, px=104.0):
        self.px = px

    async def overnight_price(self, sym):
        return {"bid": self.px - 0.02, "ask": self.px + 0.02,
                "ts": _time.time() * 1000}


class FakeRunner:
    def __init__(self):
        self.notes: list[dict] = []
        self.intents: list = []

    async def journal_note(self, _id, detail, signal_ids=()):
        self.notes.append(detail)

    async def execute_intent(self, _id, intent):
        self.intents.append(intent)
        return {"id": "plan-1"}

    async def account(self, _id):
        return {"equity": 10_000.0, "available": 10_000.0}


def make(px=104.0, params=None):
    strat = MechCarry()
    strat._watch = {"NVDA": {"est": {}, "px0": 100.0, "est_id": None,
                             "dv": 2e8}}
    strat._watch_date = datetime.now(ET).date().isoformat()
    runner = FakeRunner()
    ctx = StrategyContext(
        market=FakeMarket(px), clock=None,
        params=MechCarry.validate_params(params or {}),
        log=logging.getLogger("test"), _runner=runner, _instance_id="t",
    )
    return strat, ctx, runner


def test_registered():
    assert REGISTRY["mech_carry"] is MechCarry
    assert "llm" not in MechCarry.requires and "sip" in MechCarry.requires


@pytest.mark.asyncio
async def test_up_move_carries_to_the_open():
    strat, ctx, runner = make(px=104.0)          # +4% vs px0 100
    await strat._decide_text("NVDA", "", (), "edgar-8k", ctx)
    (note,) = runner.notes
    d = note["would_trade"]
    assert d["side"] == 1 and d["move_pct"] == 4.0
    exit_et = datetime.fromisoformat(
        d["intent"]["time_stop_utc"]).astimezone(ET)
    assert exit_et.strftime("%H:%M") == "09:31"
    assert exit_et.weekday() < 5
    assert d["intent"]["dedupe_key"].startswith("mcarry:NVDA:")
    assert d["intent"]["tp"] is None and d["intent"]["sl"] is None


@pytest.mark.asyncio
async def test_down_move_is_refused_not_shorted():
    strat, ctx, runner = make(px=95.0)           # -5%
    await strat._decide_text("NVDA", "", (), "edgar-8k", ctx)
    (note,) = runner.notes
    assert "no_trade" in note and "short" in note["why"]


@pytest.mark.asyncio
async def test_small_pop_is_below_the_gate():
    strat, ctx, runner = make(px=101.0)          # +1% < 2% gate
    await strat._decide_text("NVDA", "", (), "edgar-8k", ctx)
    (note,) = runner.notes
    assert "no_trade" in note and "below" in note["why"]


@pytest.mark.asyncio
async def test_live_submits_the_long():
    strat, ctx, runner = make(px=104.0, params={"live": True})
    await strat._decide_text("NVDA", "", (), "edgar-8k", ctx)
    (intent,) = runner.intents
    assert intent.asset_class == "equity"
    assert intent.legs[0]["side"] == 1
    assert intent.extended_hours is True
