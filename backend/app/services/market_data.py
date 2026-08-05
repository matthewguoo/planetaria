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
import logging
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

# A quote may anchor spot only while it is at least this close in time to the
# newest bar; beyond that the trade-derived bar close wins.
QUOTE_VS_BAR_GRACE_MS = 60_000

# Overnight (Blue Ocean ATS via Alpaca's `overnight` feed): the 20:00-04:00 ET
# session that runs Sun 20:00 -> Fri 04:00. Only the latest-trade REST
# endpoint carries it on the free tier, so we poll and roll our own 1m bars.
OVERNIGHT_POLL_S = 10.0
OVERNIGHT_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest?feed=overnight"


def is_overnight_et(now_utc: datetime) -> bool:
    """True inside the Blue Ocean overnight session (ET):
    Sun-Thu 20:00 -> next-day 04:00 (so Mon-Fri 00:00-04:00 and Sun-Thu
    20:00-24:00). Sat is fully closed; Fri closes at 04:00."""
    from zoneinfo import ZoneInfo

    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    minute = et.hour * 60 + et.minute
    dow = et.weekday()  # Mon=0 .. Sun=6
    if dow == 5:  # Sat
        return False
    if dow == 6:  # Sun: session opens 20:00
        return minute >= 1200
    if dow == 4:  # Fri: only the tail of Thu's session
        return minute < 240
    return minute < 240 or minute >= 1200  # Mon-Thu


def freshest_spot(quote: dict | None, last_bar: dict | None) -> float:
    """Freshest defensible spot price.

    A quote wins only while its timestamp is no older than the last 1m bar's
    close (minus a 60s grace). A frozen quote (thin or closed book) losing to
    a printing tape is exactly the failure mode this guards: quotes from
    yesterday's close must not anchor pricing 2 points off today's trades.
    """
    q_ms = quote["ts"] if quote and quote.get("mid") else None
    bar_close_ms = last_bar["t"] + 60_000 if last_bar else None
    if q_ms is not None and (bar_close_ms is None or q_ms >= bar_close_ms - QUOTE_VS_BAR_GRACE_MS):
        return float(quote["mid"])
    if last_bar is not None:
        return float(last_bar["c"])
    return float(quote["mid"]) if quote and quote.get("mid") else 0.0


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
        self._quote_fetch_at: dict[str, float] = {}  # symbol -> monotonic REST attempt
        self._overnight_bars: dict[str, dict] = {}  # symbol -> forming 1m bar
        self._overnight_trade_ids: dict[str, int] = {}  # dedupe volume across polls
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
            asyncio.create_task(
                supervise("overnight-poll", self._overnight_poll_loop),
                name="overnight-poll",
            ),
            asyncio.create_task(
                supervise("silent-stream-gap-fill", self._gap_fill_loop),
                name="silent-stream-gap-fill",
            ),
        ]

    async def _gap_fill_loop(self) -> None:
        """IEX only operates 08:00-17:00 ET, so the free real-time stream is
        silent through 04:00-08:00 premarket (and thin elsewhere). Whenever a
        subscribed symbol's bars fall >3 min behind during the 04:00-20:00 ET
        session, re-run the REST backfill — its SIP segment (allowed >16 min
        back on this tier) keeps the chart trailing ~16 min instead of frozen
        at the last IEX print."""
        while True:
            await asyncio.sleep(120)
            now = datetime.now(timezone.utc)
            if is_overnight_et(now):  # overnight poller owns that window
                continue
            from zoneinfo import ZoneInfo

            et = now.astimezone(ZoneInfo("America/New_York"))
            minute = et.hour * 60 + et.minute
            if et.weekday() >= 5 or minute < 240 or minute >= 1200:
                continue
            now_ms = now.timestamp() * 1000
            for symbol in list(self._stock_refs.keys()):
                last = self.bars.last_ts(symbol)
                if last is not None and now_ms - last < 3 * 60_000:
                    continue
                try:
                    await self._backfill(symbol, gap_fill=True)
                except Exception as exc:
                    log.warning("silent-stream gap-fill %s failed: %s", symbol, exc)

    async def _overnight_poll_loop(self) -> None:
        """Blue Ocean overnight freshness: while the 20:00-04:00 ET session is
        live, poll each subscribed symbol's latest overnight trade (~10s) and
        roll our own 1m bars — the IEX stream is silent in that window and a
        chart pinned to the 8pm close would anchor pricing a point or more off
        the tape that Blue Ocean is actually printing."""
        import httpx

        headers = {
            "APCA-API-KEY-ID": self.settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
        }
        async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
            while True:
                await asyncio.sleep(OVERNIGHT_POLL_S)
                if not is_overnight_et(datetime.now(timezone.utc)):
                    self._overnight_bars.clear()
                    self._overnight_trade_ids.clear()
                    continue
                for symbol in list(self._stock_refs.keys()):
                    try:
                        await self._poll_overnight_symbol(client, symbol)
                    except Exception as exc:
                        log.warning("overnight poll %s failed: %s", symbol, exc)

    async def _poll_overnight_symbol(self, client, symbol: str) -> None:
        resp = await client.get(OVERNIGHT_URL.format(symbol=symbol))
        if resp.status_code != 200:
            return
        trade = (resp.json() or {}).get("trade") or {}
        price = float(trade.get("p") or 0)
        size = int(trade.get("s") or 0)
        raw_ts = trade.get("t")
        trade_id = trade.get("i")
        if price <= 0 or not raw_ts:
            return
        ts = datetime.fromisoformat(raw_ts.split(".")[0] + "+00:00")
        ts_ms = int(ts.timestamp() * 1000)
        # A weekend-stale print must not masquerade as a live tick.
        if time.time() * 1000 - ts_ms > 20 * 60_000:
            return

        cached = self._latest_quotes.get(symbol)
        if cached is None or ts_ms >= cached["ts"]:
            # A trade print, not a book: bid == ask == last. freshest_spot and
            # the header treat it as the live price; the O/N phase chip and
            # zero spread make the provenance readable.
            msg = {"t": "quote", "symbol": symbol, "bid": price, "ask": price,
                   "mid": price, "ts": ts_ms, "src": "overnight"}
            self._latest_quotes[symbol] = msg
            self.broadcast.publish(f"quote:{symbol}", msg)

        minute_ts = ts_ms - (ts_ms % 60_000)
        bar = self._overnight_bars.get(symbol)
        is_new_trade = self._overnight_trade_ids.get(symbol) != trade_id
        self._overnight_trade_ids[symbol] = trade_id
        if bar is None or bar["t"] != minute_ts:
            bar = {"t": minute_ts, "o": price, "h": price, "l": price, "c": price, "v": 0}
            self._overnight_bars[symbol] = bar
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price
        if is_new_trade:
            bar["v"] += size
        updates = await self.bars.upsert_1m(symbol, dict(bar))
        for tf, updated in updates:
            self.broadcast.publish(
                f"bars:{symbol}:{tf}",
                {"t": "bar", "symbol": symbol, "tf": tf, "bar": updated},
            )

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
        for sym in gone:
            self._oquote_fetch_ts.pop(sym, None)
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
        """Latest quote with staleness-aware refresh.

        The cache must NOT be forever: outside regular hours (and on the thin
        IEX book generally) the quote stream can go silent while trades keep
        printing — a quote cached at yesterday's close would otherwise anchor
        spot 2+ points off this morning's tape. Refresh over REST when the
        cached quote is older than 10s, throttled to one attempt per 5s per
        symbol, and never let an older broker quote overwrite a newer one.
        """
        cached = self._latest_quotes.get(symbol)
        now_ms = time.time() * 1000
        if cached and now_ms - cached["ts"] < 10_000:
            return cached
        if not self.alpaca.configured:
            return cached
        last_attempt = self._quote_fetch_at.get(symbol, 0.0)
        if time.monotonic() - last_attempt < 5.0:
            return cached
        self._quote_fetch_at[symbol] = time.monotonic()
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.alpaca.stock_feed)
            result = await self.alpaca.call(self.alpaca.stock_data.get_stock_latest_quote, request)
            quote = result[symbol]
            msg = self._quote_msg("quote", symbol, quote.bid_price, quote.ask_price,
                                  quote.timestamp,
                                  getattr(quote, "bid_size", None),
                                  getattr(quote, "ask_size", None))
            # Keep the newer-wins guard: a REST response carrying an older
            # broker timestamp must not clobber a fresher stream quote.
            if cached is None or msg["ts"] >= cached["ts"]:
                self._latest_quotes[symbol] = msg
            return self._latest_quotes[symbol]
        except Exception as exc:
            log.error("latest quote %s failed: %s", symbol, exc)
            return cached

    def spot(self, symbol: str) -> float:
        """Freshest defensible spot for pricing (see freshest_spot)."""
        bars = self.bars.get_bars(symbol, "1m", limit=1)
        return freshest_spot(self._latest_quotes.get(symbol), bars[-1] if bars else None)

    def equity_tape_age_s(self, symbol: str) -> float | None:
        """Seconds since SYMBOL's freshest per-symbol market evidence: the
        cached quote or the latest 1m bar. The bar counts as of its CLOSE
        (clamped to now) — the overnight poller updates the forming bar in
        place, so a bar opened 59s ago is live tape, not a minute of
        staleness. None = no evidence at all (callers must block)."""
        now_ms = time.time() * 1000
        newest = 0.0
        quote = self._latest_quotes.get(symbol)
        if quote and quote.get("ts"):
            newest = float(quote["ts"])
        bars = self.bars.get_bars(symbol, "1m", limit=1)
        if bars:
            newest = max(newest, min(float(bars[-1]["t"]) + 60_000, now_ms))
        if newest <= 0:
            return None
        return max(0.0, (now_ms - newest) / 1000)

    async def overnight_price(self, symbol: str) -> dict | None:
        """Entry-pricing quote for an equity symbol, overnight-aware.

        Outside the overnight session this is just the staleness-aware REST
        quote (fetch_latest_stock_quote). Inside it, that REST book is a
        trap: IEX closed at 17:00 and latest-quote answers with the dead
        17:00 book — observed 18 points off the live Blue Ocean tape
        (2026-08-04 e2e). So instead: BORROW a subscription (side effects
        we want: bar backfill + stream registration), poll the latest
        overnight trade ONCE so the caller isn't waiting on the next poll
        tick, then RELEASE the ref. Refs must balance here — the poll loop
        iterates _stock_refs, so an unreleased entry-pricing ref pins the
        symbol into the 10s poller forever on every skipped or risk-blocked
        entry. A plan that actually fills gets its freshness from the exit
        monitor's OWN ref (subscribe on arm, release on close); the caches
        this reads outlive the borrow. Returns the freshest of the cached
        quote vs the latest 1m bar as a quote-shaped dict (tape prints
        carry bid == ask == last); None when there is no live evidence."""
        symbol = symbol.upper()
        if not is_overnight_et(datetime.now(timezone.utc)):
            return await self.fetch_latest_stock_quote(symbol)
        await self.subscribe_stock(symbol)
        try:
            if self.alpaca.configured:
                try:
                    await self._poll_overnight_once(symbol)
                except Exception as exc:
                    log.warning("one-shot overnight poll %s failed: %s", symbol, exc)
            quote = self._latest_quotes.get(symbol)
            bars = self.bars.get_bars(symbol, "1m", limit=1)
            last_bar = bars[-1] if bars else None
            now_ms = time.time() * 1000
            q_ts = float(quote["ts"]) if quote and quote.get("mid") and quote.get("ts") else 0.0
            bar_close = min(float(last_bar["t"]) + 60_000, now_ms) if last_bar else 0.0
            if last_bar and bar_close > q_ts and float(last_bar["c"]) > 0:
                # Bars outlive the quote cache (a restart hydrates them from
                # Redis) — synthesize the tape print the bar recorded.
                price = float(last_bar["c"])
                return {"t": "quote", "symbol": symbol, "bid": price, "ask": price,
                        "mid": price, "ts": int(bar_close), "src": "overnight-bar"}
            return quote
        finally:
            await self.unsubscribe_stock(symbol)

    async def _poll_overnight_once(self, symbol: str) -> None:
        """One-shot Blue Ocean latest-trade poll (same parse/dedupe/guards as
        the poll loop) for callers that need tape freshness NOW."""
        import httpx

        headers = {
            "APCA-API-KEY-ID": self.settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
        }
        async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
            await self._poll_overnight_symbol(client, symbol)

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

    async def _on_option_quote(self, quote) -> None:
        self._last_stream_msg = time.monotonic()
        msg = self._quote_msg("oquote", quote.symbol, quote.bid_price, quote.ask_price,
                              quote.timestamp,
                              getattr(quote, "bid_size", None),
                              getattr(quote, "ask_size", None))
        self._latest_quotes[quote.symbol] = msg
        self.broadcast.publish(f"oquote:{quote.symbol}", msg)
