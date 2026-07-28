import asyncio

import pytest

from app.services.bar_store import BarStore, bucket_start, merge_into_bucket, resample


class FakeRedis:
    healthy = False

    async def save_bars(self, *a, **k):
        pass

    async def load_bars(self, *a, **k):
        return {}


def mk(t_min: int, o=100.0, h=101.0, l=99.0, c=100.5, v=10):
    return {"t": t_min * 60_000, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_bucket_start():
    assert bucket_start(0, "5m") == 0
    assert bucket_start(4 * 60_000, "5m") == 0
    assert bucket_start(5 * 60_000, "5m") == 5 * 60_000
    assert bucket_start(59 * 60_000, "1h") == 0
    assert bucket_start(60 * 60_000, "1h") == 60 * 60_000


def test_merge_into_bucket_new_and_fold():
    bar1 = mk(0, o=10, h=12, l=9, c=11, v=5)
    bar2 = mk(1, o=11, h=15, l=10, c=14, v=7)
    bucket = merge_into_bucket(None, bar1, 0)
    assert bucket == {"t": 0, "o": 10, "h": 12, "l": 9, "c": 11, "v": 5}
    bucket = merge_into_bucket(bucket, bar2, 0)
    assert bucket == {"t": 0, "o": 10, "h": 15, "l": 9, "c": 14, "v": 12}


def test_resample_5m_ohlcv_semantics():
    bars = [
        mk(0, o=1, h=5, l=1, c=2, v=1),
        mk(1, o=2, h=3, l=0.5, c=3, v=1),
        mk(4, o=3, h=4, l=2, c=3.5, v=1),
        mk(5, o=4, h=6, l=3, c=5, v=2),  # next bucket
    ]
    out = resample(bars, "5m")
    assert len(out) == 2
    first, second = out
    assert first["o"] == 1 and first["c"] == 3.5
    assert first["h"] == 5 and first["l"] == 0.5 and first["v"] == 3
    assert second["t"] == 5 * 60_000 and second["v"] == 2


def test_resample_handles_session_gaps():
    bars = [mk(0), mk(1), mk(390), mk(391)]  # 6.5h gap (next session)
    out = resample(bars, "15m")
    assert [b["t"] for b in out] == [0, 390 * 60_000 - (390 * 60_000 % 900_000)]


@pytest.fixture
def store():
    return BarStore(FakeRedis(), max_1m_bars=100)


def test_upsert_updates_derived_timeframes(store):
    async def run():
        updates = await store.upsert_1m("SPY", mk(0, o=1, h=2, l=1, c=2, v=1))
        tfs = {tf for tf, _ in updates}
        assert tfs == {"1m", "5m", "15m", "1h"}

        # Correction of the same 1m bar must re-fold, not double-count volume.
        await store.upsert_1m("SPY", mk(0, o=1, h=3, l=1, c=2.5, v=4))
        five = store.get_bars("SPY", "5m")
        assert five[-1]["v"] == 4 and five[-1]["h"] == 3

        await store.upsert_1m("SPY", mk(1, o=2.5, h=4, l=2, c=3, v=2))
        five = store.get_bars("SPY", "5m")
        assert five[-1]["v"] == 6 and five[-1]["o"] == 1 and five[-1]["c"] == 3

    asyncio.run(run())


def test_retention_bounded(store):
    async def run():
        for i in range(150):
            await store.upsert_1m("SPY", mk(i))
        assert len(store.get_bars("SPY", "1m", limit=10_000)) == 100

    asyncio.run(run())


def test_bulk_insert_then_get(store):
    async def run():
        await store.bulk_insert_1m("SPY", [mk(i) for i in range(20)])
        assert len(store.get_bars("SPY", "1m", limit=100)) == 20
        assert len(store.get_bars("SPY", "5m", limit=100)) == 4
        assert store.last_ts("SPY") == 19 * 60_000

    asyncio.run(run())
