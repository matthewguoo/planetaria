"""Synthetic market data for keyless/off-hours development and UI QA.

Activates ONLY when Alpaca keys are not configured. Generates a seeded
random-walk 1m bar history plus a live ticking quote/bar so every part of
the UI pipeline (backfill, snapshots, deltas, resampling) can be exercised
end-to-end. Status frames carry demo=true and the UI shows a DEMO badge —
this must never be mistakable for market data.
"""

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("app.demo")

BASE_PRICES = {"SPY": 452.0, "QQQ": 388.0, "IWM": 205.0, "AAPL": 194.0, "TSLA": 262.0}
RTH_START_H, RTH_END_H = 13, 20  # 09:30-16:00 ET expressed loosely in UTC (13:30-20:00)


def _seed(symbol: str) -> random.Random:
    return random.Random(hash(symbol) & 0xFFFFFFFF)


def synth_history(symbol: str, days: int = 3) -> list[dict]:
    """Random-walk 1m bars across the LAST `days` sessions ENDING TODAY —
    today's intraday bars must exist so entry-anchored position views (and
    anything else time-addressed) land on real bars, not in a gap."""
    rng = _seed(symbol)
    price = BASE_PRICES.get(symbol, 50 + (hash(symbol) % 400))
    vol = price * 0.00035  # per-minute sigma
    bars: list[dict] = []
    now = datetime.now(timezone.utc)
    session_days: list = []
    cursor = now.date()
    while len(session_days) < days:
        if cursor.weekday() < 5:
            session_days.append(cursor)
        cursor -= timedelta(days=1)
    session_days.reverse()
    for day in session_days:
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=RTH_START_H, minute=30
        )
        for minute in range(390):
            ts = start + timedelta(minutes=minute)
            if ts > now:
                break
            drift = math.sin(minute / 55) * vol * 0.4
            o = price
            c = price + rng.gauss(drift, vol)
            h = max(o, c) + abs(rng.gauss(0, vol * 0.5))
            l = min(o, c) - abs(rng.gauss(0, vol * 0.5))
            v = int(abs(rng.gauss(280_000, 90_000)))
            bars.append(
                {"t": int(ts.timestamp() * 1000), "o": round(o, 2), "h": round(h, 2),
                 "l": round(l, 2), "c": round(c, 2), "v": v}
            )
            price = c
    return bars


class DemoFeed:
    """Drives MarketDataService's store/broadcast with synthetic ticks."""

    def __init__(self, market):
        self.market = market
        self._tasks: dict[str, asyncio.Task] = {}
        self._prices: dict[str, float] = {}
        self._rngs: dict[str, random.Random] = {}

    def active_for(self, symbol: str) -> bool:
        return symbol in self._tasks

    async def ensure(self, symbol: str) -> None:
        if symbol in self._tasks:
            return
        history = synth_history(symbol)
        await self.market.bars.bulk_insert_1m(symbol, history)
        self._prices[symbol] = history[-1]["c"] if history else 100.0
        self._rngs[symbol] = _seed(symbol + "live")
        self._tasks[symbol] = asyncio.create_task(self._tick(symbol), name=f"demo-{symbol}")
        log.info("demo feed active for %s (%d bars)", symbol, len(history))

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _tick(self, symbol: str) -> None:
        rng = self._rngs[symbol]
        current: dict | None = None
        while True:
            await asyncio.sleep(1.5)
            price = self._prices[symbol]
            vol = price * 0.00012
            price = max(0.01, price + rng.gauss(0, vol))
            self._prices[symbol] = price

            # Quote.
            spread = price * 0.0001
            msg = {
                "t": "quote",
                "symbol": symbol,
                "bid": round(price - spread, 2),
                "ask": round(price + spread, 2),
                "mid": round(price, 4),
                "ts": int(time.time() * 1000),
            }
            self.market._latest_quotes[symbol] = msg
            self.market.broadcast.publish(f"quote:{symbol}", msg)

            # Live 1m bar (real minute boundaries).
            minute_ts = int(time.time() // 60 * 60 * 1000)
            p = round(price, 2)
            if current is None or current["t"] != minute_ts:
                current = {"t": minute_ts, "o": p, "h": p, "l": p, "c": p, "v": 0}
            current["h"] = max(current["h"], p)
            current["l"] = min(current["l"], p)
            current["c"] = p
            current["v"] += int(abs(rng.gauss(5000, 2000)))
            updates = await self.market.bars.upsert_1m(symbol, dict(current))
            for tf, bar in updates:
                self.market.broadcast.publish(
                    f"bars:{symbol}:{tf}",
                    {"t": "bar", "symbol": symbol, "tf": tf, "bar": bar},
                )
