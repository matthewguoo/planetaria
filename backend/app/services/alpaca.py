"""Alpaca client factory + sync-SDK bridging.

alpaca-py REST clients are synchronous (requests-based); every call from async
code must go through a threadpool. Streams are asyncio-based but their .run()
spawns a private event loop — we schedule stream._run_forever() on the app
loop instead (see MarketDataService).

One stream connection per feed type per account is an Alpaca hard limit; all
client construction is centralized here so that invariant is easy to keep.
"""

import asyncio
import logging
import time
from functools import partial

import websockets
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.option import OptionDataStream
from alpaca.data.live.stock import StockDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream
from anyio import to_thread

from app.config import Settings

log = logging.getLogger("app.alpaca")

# Socket-level (connect+read) timeout stamped onto every SDK request. The SDK
# ships with NO timeout — one hung TCP connection would otherwise pin a
# threadpool worker forever and freeze whichever coroutine awaits it (e.g. a
# plan's exit monitor).
REQUEST_TIMEOUT_S = 10.0
# Coroutine-level ceiling: even if the socket timeout misbehaves, the awaiting
# caller gets control back and can retry/escalate.
CALL_TIMEOUT_S = 15.0


def _stamp_session_timeout(client, timeout: float) -> None:
    """Default a timeout onto every request of the SDK client's session."""
    session = getattr(client, "_session", None)
    if session is None:  # SDK internals moved; the wait_for ceiling still holds
        log.warning("no _session on %s - socket timeouts not stamped", type(client).__name__)
        return
    original = session.request

    def timed_request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original(method, url, **kwargs)

    session.request = timed_request


def is_transient(exc: BaseException) -> bool:
    """Errors worth retrying: timeouts, connection drops, 429/5xx."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    try:  # requests-level failures (SDK transport)
        import requests

        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    status = getattr(exc, "status_code", None)
    return status is not None and (status == 429 or status >= 500)


IDLE_POLL_S = 0.5          # how often an unsubscribed stream checks for handlers
REFUSAL_BACKOFF_MAX_S = 60.0


class _PatientStreamMixin:
    """alpaca-py's DataStream._run_forever burns a core in two places, both
    measured on the live box 2026-09-03 (100% CPU for ten hours): it
    busy-waits with sleep(0) until the first subscription, and it retries a
    REFUSED connection — an auth-time ValueError such as "connection limit
    exceeded", which is what the free data plan returns when another
    process already holds the account's one websocket — with no delay at
    all. This override keeps the SDK loop's exact semantics (stop queue,
    subscribe-on-connect, the 'insufficient subscription' bail-out) and adds
    the two missing sleeps. While refused, quotes come from REST polling
    (the enforcer's own path); the stream is retried once a minute and
    picked up the moment it is free."""

    async def _run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not any(
            v for k, v in self._handlers.items()
            if k not in ("cancelErrors", "corrections")
        ):
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                return
            await asyncio.sleep(IDLE_POLL_S)
        log.info("started %s stream", self._name)
        self._should_run = True
        self._running = False
        refusals = 0
        while True:
            try:
                if not self._should_run:
                    log.info("%s stream stopped", self._name)
                    return
                if not self._running:
                    log.info("starting %s websocket connection", self._name)
                    await self._start_ws()
                    await self._send_subscribe_msg()
                    self._running = True
                    refusals = 0
                await self._consume()
            except websockets.WebSocketException as wse:
                await self.close()
                self._running = False
                log.warning("data websocket error, restarting connection: %s", wse)
            except ValueError as ve:
                if "insufficient subscription" in str(ve):
                    await self.close()
                    self._running = False
                    log.exception("error during websocket communication: %s", ve)
                    return
                refusals += 1
                delay = min(REFUSAL_BACKOFF_MAX_S, 2.0 ** refusals)
                log.error("%s stream refused (%s) - retrying in %.0fs "
                          "(quotes fall back to REST polling meanwhile)",
                          self._name, ve, delay)
                await self.close()
                self._running = False
                await asyncio.sleep(delay)
            except Exception as e:
                # A dead socket surfacing as OSError / ConnectionReset / a
                # decode error must not re-enter _consume() at zero delay —
                # that is the same 100%-CPU class as the two cases above.
                # Reconnect with the refusal backoff, resetting the client
                # so the next pass builds a fresh connection.
                refusals += 1
                delay = min(REFUSAL_BACKOFF_MAX_S, 2.0 ** refusals)
                log.exception("%s stream error (%s) - reconnecting in %.0fs",
                              self._name, e, delay)
                await self.close()
                self._running = False
                await asyncio.sleep(delay)
            finally:
                await asyncio.sleep(0)


class PatientStockDataStream(_PatientStreamMixin, StockDataStream):
    pass


class PatientOptionDataStream(_PatientStreamMixin, OptionDataStream):
    pass


class AlpacaService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.configured = bool(settings.alpaca_api_key and settings.alpaca_secret_key)
        if not self.configured:
            log.warning("Alpaca keys not configured - market data and trading disabled")
            return

        key, secret = settings.alpaca_api_key, settings.alpaca_secret_key
        self.trading = TradingClient(key, secret, paper=settings.alpaca_paper)
        self.stock_data = StockHistoricalDataClient(key, secret)
        self.option_data = OptionHistoricalDataClient(key, secret)
        for client in (self.trading, self.stock_data, self.option_data):
            _stamp_session_timeout(client, REQUEST_TIMEOUT_S)

        self.stock_feed = DataFeed.SIP if settings.alpaca_stock_feed == "sip" else DataFeed.IEX
        self.option_feed = (
            OptionsFeed.OPRA if settings.alpaca_option_feed == "opra" else OptionsFeed.INDICATIVE
        )

    def make_stock_stream(self) -> StockDataStream:
        s = self.settings
        return PatientStockDataStream(s.alpaca_api_key, s.alpaca_secret_key,
                                      feed=self.stock_feed)

    def make_option_stream(self) -> OptionDataStream:
        s = self.settings
        return PatientOptionDataStream(s.alpaca_api_key, s.alpaca_secret_key,
                                       feed=self.option_feed)

    def make_trading_stream(self) -> TradingStream:
        s = self.settings
        return TradingStream(s.alpaca_api_key, s.alpaca_secret_key, paper=s.alpaca_paper)

    def make_news_stream(self):
        from alpaca.data.live.news import NewsDataStream

        s = self.settings
        return NewsDataStream(s.alpaca_api_key, s.alpaca_secret_key)

    async def call(self, fn, /, *args, timeout: float = CALL_TIMEOUT_S,
                   retries: int = 0, **kwargs):
        """Run a sync SDK call in the threadpool, bounded and optionally
        retried.

        `timeout` caps how long the CALLER waits — the awaiting coroutine
        always gets control back, so a wedged broker API can never freeze an
        exit monitor. `retries` re-attempts transient failures (timeout,
        connection error, 429/5xx) with exponential backoff. Only use
        retries on idempotent calls (reads, cancels); order SUBMITS must
        instead go through client_order_id recovery (see TradeService) since
        a timed-out submit may still have reached the broker.
        """
        from app.services.call_log import CALL_LOG

        attempt = 0
        while True:
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    to_thread.run_sync(partial(fn, *args, **kwargs)), timeout
                )
                CALL_LOG.record("broker", getattr(fn, "__name__", str(fn)),
                                ms=(time.monotonic() - started) * 1000)
                return result
            except Exception as exc:
                CALL_LOG.record("broker", getattr(fn, "__name__", str(fn)),
                                detail=str(exc)[:120],
                                ms=(time.monotonic() - started) * 1000, ok=False)
                attempt += 1
                if attempt > retries or not is_transient(exc):
                    raise
                delay = min(0.5 * (2 ** (attempt - 1)), 4.0)
                log.warning(
                    "transient broker error on %s (attempt %d/%d, retry in %.1fs): %s",
                    getattr(fn, "__name__", fn), attempt, retries + 1, delay, exc,
                )
                await asyncio.sleep(delay)
