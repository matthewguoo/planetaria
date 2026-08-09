"""The external AH print sources: parsers, source order, cache discipline.

The feed's one job is honest provenance — a print with a real timestamp and a
`src` tag, or None. Silence (a dead source, garbage JSON, a stale tail) must
degrade to None or an old timestamp the downstream age gates refuse, never to
an invented fresh price.
"""

import time
from types import SimpleNamespace

import httpx
import pytest

from app.services.ah_quotes import (
    AhQuoteFeed,
    parse_finnhub_quote,
    parse_yahoo_chart,
)


def _yahoo_doc(post_px=None, post_ts=None, series=(), reg_px=None, reg_ts=None):
    stamps = [s for s, _ in series]
    closes = [c for _, c in series]
    meta = {}
    if post_px is not None:
        meta["postMarketPrice"], meta["postMarketTime"] = post_px, post_ts
    if reg_px is not None:
        meta["regularMarketPrice"], meta["regularMarketTime"] = reg_px, reg_ts
    return {"chart": {"result": [{
        "meta": meta,
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


class TestYahooParse:
    def test_prefers_the_freshest_source_in_the_document(self):
        now = int(time.time())
        doc = _yahoo_doc(post_px=101.5, post_ts=now,
                         series=[(now - 300, 100.9), (now - 240, 101.0)])
        q = parse_yahoo_chart("ABCD", doc)
        assert q["mid"] == 101.5 and q["src"] == "yahoo"
        assert q["ts"] == now * 1000

    def test_series_tail_when_meta_has_no_post_fields(self):
        now = int(time.time())
        doc = _yahoo_doc(series=[(now - 120, 55.0), (now - 60, None),
                                 (now - 55, 55.4)],
                         reg_px=54.0, reg_ts=now - 3600)
        q = parse_yahoo_chart("ABCD", doc)
        assert q["mid"] == 55.4, "last NON-NULL close, not the null"
        assert q["ts"] == (now - 55) * 1000

    def test_a_print_is_bid_equals_ask(self):
        """Zero spread is 'no book information', the engine's overnight
        convention — the spread veto must see nothing to veto."""
        now = int(time.time())
        q = parse_yahoo_chart("ABCD", _yahoo_doc(post_px=10.0, post_ts=now))
        assert q["bid"] == q["ask"] == q["mid"] == 10.0

    def test_garbage_degrades_to_none(self):
        assert parse_yahoo_chart("ABCD", {}) is None
        assert parse_yahoo_chart("ABCD", {"chart": {"result": [None]}}) is None
        assert parse_yahoo_chart(
            "ABCD", _yahoo_doc(post_px=0, post_ts=0)) is None


class TestFinnhubParse:
    def test_last_trade_shape(self):
        now = int(time.time())
        q = parse_finnhub_quote("ABCD", {"c": 12.34, "t": now})
        assert q["mid"] == 12.34 and q["src"] == "finnhub"
        assert q["ts"] == now * 1000

    def test_zeros_mean_no_data(self):
        assert parse_finnhub_quote("ABCD", {"c": 0, "t": 0}) is None
        assert parse_finnhub_quote("ABCD", {}) is None


def _transport(yahoo_doc=None, finnhub_doc=None, counter=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(request.url.host)
        if "yahoo" in request.url.host:
            if yahoo_doc is None:
                return httpx.Response(500)
            return httpx.Response(200, json=yahoo_doc)
        if finnhub_doc is None:
            return httpx.Response(500)
        return httpx.Response(200, json=finnhub_doc)
    return httpx.MockTransport(handler)


def _feed(transport, finnhub_key="tok") -> AhQuoteFeed:
    return AhQuoteFeed(SimpleNamespace(finnhub_api_key=finnhub_key),
                       transport=transport)


class TestSourceOrder:
    @pytest.mark.asyncio
    async def test_fresh_yahoo_short_circuits_finnhub(self):
        now = int(time.time())
        calls: list[str] = []
        feed = _feed(_transport(
            yahoo_doc=_yahoo_doc(post_px=20.0, post_ts=now - 5),
            finnhub_doc={"c": 21.0, "t": now}, counter=calls))
        q = await feed.latest("ABCD")
        assert q["src"] == "yahoo" and q["mid"] == 20.0
        assert not any("finnhub" in h for h in calls)

    @pytest.mark.asyncio
    async def test_stale_yahoo_falls_through_and_fresher_wins(self):
        now = int(time.time())
        feed = _feed(_transport(
            yahoo_doc=_yahoo_doc(post_px=20.0, post_ts=now - 600),
            finnhub_doc={"c": 21.0, "t": now - 10}))
        q = await feed.latest("ABCD")
        assert q["src"] == "finnhub" and q["mid"] == 21.0

    @pytest.mark.asyncio
    async def test_dead_sources_degrade_to_none(self):
        feed = _feed(_transport(yahoo_doc=None, finnhub_doc=None))
        assert await feed.latest("ABCD") is None

    @pytest.mark.asyncio
    async def test_no_finnhub_key_means_no_finnhub_call(self):
        now = int(time.time())
        calls: list[str] = []
        feed = _feed(_transport(
            yahoo_doc=_yahoo_doc(post_px=20.0, post_ts=now - 600),
            finnhub_doc={"c": 21.0, "t": now}, counter=calls),
            finnhub_key="")
        q = await feed.latest("ABCD")
        assert q["src"] == "yahoo", "stale yahoo is still the best we have"
        assert not any("finnhub" in h for h in calls)

    @pytest.mark.asyncio
    async def test_cache_absorbs_repeat_lookups(self):
        now = int(time.time())
        calls: list[str] = []
        feed = _feed(_transport(
            yahoo_doc=_yahoo_doc(post_px=20.0, post_ts=now),
            counter=calls))
        first = await feed.latest("ABCD")
        n_after_first = len(calls)
        second = await feed.latest("ABCD")
        assert second == first
        assert len(calls) == n_after_first, "second lookup served from cache"
