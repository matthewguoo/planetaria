"""Event bus, signal journal, and news feed normalization."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.signals.events import Event, EventBus
from app.services.signals.feeds import AlpacaNewsFeed, et_session
from app.services.signals.store import SignalStore


def make_event(**overrides) -> Event:
    base = dict(
        type="news", ts=datetime.now(timezone.utc), source="test",
        key="k1", symbols=("SPY",), payload={"headline": "hi"},
    )
    base.update(overrides)
    return Event(**base)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/signals.db")
    yield database
    await database.close()


class TestEventBus:
    def test_fanout_by_type(self):
        bus = EventBus()
        q_news = bus.subscribe("news")
        q_timer = bus.subscribe("timer")
        bus.publish(make_event())
        assert q_news.qsize() == 1
        assert q_timer.qsize() == 0

    def test_lossless_full_queue_counts_drops_keeps_backlog(self):
        bus = EventBus()
        queue = bus.subscribe("news", maxsize=2, lossless=True)
        for i in range(4):
            bus.publish(make_event(key=f"k{i}"))
        # Backlog preserved (oldest first), overflow counted, nothing shed.
        assert queue.qsize() == 2
        assert queue.get_nowait().key == "k0"
        status = bus.status()["news"]["subscribers"][0]
        assert status["dropped"] == 2

    def test_lossy_full_queue_keeps_newest(self):
        bus = EventBus()
        queue = bus.subscribe("news", maxsize=2, lossless=False)
        for i in range(4):
            bus.publish(make_event(key=f"k{i}"))
        assert queue.qsize() == 2
        assert queue.get_nowait().key == "k2"  # oldest shed

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        queue = bus.subscribe("news")
        bus.unsubscribe("news", queue)
        bus.publish(make_event())
        assert queue.qsize() == 0


class TestSignalStore:
    @pytest.mark.asyncio
    async def test_record_assigns_id_and_dedupes_replay(self, db):
        store = SignalStore(db)
        event, created = await store.record(make_event())
        assert created and event.signal_id is not None

        replay, created2 = await store.record(make_event())
        assert not created2
        assert replay.signal_id == event.signal_id

    @pytest.mark.asyncio
    async def test_timer_events_not_journaled(self, db):
        store = SignalStore(db)
        event, created = await store.record(make_event(type="timer", key=None))
        assert created and event.signal_id is None
        assert await store.recent() == []

    @pytest.mark.asyncio
    async def test_enrichment_chains_parent(self, db):
        store = SignalStore(db)
        raw, _ = await store.record(make_event())
        child = make_event(type="signal", key="k1:llm", source="llm-worker",
                           payload={"direction": "up"})
        child = await store.enrich(raw.signal_id, child)
        rows = await store.by_ids([child.signal_id])
        assert rows[0]["parent_id"] == raw.signal_id

    @pytest.mark.asyncio
    async def test_recent_filters_and_prune(self, db):
        store = SignalStore(db)
        await store.record(make_event(key="a", symbols=("SPY",)))
        await store.record(make_event(key="b", symbols=("AMD",)))
        assert len(await store.recent(symbol="AMD")) == 1
        assert await store.prune(keep_days=0) == 2
        assert await store.recent() == []


def fake_news(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(id=101, headline="AMD beats", summary="s", url="http://x",
                author="a", created_at=now, updated_at=now, symbols=["AMD"])
    base.update(overrides)
    return SimpleNamespace(**base)


class TestNewsFeed:
    @pytest.mark.asyncio
    async def test_normalize_journal_publish(self, db):
        bus = EventBus()
        queue = bus.subscribe("news")
        feed = AlpacaNewsFeed(alpaca=None, bus=bus, store=SignalStore(db))
        await feed._on_news(fake_news())
        event = queue.get_nowait()
        assert event.symbols == ("AMD",)
        assert event.payload["headline"] == "AMD beats"
        assert event.signal_id is not None
        assert feed.journaled == 1

    @pytest.mark.asyncio
    async def test_reconnect_replay_not_republished(self, db):
        bus = EventBus()
        queue = bus.subscribe("news")
        feed = AlpacaNewsFeed(alpaca=None, bus=bus, store=SignalStore(db))
        item = fake_news()
        await feed._on_news(item)
        await feed._on_news(item)  # replay after reconnect
        assert queue.qsize() == 1
        assert feed.received == 2 and feed.journaled == 1

    @pytest.mark.asyncio
    async def test_revised_headline_is_a_fresh_event(self, db):
        bus = EventBus()
        queue = bus.subscribe("news")
        feed = AlpacaNewsFeed(alpaca=None, bus=bus, store=SignalStore(db))
        await feed._on_news(fake_news())
        await feed._on_news(fake_news(
            headline="AMD beats, guides UP",
            updated_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        ))
        assert queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_malformed_item_survives(self, db):
        bus = EventBus()
        feed = AlpacaNewsFeed(alpaca=None, bus=bus, store=SignalStore(db))
        await feed._on_news(object())  # no .id -> normalization error
        assert feed.errors == 1
        await feed._on_news(fake_news())  # socket handler still alive
        assert feed.journaled == 1


def test_et_session_labels():
    def at(hour, minute=0, weekday_date="2026-08-04"):  # a Tuesday
        return datetime.fromisoformat(f"{weekday_date}T{hour:02d}:{minute:02d}:00-04:00")

    assert et_session(at(3)) == "overnight"
    assert et_session(at(5)) == "premarket"
    assert et_session(at(10)) == "rth"
    assert et_session(at(17)) == "postmarket"
    assert et_session(at(21)) == "overnight"
    assert et_session(datetime.fromisoformat("2026-08-08T10:00:00-04:00")) == "weekend"
