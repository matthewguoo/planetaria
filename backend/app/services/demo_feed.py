"""Keyless fallback market data.

Activates ONLY when Alpaca keys are not configured. Prefers REAL prices:
1m bar history and a polled live quote come from Yahoo's public chart API
(no key required), so the chart shows the actual market even before Alpaca
is wired up. Only if that endpoint is unreachable does a symbol fall back
to a seeded synthetic random walk. Status frames carry demo=true plus a
per-symbol source ("public" | "synthetic") and the UI labels the feed —
the fallback must never be mistakable for tradeable Alpaca data.
"""

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("app.demo")

BASE_PRICES = {"SPY": 452.0, "QQQ": 388.0, "IWM": 205.0, "AAPL": 194.0, "TSLA": 262.0}
RTH_START_H, RTH_END_H = 13, 20  # 09:30-16:00 ET expressed loosely in UTC (13:30-20:00)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) planetaria/1.0"}
POLL_INTERVAL_S = 5.0


def parse_yahoo_chart(payload: dict) -> tuple[list[dict], float | None]:
    """Pure parser for Yahoo v8 chart JSON -> (1m bars, last price).

    Skips null rows (halted/no-trade minutes come back as nulls). Last price
    prefers meta.regularMarketPrice, falling back to the final bar close.
    """
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return [], None
    data = result[0]
    meta = data.get("meta") or {}
    stamps = data.get("timestamp") or []
    quote_block = ((data.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []

    bars: list[dict] = []
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            break
        if o is None or h is None or l is None or c is None:
            continue
        v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        bars.append(
            {"t": int(ts) * 1000, "o": round(float(o), 4), "h": round(float(h), 4),
             "l": round(float(l), 4), "c": round(float(c), 4), "v": int(v)}
        )

    last = meta.get("regularMarketPrice")
    if last is None and bars:
        last = bars[-1]["c"]
    return bars, (float(last) if last is not None else None)


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
    """Drives MarketDataService's store/broadcast with real public data
    (Yahoo poll) per symbol, or synthetic ticks when that's unreachable."""

    def __init__(self, market):
        self.market = market
        self._tasks: dict[str, asyncio.Task] = {}
        self._prices: dict[str, float] = {}
        self._rngs: dict[str, random.Random] = {}
        self._sources: dict[str, str] = {}  # symbol -> "public" | "synthetic"
        self._http: httpx.AsyncClient | None = None
        # Live-adjustable via the feed settings API.
        self.poll_s: float = POLL_INTERVAL_S
        # ensure() is called concurrently (WS subscribe + chain fetch); the
        # lock prevents double history fetch / duplicate poll tasks.
        self._ensure_lock = asyncio.Lock()

    def active_for(self, symbol: str) -> bool:
        return symbol in self._tasks

    @property
    def sources(self) -> dict[str, str]:
        return dict(self._sources)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers=YAHOO_HEADERS, timeout=8.0)
        return self._http

    async def _fetch_chart(self, symbol: str, range_: str) -> tuple[list[dict], float | None]:
        resp = await self._client().get(
            YAHOO_CHART_URL.format(symbol=symbol),
            params={"range": range_, "interval": "1m", "includePrePost": "false"},
        )
        resp.raise_for_status()
        return parse_yahoo_chart(resp.json())

    async def ensure(self, symbol: str) -> None:
        if symbol in self._tasks:
            return
        async with self._ensure_lock:
            await self._ensure_locked(symbol)

    async def _ensure_locked(self, symbol: str) -> None:
        if symbol in self._tasks:
            return
        try:
            history, last = await self._fetch_chart(symbol, "5d")
            if not history:
                raise ValueError("empty chart payload")
            self._sources[symbol] = "public"
            self._prices[symbol] = last or history[-1]["c"]
            log.info("public feed (yahoo) active for %s: %d real 1m bars, last %.2f",
                     symbol, len(history), self._prices[symbol])
        except Exception as exc:
            log.warning("public feed unavailable for %s (%s) - synthetic fallback", symbol, exc)
            history = synth_history(symbol)
            self._sources[symbol] = "synthetic"
            self._prices[symbol] = history[-1]["c"] if history else 100.0
        await self.market.bars.bulk_insert_1m(symbol, history)
        self._rngs[symbol] = _seed(symbol + "live")
        if self._sources[symbol] == "public":
            self._tasks[symbol] = asyncio.create_task(
                self._poll_public(symbol), name=f"public-{symbol}"
            )
        else:
            self._tasks[symbol] = asyncio.create_task(
                self._tick_synth(symbol), name=f"demo-{symbol}"
            )

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    # ------------------------------------------------------------ real data

    def _publish_quote(self, symbol: str, price: float) -> None:
        # Yahoo's chart API has no NBBO; synthesize a 1bp spread around the
        # real last price purely for display. Order routing never sees this
        # (trading is disabled without Alpaca keys).
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

    async def _poll_public(self, symbol: str) -> None:
        """Poll the day's 1m chart; upsert the tail bars and publish the real
        last price as a quote. Off-hours the price simply stops moving —
        which is correct, unlike a random walk."""
        failures = 0
        while True:
            try:
                bars, last = await self._fetch_chart(symbol, "1d")
                failures = 0
                # Re-upsert the last few bars: the forming minute mutates and
                # late prints can revise the prior one.
                for bar in bars[-3:]:
                    updates = await self.market.bars.upsert_1m(symbol, bar)
                    for tf, updated in updates:
                        self.market.broadcast.publish(
                            f"bars:{symbol}:{tf}",
                            {"t": "bar", "symbol": symbol, "tf": tf, "bar": updated},
                        )
                if last:
                    self._prices[symbol] = last
                    self._publish_quote(symbol, last)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures in (1, 10):  # log first and then rarely
                    log.warning("public poll %s failed (%d): %s", symbol, failures, exc)
            # Back off while failing; recover automatically when Yahoo returns.
            await asyncio.sleep(min(self.poll_s * (2 ** min(failures, 4)), 60.0)
                                if failures else self.poll_s)

    # ------------------------------------------------------------ synthetic

    async def _tick_synth(self, symbol: str) -> None:
        rng = self._rngs[symbol]
        current: dict | None = None
        while True:
            await asyncio.sleep(1.5)
            price = self._prices[symbol]
            vol = price * 0.00012
            price = max(0.01, price + rng.gauss(0, vol))
            self._prices[symbol] = price
            self._publish_quote(symbol, price)

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
