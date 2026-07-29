"""Market data orchestration: one upstream stock stream + one option stream,
reference-counted subscriptions, REST backfill with reconnect gap-fill,
and fanout to the in-process broadcaster.

Topics published:
    bars:{SYMBOL}:{TF}     -> {"t": "bar", "symbol", "tf", "bar": {...}}
    quote:{SYMBOL}         -> {"t": "quote", "symbol", "bid", "ask", "mid", "ts"}
    oquote:{OCC_SYMBOL}    -> {"t": "oquote", "symbol", "bid", "ask", "mid", "ts"}
    status                 -> {"t": "status", ...}
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame

from app.config import Settings
from app.services.alpaca import AlpacaService
from app.services.bar_store import BarStore
from app.services.broadcast import Broadcaster
from app.services.redis_client import RedisFacade

log = logging.getLogger("app.mktdata")

SIP_DELAY_S = 16 * 60  # free tier: SIP allowed only >15min old; use 16 for margin


def _bar_from_alpaca(bar) -> dict:
    return {
        "t": int(bar.timestamp.timestamp() * 1000),
        "o": float(bar.open),
        "h": float(bar.high),
        "l": float(bar.low),
        "c": float(bar.close),
        "v": int(bar.volume),
    }


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        alpaca: AlpacaService,
        redis: RedisFacade,
        bars: BarStore,
        broadcaster: Broadcaster,
    ):
        self.settings = settings
        self.alpaca = alpaca
        self.redis = redis
        self.bars = bars
        self.broadcast = broadcaster

        self._stock_refs: dict[str, int] = {}
        self._option_refs: dict[str, int] = {}
        self._latest_quotes: dict[str, dict] = {}  # symbol -> quote msg (stock + option)
        self._last_stream_msg: float = 0.0
        self._stock_stream = None
        self._option_stream = None
        self._tasks: list[asyncio.Task] = []
        self._backfilled: set[str] = set()
        self._backfill_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        # Last REST fetch attempt per option symbol (monotonic) — throttles
        # the poll fallback so quiet markets don't hammer the API.
        self._oquote_fetch_ts: dict[str, float] = {}

        # Synthetic feed when no keys (dev/UI QA); import here to avoid cycle.
        from app.services.demo_feed import DemoFeed

        self.demo = DemoFeed(self) if not alpaca.configured else None

    # ---------------------------------------------------------------- status

    @property
    def stream_age_s(self) -> float | None:
        if not self._last_stream_msg:
            return None
        return time.monotonic() - self._last_stream_msg

    def status(self) -> dict:
        return {
            "t": "status",
            "configured": self.alpaca.configured,
            "demo": self.demo is not None,
            # Per-symbol price source when keyless: "public" = real prices
            # from the keyless public feed, "synthetic" = random walk.
            "sources": self.demo.sources if self.demo else {},
            "redis": self.redis.healthy,
            "stream_age_s": self.stream_age_s,
            "stock_symbols": sorted(self._stock_refs.keys()),
            "option_symbols": len(self._option_refs),
        }

    def latest_quote(self, symbol: str) -> dict | None:
        return self._latest_quotes.get(symbol)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.alpaca.configured:
            return
        from app.services.supervision import supervise

        self._stock_stream = self.alpaca.make_stock_stream()
        self._option_stream = self.alpaca.make_option_stream()
        self._tasks = [
            asyncio.create_task(
                supervise("stock-stream", self._stock_stream._run_forever,
                          on_reconnect=self._gap_fill_all),
                name="stock-stream",
            ),
            asyncio.create_task(
                supervise("option-stream", self._option_stream._run_forever),
                name="option-stream",
            ),
        ]

    async def stop(self) -> None:
        if self.demo:
            await self.demo.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._backfill_tasks.values():
            task.cancel()
        for stream in (self._stock_stream, self._option_stream):
            if stream is not None:
                try:
                    await stream.close()
                except Exception:
                    pass

    async def _gap_fill_all(self) -> None:
        """Post-reconnect hook: repair any bar gaps the disconnect caused."""
        for symbol in list(self._stock_refs.keys()):
            try:
                await self._backfill(symbol, gap_fill=True)
            except Exception as exc:
                log.error("gap-fill %s failed: %s", symbol, exc)

    # ---------------------------------------------------------- subscriptions

    async def subscribe_stock(self, symbol: str) -> None:
        symbol = symbol.upper()
        async with self._lock:
            self._stock_refs[symbol] = self._stock_refs.get(symbol, 0) + 1
            first = self._stock_refs[symbol] == 1
        if not self.alpaca.configured:
            if self.demo:
                await self.demo.ensure(symbol)
            return
        if first:
            # alpaca-py's sync subscribe_* blocks on run_coroutine_threadsafe(
            # ...).result() against THIS loop when the stream is live — calling
            # it from the loop thread deadlocks the whole process. Always hop
            # to a worker thread so .result() waits off-loop.
            await self.alpaca.call(
                self._stock_stream.subscribe_bars, self._on_stock_bar, symbol
            )
            await self.alpaca.call(
                self._stock_stream.subscribe_quotes, self._on_stock_quote, symbol
            )
        # Backfill runs as a background task: the caller (often the WS receive
        # loop) must not stall for seconds of REST paging — subscribers get the
        # cached snapshot immediately and a fresh bars_snapshot is pushed when
        # the backfill lands.
        self._kick_backfill(symbol)

    def _kick_backfill(self, symbol: str) -> None:
        if symbol in self._backfilled or symbol in self._backfill_tasks:
            return
        task = asyncio.create_task(self._backfill_and_mark(symbol), name=f"backfill-{symbol}")
        self._backfill_tasks[symbol] = task

    async def _backfill_and_mark(self, symbol: str) -> None:
        try:
            await self._backfill(symbol)
            self._backfilled.add(symbol)
        except Exception as exc:
            log.error("backfill %s failed: %s", symbol, exc)
        finally:
            self._backfill_tasks.pop(symbol, None)

    async def ensure_backfilled(self, symbol: str) -> None:
        """Await completion of the symbol's backfill (REST callers that want
        the data in-hand rather than streamed)."""
        symbol = symbol.upper()
        self._kick_backfill(symbol)
        task = self._backfill_tasks.get(symbol)
        if task is not None:
            await asyncio.shield(task)

    async def unsubscribe_stock(self, symbol: str) -> None:
        symbol = symbol.upper()
        async with self._lock:
            count = self._stock_refs.get(symbol, 0) - 1
            if count <= 0:
                self._stock_refs.pop(symbol, None)
            else:
                self._stock_refs[symbol] = count
        # Bars stay cached; we keep upstream bar subscription (30-symbol budget
        # is plenty for an app watching a handful of underlyings) so re-focus
        # is instant. Only quotes for options are aggressively pruned.

    async def subscribe_options(self, symbols: list[str]) -> None:
        fresh = []
        async with self._lock:
            for sym in symbols:
                self._option_refs[sym] = self._option_refs.get(sym, 0) + 1
                if self._option_refs[sym] == 1:
                    fresh.append(sym)
        if fresh and self.alpaca.configured:
            await self.alpaca.call(
                self._option_stream.subscribe_quotes, self._on_option_quote, *fresh
            )

    async def unsubscribe_options(self, symbols: list[str]) -> None:
        gone = []
        async with self._lock:
            for sym in symbols:
                count = self._option_refs.get(sym, 0) - 1
                if count <= 0:
                    self._option_refs.pop(sym, None)
                    gone.append(sym)
                else:
                    self._option_refs[sym] = count
        if gone and self.alpaca.configured:
            await self.alpaca.call(self._option_stream.unsubscribe_quotes, *gone)
            for sym in gone:
                self._latest_quotes.pop(sym, None)

    # -------------------------------------------------------------- backfill

    async def _backfill(self, symbol: str, gap_fill: bool = False) -> None:
        """Hydrate from Redis, then REST: SIP for >16min-old, IEX for the rest."""
        if not gap_fill:
            count = await self.bars.hydrate(symbol)
            if count:
                log.info("hydrated %d 1m bars for %s from redis", count, symbol)

        now = datetime.now(timezone.utc)
        last = self.bars.last_ts(symbol)
        if last is not None:
            start = datetime.fromtimestamp(last / 1000, tz=timezone.utc)
        else:
            start = now - timedelta(days=self.settings.bar_cache_days * 2)  # calendar pad
        sip_cutoff = now - timedelta(seconds=SIP_DELAY_S)

        fetched: list[dict] = []
        if self.alpaca.stock_feed == DataFeed.IEX and start < sip_cutoff:
            # Free tier: SIP history is allowed (and far more accurate) for the
            # old segment; IEX covers the last 16 minutes.
            fetched += await self._fetch_bars(symbol, start, sip_cutoff, DataFeed.SIP)
            fetched += await self._fetch_bars(symbol, sip_cutoff, now, DataFeed.IEX)
        else:
            fetched += await self._fetch_bars(symbol, start, now, self.alpaca.stock_feed)

        if fetched:
            await self.bars.bulk_insert_1m(symbol, fetched)
            log.info("backfilled %d 1m bars for %s (gap_fill=%s)", len(fetched), symbol, gap_fill)
            # Push a fresh snapshot so connected clients repair their charts.
            for tf in ("1m", "5m", "15m", "1h"):
                self.broadcast.publish(
                    f"bars:{symbol}:{tf}",
                    {"t": "bars_snapshot", "symbol": symbol, "tf": tf,
                     "bars": self.bars.get_bars(symbol, tf)},
                )

    async def _fetch_bars(self, symbol: str, start: datetime, end: datetime, feed: DataFeed) -> list[dict]:
        if end <= start:
            return []
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=feed,
            limit=None,
        )
        try:
            result = await self.alpaca.call(self.alpaca.stock_data.get_stock_bars, request)
        except Exception as exc:
            log.error("get_stock_bars %s %s failed: %s", symbol, feed, exc)
            return []
        bars = result.data.get(symbol, [])
        return [_bar_from_alpaca(b) for b in bars]

    async def refresh_option_quotes(self, symbols: list[str], max_age_s: float = 30.0) -> None:
        """REST fallback for option quotes: the stream is the fast path, but
        illiquid legs (condor wings) may not tick for minutes and NOTHING
        ticks after hours — and the exit enforcer needs a mid for EVERY leg
        to evaluate TP/SL. Fetch any leg whose cached quote is missing or
        older than max_age_s, publish results as normal oquote messages
        (cache + broadcast, so waiting monitors wake). Throttled per symbol
        so quiet markets don't hammer the API. Keyless mode synthesizes
        quotes from the public-feed spot instead."""
        now_ms = time.time() * 1000
        now_mono = time.monotonic()
        stale = [
            s for s in symbols
            if (
                (q := self._latest_quotes.get(s)) is None
                or now_ms - q.get("ts", 0) > max_age_s * 1000
            )
            and now_mono - self._oquote_fetch_ts.get(s, 0.0) >= max_age_s
        ]
        if not stale:
            return
        for sym in stale:
            self._oquote_fetch_ts[sym] = now_mono
        if not self.alpaca.configured:
            if self.demo:
                self.demo.publish_option_quotes(stale)
            return
        try:
            request = OptionLatestQuoteRequest(
                symbol_or_symbols=stale, feed=self.alpaca.option_feed
            )
            result = await self.alpaca.call(
                self.alpaca.option_data.get_option_latest_quote, request, retries=1
            )
        except Exception as exc:
            log.warning("option quote refresh failed for %s: %s", stale, exc)
            return
        for sym, quote in result.items():
            msg = self._quote_msg("oquote", sym, quote.bid_price, quote.ask_price,
                                  quote.timestamp,
                                  getattr(quote, "bid_size", None),
                                  getattr(quote, "ask_size", None))
            self._latest_quotes[sym] = msg
            self.broadcast.publish(f"oquote:{sym}", msg)
        missing = [s for s in stale if s not in result]
        if missing:
            log.warning("no option quote available (even via REST) for %s", missing)

    async def fetch_latest_stock_quote(self, symbol: str) -> dict | None:
        """REST fallback when no stream quote has arrived yet."""
        cached = self._latest_quotes.get(symbol)
        if cached:
            return cached
        if not self.alpaca.configured:
            return None
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.alpaca.stock_feed)
            result = await self.alpaca.call(self.alpaca.stock_data.get_stock_latest_quote, request)
            quote = result[symbol]
            msg = self._quote_msg("quote", symbol, quote.bid_price, quote.ask_price,
                                  quote.timestamp,
                                  getattr(quote, "bid_size", None),
                                  getattr(quote, "ask_size", None))
            self._latest_quotes[symbol] = msg
            return msg
        except Exception as exc:
            log.error("latest quote %s failed: %s", symbol, exc)
            return None

    # -------------------------------------------------------------- handlers

    def _quote_msg(self, kind: str, symbol: str, bid, ask, ts,
                   bid_size=None, ask_size=None) -> dict:
        bid = float(bid or 0)
        ask = float(ask or 0)
        mid = (bid + ask) / 2 if bid and ask else (ask or bid)
        return {
            "t": kind,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": round(mid, 4),
            # Book pressure feeds the microprice trigger (fair_value.py).
            "bid_size": float(bid_size) if bid_size else None,
            "ask_size": float(ask_size) if ask_size else None,
            "ts": int(ts.timestamp() * 1000) if ts else int(time.time() * 1000),
        }

    async def _on_stock_bar(self, bar) -> None:
        self._last_stream_msg = time.monotonic()
        symbol = bar.symbol
        updates = await self.bars.upsert_1m(symbol, _bar_from_alpaca(bar))
        for tf, updated in updates:
            self.broadcast.publish(
                f"bars:{symbol}:{tf}",
                {"t": "bar", "symbol": symbol, "tf": tf, "bar": updated},
            )

    async def _on_stock_quote(self, quote) -> None:
        self._last_stream_msg = time.monotonic()
        msg = self._quote_msg("quote", quote.symbol, quote.bid_price, quote.ask_price,
                              quote.timestamp,
                              getattr(quote, "bid_size", None),
                              getattr(quote, "ask_size", None))
        self._latest_quotes[quote.symbol] = msg
        self.broadcast.publish(f"quote:{quote.symbol}", msg)
        await self.redis.set_json(f"quote:{quote.symbol}", json.dumps(msg), ttl_seconds=60)

    async def _on_option_quote(self, quote) -> None:
        self._last_stream_msg = time.monotonic()
        msg = self._quote_msg("oquote", quote.symbol, quote.bid_price, quote.ask_price,
                              quote.timestamp,
                              getattr(quote, "bid_size", None),
                              getattr(quote, "ask_size", None))
        self._latest_quotes[quote.symbol] = msg
        self.broadcast.publish(f"oquote:{quote.symbol}", msg)
