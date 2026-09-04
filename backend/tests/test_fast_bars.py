"""Sub-minute bars rolled from the trade tape (services/fast_bars.py) and
their plumbing through MarketDataService: fold semantics, print filtering,
seed/stream dedupe, retention, and the keyless/refcount paths."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.broadcast import Broadcaster
from app.services.fast_bars import (
    FAST_TFS,
    FastBarStore,
    fast_bucket_start,
    fold_trade,
    is_fast_tf,
    price_forming,
)

T0 = 1_785_400_020_000  # epoch-ms base aligned to a 60s boundary


def test_fast_tfs_and_buckets():
    assert FAST_TFS == ["5s", "15s", "30s"]
    assert is_fast_tf("5s") and not is_fast_tf("1m")
    assert fast_bucket_start(T0 + 4_999, "5s") == T0
    assert fast_bucket_start(T0 + 5_000, "5s") == T0 + 5_000
    assert fast_bucket_start(T0 + 29_999, "30s") == T0


def test_price_forming_filters_cta_non_last_conditions():
    assert price_forming(None)
    assert price_forming([])
    assert price_forming(["@", "I"])  # regular + odd lot update the last
    assert not price_forming(["W"])   # average price
    assert not price_forming(["@", "Z"])  # out of sequence
    assert not price_forming("4")  # derivatively priced, as a bare string


def test_fold_trade_opens_and_extends():
    bar = fold_trade(None, T0, 100.0, 10)
    assert bar == {"t": T0, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 10}
    bar = fold_trade(bar, T0, 101.0, 5)
    bar = fold_trade(bar, T0, 99.5, 1)
    assert bar == {"t": T0, "o": 100.0, "h": 101.0, "l": 99.5, "c": 99.5, "v": 16}


def test_store_updates_every_fast_tf_per_print():
    store = FastBarStore()
    updates = store.on_trade("SPY", 100.0, 10, T0 + 1_000, ["@"], 1)
    assert [tf for tf, _ in updates] == FAST_TFS
    updates = store.on_trade("SPY", 100.5, 3, T0 + 6_000, ["@"], 2)
    # 5s rolled into a new bucket; 15s/30s extended the same one.
    by_tf = dict(updates)
    assert by_tf["5s"]["t"] == T0 + 5_000 and by_tf["5s"]["o"] == 100.5
    assert by_tf["15s"]["t"] == T0 and by_tf["15s"]["h"] == 100.5 and by_tf["15s"]["v"] == 13
    assert store.count("SPY", "5s") == 2 and store.count("SPY", "30s") == 1


def test_store_drops_junk_late_and_replayed_prints():
    store = FastBarStore()
    assert store.on_trade("SPY", 100.0, 10, T0 + 1_000, None, 7)
    assert store.on_trade("SPY", 100.0, 10, T0 + 1_000, None, 7) == []  # replay
    assert store.on_trade("SPY", 90.0, 10, T0 + 500, None, 8) == []      # late
    assert store.on_trade("SPY", 90.0, 10, T0 + 2_000, ["W"], 9) == []   # avg-price
    assert store.on_trade("SPY", 0.0, 10, T0 + 2_000, None, 10) == []    # no price
    assert store.get_bars("SPY", "5s")[-1]["l"] == 100.0


def test_seed_folds_oldest_first_and_never_rewrites_live_buckets():
    store = FastBarStore()
    # Stream already delivered a print at +20s.
    store.on_trade("SPY", 101.0, 1, T0 + 20_000, None, 50)
    n = store.seed("SPY", [
        {"p": 100.0, "s": 5, "t": T0 + 3_000, "c": None, "i": 1},   # older: skipped
        {"p": 100.2, "s": 5, "t": T0 + 21_000, "c": None, "i": 51},  # newer: folded
        {"p": 100.9, "s": 5, "t": T0 + 20_000, "c": None, "i": 50},  # replay: skipped
    ])
    assert n == 1
    bars = store.get_bars("SPY", "5s")
    assert [b["t"] for b in bars] == [T0 + 20_000]
    assert bars[0]["c"] == 100.2 and bars[0]["v"] == 6


def test_retention_trims_old_buckets():
    store = FastBarStore(retention_ms=60_000)
    for i in range(30):
        store.on_trade("SPY", 100.0, 1, T0 + i * 5_000, None, i)
    ts = [b["t"] for b in store.get_bars("SPY", "5s")]
    assert ts[0] >= T0 + 29 * 5_000 - 60_000
    assert ts[-1] == T0 + 29 * 5_000
    store.forget("SPY")
    assert store.count("SPY", "5s") == 0 and store.last_print_ts("SPY") is None


# ------------------------------------------------ MarketDataService plumbing


class _Bars:
    def get_bars(self, symbol, tf, limit=1000):
        return [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "tf": tf}]


def _market(configured: bool):
    from app.services.market_data import MarketDataService

    alpaca = SimpleNamespace(configured=configured, stock_feed="iex")
    settings = SimpleNamespace(bar_cache_days=1, alpaca_api_key="", alpaca_secret_key="")
    return MarketDataService(settings, alpaca, SimpleNamespace(healthy=False), _Bars(), Broadcaster())


def test_get_bars_routes_fast_tfs_to_the_tape_store():
    market = _market(configured=False)
    assert market.get_bars("SPY", "1m")[0]["tf"] == "1m"
    assert market.get_bars("SPY", "5s") == []
    market.fast.on_trade("SPY", 100.0, 1, T0, None, 1)
    assert market.get_bars("SPY", "5s")[0]["c"] == 100.0


@pytest.mark.asyncio
async def test_keyless_subscribe_fast_counts_refs_and_publishes_prints():
    market = _market(configured=False)
    await market.subscribe_fast("spy")
    await market.subscribe_fast("SPY")
    assert market.status()["fast_symbols"] == ["SPY"]
    assert market._fast_refs["SPY"] == 2

    queue: asyncio.Queue = asyncio.Queue()
    market.broadcast.subscribe("bars:SPY:5s", queue)
    trade = SimpleNamespace(
        symbol="SPY", price=100.25, size=7,
        timestamp=SimpleNamespace(timestamp=lambda: T0 / 1000), conditions=["@"], id=3,
    )
    await market._on_stock_trade(trade)
    msg = queue.get_nowait()
    assert msg["t"] == "bar" and msg["tf"] == "5s" and msg["bar"]["c"] == 100.25
    assert market.stream_age_s is not None and market.stream_age_s < 5

    await market.unsubscribe_fast("SPY")
    assert market._fast_refs["SPY"] == 1
    await market.unsubscribe_fast("SPY")
    assert "SPY" not in market._fast_refs
    # Keyless: nothing to unsubscribe upstream, and the series is kept
    # (only a configured stream drop forgets it).
    assert market.fast.count("SPY", "5s") == 1
