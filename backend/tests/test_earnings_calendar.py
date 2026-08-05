"""EarningsCalendarFeed chaos coverage: journaling + at-least-once republish,
malformed reporters, auth failure idling, fetch scheduling."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.signals.earnings_calendar import (
    EarningsCalendarFeed,
    seconds_until_next_fetch,
)
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore

CALENDAR = {
    "earningsCalendar": [
        {"date": "2026-08-05", "epsEstimate": 1.61, "epsActual": None,
         "hour": "amc", "quarter": 2, "revenueEstimate": 7_400_000_000,
         "revenueActual": None, "symbol": "AMD", "year": 2026},
        {"date": "2026-08-06", "epsEstimate": 0.55, "epsActual": None,
         "hour": "bmo", "quarter": 2, "revenueEstimate": None,
         "revenueActual": None, "symbol": "SHOP", "year": 2026},
    ]
}


def client(payload=None, status=200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else CALENDAR)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/cal.db")
    yield database
    await database.close()


def make_feed(db):
    bus = EventBus()
    queue = bus.subscribe("estimate")
    feed = EarningsCalendarFeed(bus, SignalStore(db),
                                SimpleNamespace(finnhub_api_key="k"))
    return feed, queue


@pytest.mark.asyncio
class TestCalendarFetch:
    async def test_reporters_journaled_with_consensus(self, db):
        feed, queue = make_feed(db)
        async with client() as http:
            await feed._fetch_once(http)

        assert feed.reporters_last_fetch == 2 and feed.journaled == 2
        amd = queue.get_nowait()
        assert amd.type == "estimate" and amd.source == "earnings-cal"
        assert amd.key == "cal:AMD:2026-08-05"
        assert amd.symbols == ("AMD",)
        assert amd.payload["when"] == "amc"
        assert amd.payload["eps_consensus"] == 1.61
        assert amd.payload["revenue_consensus"] == 7_400_000_000
        assert amd.payload["fiscal_period"] == "Q2 2026"
        assert amd.signal_id is not None
        shop = queue.get_nowait()
        assert shop.payload["when"] == "bmo"
        assert shop.payload["revenue_consensus"] is None

    async def test_repoll_republishes_but_journals_once(self, db):
        """At-least-once bus delivery: a strategy that restarts mid-afternoon
        must hear tonight's reporters again on the next fetch; the journal
        still dedupes on cal:{symbol}:{date}."""
        feed, queue = make_feed(db)
        async with client() as http:
            await feed._fetch_once(http)
            await feed._fetch_once(http)
        assert feed.journaled == 2 and feed.published == 4
        assert queue.qsize() == 4
        # Replayed event carries the ORIGINAL journal id (provenance intact).
        first = queue.get_nowait()
        queue.get_nowait()
        replay = queue.get_nowait()
        assert replay.signal_id == first.signal_id

    async def test_malformed_reporter_skipped_rest_survive(self, db):
        feed, queue = make_feed(db)
        bad = {"earningsCalendar": [
            {"date": "2026-08-05", "hour": "amc"},  # no symbol
            CALENDAR["earningsCalendar"][0],
        ]}
        async with client(bad) as http:
            await feed._fetch_once(http)
        assert feed.errors == 1 and feed.journaled == 1 and queue.qsize() == 1

    async def test_unknown_hour_still_journals(self, db):
        feed, queue = make_feed(db)
        odd = {"earningsCalendar": [
            {**CALENDAR["earningsCalendar"][0], "hour": ""},
        ]}
        async with client(odd) as http:
            await feed._fetch_once(http)
        assert queue.get_nowait().payload["when"] == "unknown"

    async def test_empty_calendar_is_fine(self, db):
        feed, queue = make_feed(db)
        async with client({"earningsCalendar": []}) as http:
            await feed._fetch_once(http)
        assert feed.fetches == 1 and queue.qsize() == 0

    async def test_server_error_propagates_to_supervisor(self, db):
        feed, _ = make_feed(db)
        async with client(status=500) as http:
            with pytest.raises(httpx.HTTPStatusError):
                await feed._fetch_once(http)


@pytest.mark.asyncio
class TestAuthFailure:
    async def test_bad_key_idles_instead_of_hammering(self, db, monkeypatch):
        """401 must NOT raise out of run() (supervise would retry all day);
        the feed marks auth_failed and sleeps to the next daily fetch."""
        feed, _ = make_feed(db)
        monkeypatch.setattr(feed, "_client", lambda: client(status=401))
        task = asyncio.create_task(feed.run())
        try:
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                if feed.auth_failed:
                    break
                await asyncio.sleep(0.02)
            assert feed.auth_failed and not task.done()
        finally:
            task.cancel()


def test_seconds_until_next_fetch():
    before = datetime.fromisoformat("2026-08-04T14:00:00-04:00")
    assert seconds_until_next_fetch(before) == 3600.0
    after = datetime.fromisoformat("2026-08-04T15:00:00-04:00")
    assert seconds_until_next_fetch(after) == 24 * 3600.0
