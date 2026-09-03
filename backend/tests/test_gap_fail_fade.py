"""gap_fail_fade: the scan->snapshot->decide clock, the failed-gap
qualification, the auction leg, and the OPG plumbing it rides on. The
engine-side guards (allocation, shorts, collateral) are tested where they
live; these tests cover THIS strategy's own logic."""

import logging
import time as _time
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.trade_service import TradeService, auction_submit_ok
from app.strategies import REGISTRY
from app.strategies.base import StrategyContext
from app.strategies.gap_fail_fade import GapFailFade
from tests.conftest import FakeRunner

ET = ZoneInfo("America/New_York")


class FakeMarket:
    """Movers + quotes with a settable phase price per symbol. `prints`
    symbols price like ah_quotes externals (bid == ask == last, src tag);
    `dark` symbols have no quote at all — the Amendment 1 seam."""

    def __init__(self, movers, px, prints=()):
        self.movers = movers
        self.px = px            # symbol -> current price (mutable per phase)
        self.prints = set(prints)
        self.dark = set()

    async def premarket_movers(self, top=50):
        return self.movers

    async def daily_dollar_volumes(self, symbols):
        return {s: 5e8 for s in symbols}

    async def ah_quote(self, symbol, max_age_s=120.0):
        if symbol in self.dark:
            return None
        p = self.px[symbol]
        if symbol in self.prints:
            return {"bid": p, "ask": p, "ts": _time.time() * 1000,
                    "src": "yahoo"}
        return {"bid": p - 0.02, "ask": p + 0.02, "ts": _time.time() * 1000}


def make(movers, px, params=None, prints=()):
    strat = GapFailFade()
    runner = FakeRunner()
    market = FakeMarket(movers, px, prints=prints)
    ctx = StrategyContext(
        market=market, clock=None,
        params=GapFailFade.validate_params(params or {}),
        log=logging.getLogger("test"), _runner=runner, _instance_id="t",
    )
    return strat, ctx, runner, market


def mover(sym, pct, price):
    return {"symbol": sym, "pct": pct, "price": price,
            "prior_close": price / (1 + pct)}


@pytest.mark.asyncio
async def test_failed_gap_down_goes_long_at_the_auction():
    # Gaps -3% (price 97 vs prior 100), premarket recovers 96.5 -> 97.3.
    movers = [mover("GAPD", -0.03, 97.0)]
    strat, ctx, runner, market = make(movers, {"GAPD": 96.5})
    await strat._scan(ctx)
    await strat._snapshot(ctx)
    market.px["GAPD"] = 97.3
    await strat._decide(ctx)
    note = runner.notes[-1]
    assert "would_trade" in note
    q = note["would_trade"]
    assert q["side"] == 1 and q["qty"] == int(2500 // 97.3)
    assert q["turn_bp"] >= 20 and q["gap_pct"] <= -1.5
    (intent,) = runner.intents           # note-mode submits (sim routing)
    assert intent.legs[0]["auction"] == "open"


@pytest.mark.asyncio
async def test_short_leg_stands_down_by_default_and_fires_when_enabled():
    movers = [mover("GAPU", 0.03, 103.0)]         # +3% gap, premarket fading
    strat, ctx, runner, market = make(movers, {"GAPU": 103.5})
    await strat._scan(ctx)
    await strat._snapshot(ctx)
    market.px["GAPU"] = 103.0                     # -48bp turn against the gap
    await strat._decide(ctx)
    note = runner.notes[-1]
    assert note.get("stood_down_shorts") == ["GAPU"] or \
        note.get("no_trade", {}).get("stood_down_shorts") == ["GAPU"]

    strat2, ctx2, runner2, market2 = make(
        [mover("GAPU", 0.03, 103.0)], {"GAPU": 103.5},
        params={"enable_short_leg": True, "live": True})
    await strat2._scan(ctx2)
    await strat2._snapshot(ctx2)
    market2.px["GAPU"] = 103.0
    await strat2._decide(ctx2)
    (intent,) = runner2.intents
    assert intent.legs[0]["side"] == -1
    assert intent.legs[0]["auction"] == "open"
    assert intent.tp is None and intent.sl is None
    exit_et = intent.time_stop_utc.astimezone(ET)
    assert exit_et.strftime("%H:%M:%S") == "09:31:30"


@pytest.mark.asyncio
async def test_aligned_gap_and_small_turn_are_skipped():
    movers = [mover("ALGN", -0.03, 97.0), mover("SMLL", -0.03, 97.0)]
    strat, ctx, runner, market = make(movers, {"ALGN": 97.5, "SMLL": 97.0})
    await strat._scan(ctx)
    await strat._snapshot(ctx)
    market.px["ALGN"] = 96.9        # still falling — aligned, not failing
    market.px["SMLL"] = 97.01       # +1bp turn — under the gate
    await strat._decide(ctx)
    note = runner.notes[-1]
    assert "no_trade" in note
    sk = note["no_trade"]["skipped"]
    assert "not failing" in sk["ALGN"] and "not failing" in sk["SMLL"]


@pytest.mark.asyncio
async def test_print_marks_qualify_and_carry_provenance():
    """Amendment 1: IEX dark -> consolidated prints price the marks. The
    print's bid==ask must journal a null spread (not a fake zero) and its
    source must be visible in the record."""
    movers = [mover("THIN", -0.03, 97.0)]
    strat, ctx, runner, market = make(movers, {"THIN": 96.5},
                                      prints=["THIN"])
    await strat._scan(ctx)
    await strat._snapshot(ctx)
    snap = runner.notes[-1]
    assert snap["snapshot_0915"]["THIN"] == 96.5
    assert snap["src"]["THIN"] == "yahoo" and snap["unusable"] == {}
    market.px["THIN"] = 97.3
    await strat._decide(ctx)
    q = runner.notes[-1]["would_trade"]
    assert q["px_src"] == "yahoo"
    assert q["spread_pct"] is None
    (intent,) = runner.intents
    assert intent.legs[0]["auction"] == "open"


@pytest.mark.asyncio
async def test_dark_candidate_journals_why_not_silence():
    """A candidate with no quote from ANY source lands in `unusable` — the
    coverage stopping rule's raw data — and is skipped at decide."""
    movers = [mover("DARK", -0.03, 97.0), mover("GAPD", -0.03, 97.0)]
    strat, ctx, runner, market = make(movers, {"DARK": 96.5, "GAPD": 96.5})
    market.dark.add("DARK")
    await strat._scan(ctx)
    await strat._snapshot(ctx)
    snap = runner.notes[-1]
    assert snap["unusable"]["DARK"] == "no quote"
    assert "GAPD" in snap["snapshot_0915"]
    market.px["GAPD"] = 97.3
    await strat._decide(ctx)
    note = runner.notes[-1]
    assert note["would_trade"]["symbol"] == "GAPD"


def test_params_and_registry():
    assert REGISTRY["gap_fail_fade"] is GapFailFade
    with pytest.raises(ValueError):
        GapFailFade.validate_params({"min_gap_pct": 0.1})
    with pytest.raises(ValueError):
        GapFailFade.validate_params({"position_frac": 0.9})


def test_auction_window():
    def at(h, m):
        return datetime(2026, 8, 10, h, m, tzinfo=ET).astimezone(timezone.utc)
    assert auction_submit_ok(at(9, 27))
    assert auction_submit_ok(at(7, 0))
    assert auction_submit_ok(at(18, 0))           # queues for tomorrow
    assert not auction_submit_ok(at(9, 28))
    assert not auction_submit_ok(at(12, 0))


@pytest.mark.asyncio
async def test_submit_entry_builds_an_opg_market_order():
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    from app.models.trade import TradePlan

    captured = {}

    async def capture(request, client_order_id):
        captured["request"] = request
        return types.SimpleNamespace(id="order-1")

    fake = types.SimpleNamespace(_submit_idempotent=capture)
    plan = TradePlan(
        underlying="GAPD", strategy="gff", asset_class="equity",
        extended_hours=False, qty=25, entry_limit=97.3,
        legs=[{"symbol": "GAPD", "side": 1, "ratio": 1, "entry": 97.3,
               "auction": "open"}],
    )
    await TradeService._submit_entry(fake, plan)
    req = captured["request"]
    assert isinstance(req, MarketOrderRequest)
    assert req.time_in_force == TimeInForce.OPG
    assert req.qty == 25 and req.symbol == "GAPD"
