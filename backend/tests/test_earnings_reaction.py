"""earnings_reaction through the real runner: estimate accumulation ->
15:30 watchlist freeze -> release event -> ctx.analyze (fake client) ->
agreement gate -> would_trade/no_trade note with full provenance. Note-mode
throughout: trade=None proves nothing can reach place_trade."""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.llm import LLMAnalyst
from app.services.risk import RiskService
from app.services.signals.events import Event, EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner
from app.strategies.earnings_reaction import _exit_time
from tests.test_llm_analyze import FakeAnthropic, ok_response

ET = ZoneInfo("America/New_York")

SETTINGS = SimpleNamespace(strategies_enabled=True, anthropic_api_key="k",
                           llm_model="claude-opus-5", llm_effort="low")


def verdict_json(direction="bullish", confidence="high") -> str:
    return json.dumps({
        "eps_vs_consensus": "beat", "revenue_vs_consensus": "beat",
        "guidance": "raised", "quality_flags": [],
        "direction": direction, "confidence": confidence,
        "summary": "test verdict",
    })


class FakeMarket:
    def __init__(self):
        self.prices: dict[str, tuple[float, float]] = {}
        self.daily: dict[str, list[dict]] = {}

    async def overnight_price(self, symbol: str) -> dict | None:
        px = self.prices.get(symbol)
        if px is None:
            return None
        return {"bid": px[0], "ask": px[1], "ts": time.time() * 1000}

    async def daily_closes(self, symbol: str, days: int = 6) -> list[dict]:
        return self.daily.get(symbol, [])[-days:]

    async def daily_dollar_volumes(self, symbols: list[str]) -> dict[str, float]:
        out = {}
        for sym in symbols:
            bars = self.daily.get(sym)
            if bars:
                out[sym] = bars[-1]["close"] * bars[-1]["volume"]
        return out


@pytest_asyncio.fixture
async def rig(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/earn.db")
    bus = EventBus()
    store = SignalStore(db)
    fake = FakeAnthropic(ok_response(verdict_json()))
    market = FakeMarket()
    runner = StrategyRunner(
        db, bus, store, trade=None, risk=RiskService(db), market=market,
        clock=None, settings=SETTINGS,
        llm=LLMAnalyst(SETTINGS, store, client=fake),
    )
    yield SimpleNamespace(db=db, bus=bus, store=store, runner=runner,
                          fake=fake, market=market)
    await runner.shutdown()
    await db.close()


async def wait_for(predicate, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def wait_note(rig, row_id, key, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rows = await rig.runner.decisions(row_id)
        note = next((r for r in rows if r["action"] == "note"
                     and key in (r["detail"] or {})), None)
        if note:
            return note
        await asyncio.sleep(0.02)
    return None


def today_et() -> str:
    return datetime.now(ET).date().isoformat()


async def publish_estimate(rig, symbol, when="amc", eps=1.61):
    event = Event(
        type="estimate", ts=datetime.now(timezone.utc), source="earnings-cal",
        key=f"cal:{symbol}:{today_et()}", symbols=(symbol,),
        payload={"symbol": symbol, "date": today_et(), "when": when,
                 "eps_consensus": eps, "revenue_consensus": 7_400_000_000,
                 "fiscal_period": "Q2 2026"},
    )
    journaled, _ = await rig.store.record(event)
    rig.bus.publish(journaled)
    return journaled


def publish_tick(rig, hhmm="15:30"):
    rig.bus.publish(Event(
        type="timer", ts=datetime.now(timezone.utc), source="timer", key=None,
        symbols=(), payload={"kind": "tick", "session": "rth",
                             "et": f"{today_et()}T{hhmm}:00-04:00"},
    ))


async def publish_release(rig, symbol, source="edgar-8k", headline=None,
                          text="Revenue $7.7B up 32%, EPS $1.66 vs $1.61.",
                          symbols=None):
    payload = {"headline": headline or f"{symbol} 8-K results",
               "item_codes": ["2.02"], "url": "https://x"}
    if source == "edgar-8k":
        payload["text"] = text
    else:
        payload["summary"] = text
    event = Event(
        type="news", ts=datetime.now(timezone.utc), source=source,
        key=f"{symbol}-{time.monotonic_ns()}", symbols=symbols or (symbol,),
        payload=payload,
    )
    journaled, _ = await rig.store.record(event)
    rig.bus.publish(journaled)
    return journaled


async def armed_instance(rig, params=None):
    """Create+enable an instance, feed it AMD estimate, freeze watchlist.
    Daily context: prior close 490, +8.89% run over the 5 prior sessions."""
    row = await rig.runner.create("earnings_reaction", "earn", params or {})
    await rig.runner.set_state(row["id"], "enabled")
    rig.market.prices["AMD"] = (500.0, 500.5)
    rig.market.daily["AMD"] = [
        {"date": f"2026-07-{27 + i}", "close": close, "volume": 1e6}
        for i, close in enumerate([450.0, 452.0, 470.0, 465.0, 480.0, 490.0])
    ]
    est = await publish_estimate(rig, "AMD")
    publish_tick(rig)
    note = await wait_note(rig, row["id"], "watchlist")
    assert note, "watchlist never froze"
    assert "AMD" in note["detail"]["watchlist"]
    assert note["detail"]["watchlist"]["AMD"]["px0"] == 500.25
    return row, est, note


@pytest.mark.asyncio
class TestNightFlow:
    async def test_agreement_long_journals_would_trade(self, rig):
        row, est, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)  # tape +2.0%
        release = await publish_release(rig, "AMD")

        note = await wait_note(rig, row["id"], "would_trade")
        assert note, "no would_trade note"
        wt = note["detail"]["would_trade"]
        assert wt["side"] == 1
        assert wt["move_pct"] == pytest.approx(2.0, abs=0.05)
        assert wt["intent"]["dedupe_key"] == f"earn:AMD:{today_et()}"
        assert wt["intent"]["qty"] == 1  # 1000 notional // 510
        assert wt["intent"]["extended_hours"] is True

        # Consensus AND before-close setup reached the model; text was the
        # data payload. px0 500.25 vs prior close 490 = +2.09% day move;
        # 490 vs 450 five sessions back = +8.89% run-up.
        call = rig.fake.calls[0]
        content = call["messages"][0]["content"]
        assert "1.61" in content
        assert "Revenue $7.7B" in content
        assert "+2.1% today into the print" in content
        assert "+8.9% over the prior 5 sessions" in content
        assert "priced in" in content
        assert wt["day_move_pct"] == pytest.approx(2.09, abs=0.01)
        assert wt["run5d_pct"] == pytest.approx(8.89, abs=0.01)

        # Provenance: news + estimate + analysis ids all on the note; the
        # analysis row chains to the NEWS event.
        analysis = (await rig.store.recent(type="analysis"))[0]
        assert analysis["parent_id"] == release.signal_id
        assert set(note["signal_ids"]) == {release.signal_id, est.signal_id,
                                           analysis["id"]}

    async def test_disagreement_journals_no_trade(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (490.0, 490.5)  # tape -2% vs bullish verdict
        await publish_release(rig, "AMD")
        note = await wait_note(rig, row["id"], "no_trade")
        assert note and "disagree" in note["detail"]["why"]

    async def test_bearish_agreement_notes_would_short(self, rig):
        rig.fake.response = ok_response(verdict_json(direction="bearish"))
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (490.0, 490.5)
        await publish_release(rig, "AMD")
        note = await wait_note(rig, row["id"], "would_trade")
        assert note and note["detail"]["would_trade"]["side"] == -1

    async def test_one_analysis_per_symbol_per_night(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)
        await publish_release(rig, "AMD")
        assert await wait_note(rig, row["id"], "would_trade")
        await publish_release(rig, "AMD")  # the burst twin
        await asyncio.sleep(0.3)
        assert len(rig.fake.calls) == 1

    async def test_analysis_failure_lets_other_source_retry(self, rig):
        rig.fake.response = RuntimeError("api down")
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)
        await publish_release(rig, "AMD")
        assert await wait_for(lambda: len(rig.fake.calls) == 1)
        rig.fake.response = ok_response(verdict_json())
        await publish_release(rig, "AMD")
        note = await wait_note(rig, row["id"], "would_trade")
        assert note and len(rig.fake.calls) == 2


@pytest.mark.asyncio
class TestReanchor:
    async def test_reanchor_moves_baseline_before_release(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (502.0, 502.5)  # +0.45% auction drift
        publish_tick(rig, "16:04")
        note = await wait_note(rig, row["id"], "reanchor")
        assert note["detail"]["reanchor"]["AMD"]["px0"] == 502.25
        assert note["detail"]["reanchor"]["AMD"]["was"] == 500.25

        rig.market.prices["AMD"] = (512.3, 512.8)  # +2.0% vs NEW anchor
        await publish_release(rig, "AMD")
        wt = (await wait_note(rig, row["id"], "would_trade"))["detail"]["would_trade"]
        assert wt["px0"] == 502.25
        assert wt["move_pct"] == pytest.approx(2.0, abs=0.05)

    async def test_reanchor_leaves_inflight_release_alone(self, rig):
        """A name already past min_move_pct at 16:04 is a release our
        sources have not delivered yet - erasing that move would erase the
        signal."""
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)  # +2% already
        publish_tick(rig, "16:04")
        note = await wait_note(rig, row["id"], "reanchor")
        assert note["detail"]["reanchor"] == {}
        assert "release in flight" in note["detail"]["skipped"]["AMD"]
        await publish_release(rig, "AMD")
        wt = (await wait_note(rig, row["id"], "would_trade"))["detail"]["would_trade"]
        assert wt["px0"] == 500.25  # original anchor kept

    async def test_reanchor_skips_decided(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)
        await publish_release(rig, "AMD")
        assert await wait_note(rig, row["id"], "would_trade")
        publish_tick(rig, "16:04")
        note = await wait_note(rig, row["id"], "reanchor")
        assert note["detail"]["skipped"]["AMD"] == "decided"

    async def test_missing_daily_context_never_blocks(self, rig):
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        rig.market.prices["AMD"] = (500.0, 500.5)  # no daily data set
        await publish_estimate(rig, "AMD")
        publish_tick(rig)
        note = await wait_note(rig, row["id"], "watchlist")
        assert note["detail"]["watchlist"]["AMD"]["day_move_pct"] is None
        rig.market.prices["AMD"] = (510.0, 510.5)
        await publish_release(rig, "AMD")
        assert await wait_note(rig, row["id"], "would_trade")
        assert "Setup into the print" not in rig.fake.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
class TestEligibility:
    async def test_headline_gate_on_alpaca_news(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["AMD"] = (510.0, 510.5)
        # Roundup story (multi-symbol): ignored.
        await publish_release(rig, "AMD", source="alpaca-news",
                              headline="Movers: AMD, NVDA after Q2 EPS $1.66",
                              symbols=("AMD", "NVDA"))
        # Single-symbol but no numbers: ignored.
        await publish_release(rig, "AMD", source="alpaca-news",
                              headline="AMD CEO to speak at conference")
        await asyncio.sleep(0.3)
        assert rig.fake.calls == []
        # Single-symbol numbers headline: analyzed.
        await publish_release(rig, "AMD", source="alpaca-news",
                              headline="AMD Q2 EPS $1.66 Beats $1.61 Estimate")
        assert await wait_note(rig, row["id"], "would_trade")

    async def test_unwatched_symbol_ignored(self, rig):
        row, _, _ = await armed_instance(rig)
        rig.market.prices["TSLA"] = (300.0, 300.5)
        await publish_release(rig, "TSLA")
        await asyncio.sleep(0.3)
        assert rig.fake.calls == []

    async def test_watchlist_price_floor(self, rig):
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        rig.market.prices["PENY"] = (3.0, 3.1)
        await publish_estimate(rig, "PENY")
        publish_tick(rig)
        note = await wait_note(rig, row["id"], "watchlist")
        assert note["detail"]["watchlist"] == {}
        assert note["detail"]["skipped"] == ["PENY"]

    async def test_watchlist_ranks_by_dollar_volume(self, rig):
        """269 AMC candidates on a real night: the big names must win the
        n_names slots, not the alphabet."""
        row = await rig.runner.create("earnings_reaction", "earn",
                                      {"n_names": 2})
        await rig.runner.set_state(row["id"], "enabled")
        for sym, px, vol in (("AAAA", 20.0, 1e5), ("DIS", 118.0, 8e6),
                             ("UBER", 92.0, 6e6), ("ZZZZ", 30.0, 2e5)):
            rig.market.prices[sym] = (px, px + 0.1)
            rig.market.daily["ZZZZ" if sym == "ZZZZ" else sym] = [
                {"date": "2026-08-04", "close": px, "volume": vol}]
            await publish_estimate(rig, sym)
        publish_tick(rig)
        note = await wait_note(rig, row["id"], "watchlist")
        assert set(note["detail"]["watchlist"]) == {"DIS", "UBER"}

    async def test_bmo_reporters_not_watched_tonight(self, rig):
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        rig.market.prices["SHOP"] = (60.0, 60.2)
        await publish_estimate(rig, "SHOP", when="bmo")
        publish_tick(rig)
        note = await wait_note(rig, row["id"], "watchlist")
        assert note["detail"]["watchlist"] == {}
        assert note["detail"]["amc_candidates"] == 0


@pytest.mark.asyncio
class TestManualDryRun:
    async def test_manual_build_watchlist_salvages_late_boot(self, rig):
        """Engine booted after 15:30: the command freezes the watchlist
        without a timer tick, and the normal decision path works after."""
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        rig.market.prices["AMD"] = (500.0, 500.5)
        rig.market.daily["AMD"] = [{"date": "2026-08-04", "close": 490.0,
                                    "volume": 1e6}]
        await publish_estimate(rig, "AMD")

        event = Event(type="manual", ts=datetime.now(timezone.utc),
                      source="api", key=None, symbols=(),
                      payload={"strategy_id": row["id"],
                               "cmd": "build_watchlist"})
        journaled, _ = await rig.store.record(event)
        rig.bus.publish(journaled)

        note = await wait_note(rig, row["id"], "watchlist")
        assert note["detail"]["watchlist"]["AMD"]["px0"] == 500.25

        rig.market.prices["AMD"] = (510.0, 510.5)
        await publish_release(rig, "AMD")
        assert await wait_note(rig, row["id"], "would_trade")

    async def test_unwatched_needs_px0(self, rig):
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        event = Event(type="manual", ts=datetime.now(timezone.utc),
                      source="api", key=None, symbols=(),
                      payload={"strategy_id": row["id"], "symbol": "NVDA",
                               "text": "beat"})
        journaled, _ = await rig.store.record(event)
        rig.bus.publish(journaled)
        note = await wait_note(rig, row["id"], "skip")
        assert note and "px0" in note["detail"]["skip"]

    async def test_dry_run_with_px0(self, rig):
        row = await rig.runner.create("earnings_reaction", "earn", {})
        await rig.runner.set_state(row["id"], "enabled")
        rig.market.prices["NVDA"] = (204.0, 204.2)
        event = Event(type="manual", ts=datetime.now(timezone.utc),
                      source="api", key=None, symbols=(),
                      payload={"strategy_id": row["id"], "symbol": "NVDA",
                               "text": "Q2 EPS $1.05 beats, guides up",
                               "px0": 200.0})
        journaled, _ = await rig.store.record(event)
        rig.bus.publish(journaled)
        note = await wait_note(rig, row["id"], "would_trade")
        assert note and note["detail"]["would_trade"]["side"] == 1


def test_validate_params():
    from app.strategies import REGISTRY

    cls = REGISTRY["earnings_reaction"]
    assert cls.validate_params({})["live"] is False
    with pytest.raises(ValueError):
        cls.validate_params({"hold": "T+5"})
    with pytest.raises(ValueError):
        cls.validate_params({"notional_per_name": 50_000})
    with pytest.raises(ValueError):
        cls.validate_params({"nope": 1})


def test_exit_time_skips_weekend():
    friday = datetime.fromisoformat("2026-08-07T16:30:00-04:00")
    t1 = _exit_time("T+1", friday).astimezone(ET)
    assert t1.isoformat().startswith("2026-08-10T15:55")  # Monday
    thursday = datetime.fromisoformat("2026-08-06T16:30:00-04:00")
    t2 = _exit_time("T+2", thursday).astimezone(ET)
    assert t2.isoformat().startswith("2026-08-10T15:55")  # Fri + Mon
