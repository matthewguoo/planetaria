"""In-process bar store with derived-timeframe resampling and Redis write-through.

Bars are plain dicts {t, o, h, l, c, v} with t = epoch milliseconds UTC.
1-minute bars are the source of truth (Alpaca server-aggregated); 5m/15m/1h
are derived incrementally on each 1m upsert — never recomputed from scratch
on the hot path.
"""

import json
import logging

from app.services.redis_client import RedisFacade

log = logging.getLogger("app.bars")

Bar = dict  # {t: int(ms), o: float, h: float, l: float, c: float, v: int}

TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}
DERIVED_TFS = [tf for tf in TF_MS if tf != "1m"]


def bucket_start(ts_ms: int, tf: str) -> int:
    return ts_ms - (ts_ms % TF_MS[tf])


def merge_into_bucket(bucket: Bar | None, bar: Bar, bucket_ts: int) -> Bar:
    """Fold a finer bar into a coarser bucket (order of arrival = time order)."""
    if bucket is None or bucket["t"] != bucket_ts:
        return {"t": bucket_ts, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}
    return {
        "t": bucket_ts,
        "o": bucket["o"],
        "h": max(bucket["h"], bar["h"]),
        "l": min(bucket["l"], bar["l"]),
        "c": bar["c"],
        "v": bucket["v"] + bar["v"],
    }


def resample(one_min: list[Bar], tf: str) -> list[Bar]:
    """Full resample of a sorted 1m series (used for backfill, not hot path)."""
    out: list[Bar] = []
    for bar in one_min:
        bts = bucket_start(bar["t"], tf)
        if out and out[-1]["t"] == bts:
            out[-1] = merge_into_bucket(out[-1], bar, bts)
        else:
            out.append(merge_into_bucket(None, bar, bts))
    return out


class BarStore:
    def __init__(self, redis: RedisFacade, max_1m_bars: int = 39_000):
        self._redis = redis
        self._max_1m = max_1m_bars
        # symbol -> tf -> {ts -> bar}; python dicts preserve insertion order,
        # and we only ever append at the tail or update the last bucket.
        self._bars: dict[str, dict[str, dict[int, Bar]]] = {}

    def _series(self, symbol: str, tf: str) -> dict[int, Bar]:
        return self._bars.setdefault(symbol, {}).setdefault(tf, {})

    def symbols(self) -> list[str]:
        return list(self._bars.keys())

    def get_bars(self, symbol: str, tf: str, limit: int = 1000) -> list[Bar]:
        series = self._series(symbol, tf)
        keys = sorted(series.keys())[-limit:]
        return [series[k] for k in keys]

    def last_ts(self, symbol: str) -> int | None:
        series = self._series(symbol, "1m")
        return max(series.keys()) if series else None

    # ------------------------------------------------------------------ load

    async def hydrate(self, symbol: str) -> int:
        """Load persisted 1m bars from Redis, rebuild derived TFs. Returns count."""
        raw = await self._redis.load_bars(symbol, "1m")
        if not raw:
            return 0
        series = self._series(symbol, "1m")
        for ts_str, payload in raw.items():
            series[int(ts_str)] = json.loads(payload)
        # Rebuild in time order (hgetall has no order guarantee).
        ordered = [series[k] for k in sorted(series.keys())]
        self._bars[symbol]["1m"] = {b["t"]: b for b in ordered}
        for tf in DERIVED_TFS:
            self._bars[symbol][tf] = {b["t"]: b for b in resample(ordered, tf)}
        return len(ordered)

    async def bulk_insert_1m(self, symbol: str, bars: list[Bar]) -> None:
        """Backfill path: merge a sorted batch of 1m bars, rebuild derived TFs."""
        series = self._series(symbol, "1m")
        for bar in bars:
            series[bar["t"]] = bar
        ordered = [series[k] for k in sorted(series.keys())][-self._max_1m :]
        self._bars[symbol]["1m"] = {b["t"]: b for b in ordered}
        for tf in DERIVED_TFS:
            self._bars[symbol][tf] = {b["t"]: b for b in resample(ordered, tf)}
        await self._redis.save_bars(
            symbol, "1m", {str(b["t"]): json.dumps(b) for b in ordered}
        )

    # ------------------------------------------------------------------- hot

    async def upsert_1m(self, symbol: str, bar: Bar) -> list[tuple[str, Bar]]:
        """Live path: upsert one 1m bar, incrementally update derived TFs.

        Returns [(tf, updated_bar), ...] for broadcast — always includes 1m.
        """
        self._series(symbol, "1m")[bar["t"]] = bar
        updates: list[tuple[str, Bar]] = [("1m", bar)]
        for tf in DERIVED_TFS:
            series = self._series(symbol, tf)
            bts = bucket_start(bar["t"], tf)
            existing = series.get(bts)
            # Re-fold the bucket from its constituent 1m bars: an upsert of an
            # already-seen 1m bar (dedup/correction) would corrupt a running
            # merge, so recompute the single affected bucket exactly.
            one_min = self._series(symbol, "1m")
            members = [
                one_min[k]
                for k in sorted(one_min.keys())
                if bts <= k < bts + TF_MS[tf]
            ]
            folded: Bar | None = None
            for m in members:
                folded = merge_into_bucket(folded, m, bts)
            if folded is not None and folded != existing:
                series[bts] = folded
                updates.append((tf, folded))
        await self._redis.save_bars(symbol, "1m", {str(bar["t"]): json.dumps(bar)})
        self._enforce_retention(symbol)
        return updates

    def _enforce_retention(self, symbol: str) -> None:
        series = self._series(symbol, "1m")
        if len(series) <= self._max_1m:
            return
        for ts in sorted(series.keys())[: len(series) - self._max_1m]:
            del series[ts]
