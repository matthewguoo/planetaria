"""Sub-minute bars for scalping, rolled in-process from the trade tape.

The 1m bars in `bar_store` are Alpaca's server-aggregated bars and the
source of truth for everything the engine prices off. A 0DTE scalp wants
a faster clock than that, and the broker streams nothing faster than 1m
bars — but it does stream every print (IEX on the free tier, SIP on the
paid one), and a 5s bar is nothing more than the prints inside one 5s
bucket. So the fast timeframes are built here, from `subscribe_trades`,
and live only in memory: they are a *view* of the tape for a trader
watching the screen, never a pricing input (the enforcer, the risk gate
and the strategies keep reading 1m bars + quotes).

Honest-data notes the chart must not hide:
- On the free tier the tape is IEX-only (~2-3% of consolidated volume in
  SPY/QQQ, near-zero in thin names). Prices are real prints; VOLUME is a
  fraction of the market's. A bucket with no IEX print is simply absent —
  the chart compresses the gap rather than fabricating a flat bar.
- A print is dropped when its conditions mark it as not price-forming
  (average-price, out-of-sequence, derivatively priced, bunched, the
  official open/close, prior-reference, contingent) — the CTA "does not
  update last" set, which is also what the 1m bars exclude.
"""

import logging

log = logging.getLogger("app.fastbars")

FAST_TF_MS: dict[str, int] = {
    "5s": 5_000,
    "15s": 15_000,
    "30s": 30_000,
}
FAST_TFS = list(FAST_TF_MS)

# How much fast history to keep per (symbol, tf): three RTH hours of the
# finest bucket. Enough to scroll back through the morning; small enough
# that forty subscribed symbols cost a few MB.
FAST_RETENTION_MS = 3 * 3_600_000

# Trade conditions that must not update the last price (CTA/UTP rules).
NON_PRICE_FORMING = frozenset({"W", "Z", "4", "B", "M", "Q", "P", "7", "9", "U"})

Bar = dict  # {t: int(ms), o: float, h: float, l: float, c: float, v: int}


def is_fast_tf(tf: str) -> bool:
    return tf in FAST_TF_MS


def price_forming(conditions) -> bool:
    """True when a print should update OHLC. Missing conditions = regular."""
    if not conditions:
        return True
    return not any(str(c) in NON_PRICE_FORMING for c in conditions)


def fast_bucket_start(ts_ms: int, tf: str) -> int:
    return ts_ms - (ts_ms % FAST_TF_MS[tf])


def fold_trade(bar: Bar | None, bucket_ts: int, price: float, size: int) -> Bar:
    if bar is None or bar["t"] != bucket_ts:
        return {"t": bucket_ts, "o": price, "h": price, "l": price, "c": price, "v": size}
    return {
        "t": bucket_ts,
        "o": bar["o"],
        "h": max(bar["h"], price),
        "l": min(bar["l"], price),
        "c": price,
        "v": bar["v"] + size,
    }


class FastBarStore:
    """symbol -> tf -> {bucket_ts -> bar}, appended in tape order."""

    def __init__(self, retention_ms: int = FAST_RETENTION_MS):
        self._retention_ms = retention_ms
        self._bars: dict[str, dict[str, dict[int, Bar]]] = {}
        # Last print seen per symbol (ts_ms, id) — a late duplicate from a
        # REST seed overlapping the stream must not double-count volume.
        self._last_print: dict[str, tuple[int, int | None]] = {}

    def _series(self, symbol: str, tf: str) -> dict[int, Bar]:
        return self._bars.setdefault(symbol, {}).setdefault(tf, {})

    def get_bars(self, symbol: str, tf: str, limit: int = 3000) -> list[Bar]:
        series = self._series(symbol, tf)
        keys = sorted(series.keys())[-limit:]
        return [series[k] for k in keys]

    def count(self, symbol: str, tf: str) -> int:
        return len(self._bars.get(symbol, {}).get(tf, {}))

    def last_print_ts(self, symbol: str) -> int | None:
        seen = self._last_print.get(symbol)
        return seen[0] if seen else None

    def forget(self, symbol: str) -> None:
        self._bars.pop(symbol, None)
        self._last_print.pop(symbol, None)

    def on_trade(self, symbol: str, price: float, size: int, ts_ms: int,
                 conditions=None, trade_id: int | None = None) -> list[tuple[str, Bar]]:
        """Fold one print into every fast timeframe. Returns [(tf, bar)]
        for broadcast; empty when the print is not price-forming, is out
        of order, or is a replay of the last one seen."""
        if price <= 0 or not price_forming(conditions):
            return []
        last = self._last_print.get(symbol)
        if last is not None:
            if ts_ms < last[0]:
                return []  # late print: the bucket it belongs to is closed
            if ts_ms == last[0] and trade_id is not None and trade_id == last[1]:
                return []  # exact replay (stream + seed overlap)
        self._last_print[symbol] = (ts_ms, trade_id)
        updates: list[tuple[str, Bar]] = []
        for tf in FAST_TFS:
            series = self._series(symbol, tf)
            bts = fast_bucket_start(ts_ms, tf)
            bar = fold_trade(series.get(bts), bts, float(price), int(size or 0))
            series[bts] = bar
            updates.append((tf, bar))
        self._trim(symbol, ts_ms)
        return updates

    def seed(self, symbol: str, prints: list[dict]) -> int:
        """Backfill from a REST trade page (dicts with p, s, t(ms), c, i),
        oldest first. Only prints newer than the last one folded count, so
        seeding after the stream has started cannot rewrite live buckets."""
        n = 0
        for tr in sorted(prints, key=lambda x: x["t"]):
            if self.on_trade(symbol, tr["p"], tr.get("s", 0), tr["t"],
                             tr.get("c"), tr.get("i")):
                n += 1
        return n

    def _trim(self, symbol: str, now_ms: int) -> None:
        floor = now_ms - self._retention_ms
        for tf, series in self._bars.get(symbol, {}).items():
            if not series:
                continue
            oldest = next(iter(series))
            if oldest >= floor:
                continue
            for ts in [k for k in series if k < floor]:
                del series[ts]
