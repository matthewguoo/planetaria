"""Afternoon fly: entry construction, quote guards, sizing, the session
gate, and the once-per-day latch. The engine-side collateral math is
covered in test_options_collateral.py; these tests cover THIS strategy's
own logic against a faked market."""

import logging
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.signals.events import Event
from app.strategies.afternoon_fly import AfternoonFly, occ_symbol
from app.strategies.base import StrategyContext
from tests.conftest import FakeRunner

ET = ZoneInfo("America/New_York")
MONDAY_2PM = datetime(2026, 8, 10, 14, 0, tzinfo=ET)


class FakeMarket:
    def __init__(self, spot=(559.9, 560.1), quotes=None):
        self.spot = spot
        self.quotes = quotes or {}
        self.subscribed: list[str] = []
        self.refreshed: list[str] = []

    async def fetch_latest_stock_quote(self, symbol):
        bid, ask = self.spot
        return {"bid": bid, "ask": ask, "ts": _time.time() * 1000}

    async def subscribe_options(self, symbols):
        self.subscribed += list(symbols)

    async def refresh_option_quotes(self, symbols, max_age_s=30.0):
        self.refreshed += list(symbols)

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)


def default_quotes(day):
    def q(right, k, bid, ask):
        return occ_symbol("QQQ", day, right, k), {"bid": bid, "ask": ask}
    return dict([
        q("C", 560, 1.55, 1.65),
        q("P", 560, 1.25, 1.35),
        q("C", 566, 0.30, 0.40),
        q("P", 554, 0.25, 0.35),
    ])


def make_ctx(quotes=None, params=None):
    runner = FakeRunner()
    market = FakeMarket(quotes=quotes)
    ctx = StrategyContext(
        market=market, clock=None,
        params=AfternoonFly.validate_params(params or {}),
        log=logging.getLogger("test"), _runner=runner, _instance_id="t",
    )
    return ctx, runner, market


def tick(now_et, session="rth"):
    return Event(type="timer", ts=now_et.astimezone(tz=None), source="timer",
                 key=None, symbols=(),
                 payload={"kind": "tick", "session": session,
                          "et": now_et.isoformat()})


@pytest.mark.asyncio
async def test_note_mode_builds_the_measured_structure():
    ctx, runner, market = make_ctx(default_quotes(MONDAY_2PM.date()))
    await AfternoonFly().on_event(tick(MONDAY_2PM), ctx)
    # note-mode submits too: the runner routes live=false to a simulated
    # fill (the self-paper tier); the note keeps the would_trade key.
    (intent,) = runner.intents
    assert intent.qty == 10 and intent.tp is None
    (note,) = runner.notes
    d = note["would_trade"]
    assert d["strike"] == 560 and d["width"] == 6
    # credit = (1.60 + 1.30) - (0.35 + 0.30); limit gives up 2c of it
    assert d["credit"] == 2.25 and d["entry_limit"] == -2.23
    # margin (6 - 2.23) x 100 = 377; budget 10k x 0.5 -> 13 sets, capped at 10
    assert d["margin_per_set"] == 377.0 and d["sets"] == 10
    assert len(market.subscribed) == 4


@pytest.mark.asyncio
async def test_live_submits_a_time_stop_only_defined_risk_intent():
    ctx, runner, _ = make_ctx(default_quotes(MONDAY_2PM.date()),
                              params={"live": True})
    await AfternoonFly().on_event(tick(MONDAY_2PM), ctx)
    (intent,) = runner.intents
    assert intent.asset_class == "option" and intent.qty == 10
    assert intent.tp is None and intent.sl is None
    assert intent.dedupe_key == "fly:QQQ:2026-08-10"
    shorts = [leg for leg in intent.legs if leg["side"] < 0]
    longs = [leg for leg in intent.legs if leg["side"] > 0]
    assert len(shorts) == 2 and len(longs) == 2    # defined-risk by shape
    exit_et = intent.time_stop_utc.astimezone(ET)
    assert exit_et.strftime("%H:%M") == "15:50"


@pytest.mark.asyncio
async def test_wide_leg_is_refused():
    quotes = default_quotes(MONDAY_2PM.date())
    sym = occ_symbol("QQQ", MONDAY_2PM.date(), "C", 566)
    quotes[sym] = {"bid": 0.20, "ask": 0.45}       # 25c wide wing
    ctx, runner, _ = make_ctx(quotes)
    await AfternoonFly().on_event(tick(MONDAY_2PM), ctx)
    assert not runner.intents
    (note,) = runner.notes
    assert "too wide" in note["skip"]


@pytest.mark.asyncio
async def test_missing_leg_book_is_refused():
    quotes = default_quotes(MONDAY_2PM.date())
    del quotes[occ_symbol("QQQ", MONDAY_2PM.date(), "P", 554)]
    ctx, runner, _ = make_ctx(quotes)
    await AfternoonFly().on_event(tick(MONDAY_2PM), ctx)
    assert not runner.intents and "no two-sided book" in runner.notes[0]["skip"]


@pytest.mark.asyncio
async def test_absurd_credit_is_refused():
    quotes = default_quotes(MONDAY_2PM.date())
    quotes[occ_symbol("QQQ", MONDAY_2PM.date(), "C", 560)] = {
        "bid": 5.60, "ask": 5.70}                  # credit ~ its whole width
    ctx, runner, _ = make_ctx(quotes)
    await AfternoonFly().on_event(tick(MONDAY_2PM), ctx)
    assert not runner.intents and "outside" in runner.notes[0]["skip"]


@pytest.mark.asyncio
async def test_only_fires_in_rth_at_the_entry_minute():
    ctx, runner, _ = make_ctx(default_quotes(MONDAY_2PM.date()))
    fly = AfternoonFly()
    await fly.on_event(tick(MONDAY_2PM, session="premarket"), ctx)
    await fly.on_event(tick(MONDAY_2PM.replace(hour=13)), ctx)
    assert not runner.notes
    await fly.on_event(tick(MONDAY_2PM), ctx)
    await fly.on_event(tick(MONDAY_2PM), ctx)      # same-day latch
    assert len(runner.notes) == 1


def test_param_validation():
    with pytest.raises(ValueError):
        AfternoonFly.validate_params({"width_pct": 0.0})
    with pytest.raises(ValueError):
        AfternoonFly.validate_params({"entry_et": "16:00", "exit_et": "15:50"})
    with pytest.raises(ValueError):
        AfternoonFly.validate_params({"max_margin_frac": 2.0})


@pytest.mark.asyncio
async def test_on_start_feeds_the_stock_stream():
    """2026-08-11: options plans face the GLOBAL stream-age gate, and on a
    day when nothing else subscribes a share symbol it reads "no ticks
    received yet" at 14:00. The fly subscribes its own underlying."""
    ctx, _runner, market = make_ctx()
    market.stock_subs = []

    async def sub(sym):
        market.stock_subs.append(sym)

    async def unsub(sym):
        market.stock_subs.remove(sym)

    market.subscribe_stock = sub
    market.unsubscribe_stock = unsub
    fly = AfternoonFly()
    await fly.on_start(ctx)
    assert market.stock_subs == ["QQQ"]
    await fly.on_stop(ctx)
    assert market.stock_subs == []
