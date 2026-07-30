"""Keyless fallback feed: Yahoo chart parsing + public/synthetic selection."""

import asyncio

import pytest

from app.services.demo_feed import DemoFeed, parse_yahoo_chart, synth_history


def _payload(closes, meta_price=740.86, null_at=()):
    stamps = [1_753_700_000 + 60 * i for i in range(len(closes))]
    col = lambda vals: [None if i in null_at else v for i, v in enumerate(vals)]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": meta_price},
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": col(closes),
                                "high": col([c + 0.1 for c in closes]),
                                "low": col([c - 0.1 for c in closes]),
                                "close": col(closes),
                                "volume": col([1000] * len(closes)),
                            }
                        ]
                    },
                }
            ]
        }
    }


class TestParseYahooChart:
    def test_parses_bars_and_last_price(self):
        bars, last = parse_yahoo_chart(_payload([740.0, 740.5, 741.0]))
        assert len(bars) == 3
        assert bars[0]["o"] == 740.0 and bars[-1]["c"] == 741.0
        assert bars[0]["t"] == 1_753_700_000 * 1000
        assert bars[1]["t"] - bars[0]["t"] == 60_000
        assert last == 740.86

    def test_skips_null_rows(self):
        bars, _ = parse_yahoo_chart(_payload([740.0, 740.5, 741.0], null_at=(1,)))
        assert len(bars) == 2
        assert [b["c"] for b in bars] == [740.0, 741.0]

    def test_last_price_falls_back_to_final_close(self):
        bars, last = parse_yahoo_chart(_payload([740.0, 741.5], meta_price=None))
        assert last == 741.5

    def test_empty_payload(self):
        assert parse_yahoo_chart({}) == ([], None)
        assert parse_yahoo_chart({"chart": {"result": []}}) == ([], None)


class _FakeBars:
    def __init__(self):
        self.inserted: list[dict] = []

    async def bulk_insert_1m(self, symbol, bars):
        self.inserted.extend(bars)

    async def upsert_1m(self, symbol, bar):
        return []


class _FakeBroadcast:
    def publish(self, topic, msg):
        pass


class _FakeMarket:
    def __init__(self):
        self.bars = _FakeBars()
        self.broadcast = _FakeBroadcast()
        self._latest_quotes: dict[str, dict] = {}


@pytest.mark.asyncio
async def test_ensure_uses_public_feed_when_reachable(monkeypatch):
    market = _FakeMarket()
    feed = DemoFeed(market)

    async def fake_fetch(symbol, range_):
        return [{"t": 1, "o": 740.0, "h": 740.2, "l": 739.8, "c": 740.1, "v": 100}], 740.1

    monkeypatch.setattr(feed, "_fetch_chart", fake_fetch)
    await feed.ensure("SPY")
    try:
        assert feed.sources == {"SPY": "public"}
        assert market.bars.inserted and market.bars.inserted[-1]["c"] == 740.1
        assert feed._prices["SPY"] == 740.1
    finally:
        await feed.stop()


@pytest.mark.asyncio
async def test_ensure_falls_back_to_synthetic_when_unreachable(monkeypatch):
    market = _FakeMarket()
    feed = DemoFeed(market)

    async def fake_fetch(symbol, range_):
        raise OSError("network down")

    monkeypatch.setattr(feed, "_fetch_chart", fake_fetch)
    await feed.ensure("SPY")
    try:
        assert feed.sources == {"SPY": "synthetic"}
        assert len(market.bars.inserted) > 100  # synthetic history generated
    finally:
        await feed.stop()


def test_synth_history_ends_today():
    bars = synth_history("SPY", days=2)
    assert bars, "synthetic history must not be empty"
    import time as _time

    assert bars[-1]["t"] <= _time.time() * 1000


@pytest.mark.asyncio
async def test_demo_option_quotes_synthesized_for_exit_monitoring():
    """Keyless mode must still give the exit enforcer option quotes —
    synthesized BSM premiums off the public-feed spot."""
    market = _FakeMarket()
    market.bars.series = [{"t": 1, "o": 450, "h": 451, "l": 449, "c": 450.0, "v": 1}]
    market.published: list = []
    market.broadcast.publish = lambda topic, msg: market.published.append((topic, msg))
    market.bars.get_bars = lambda symbol, tf: market.bars.series
    feed = DemoFeed(market)

    feed.publish_option_quotes(["SPY270115C00450000", "not-an-occ"])
    quote = market._latest_quotes.get("SPY270115C00450000")
    assert quote is not None, "no synthesized quote"
    assert quote["mid"] > 0
    assert 0 <= quote["bid"] < quote["ask"]
    assert market.published and market.published[0][0] == "oquote:SPY270115C00450000"
    # Non-OCC symbols are skipped, never crash.
    assert "not-an-occ" not in market._latest_quotes
