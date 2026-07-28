"""Alpaca client factory + sync-SDK bridging.

alpaca-py REST clients are synchronous (requests-based); every call from async
code must go through a threadpool. Streams are asyncio-based but their .run()
spawns a private event loop — we schedule stream._run_forever() on the app
loop instead (see MarketDataService).

One stream connection per feed type per account is an Alpaca hard limit; all
client construction is centralized here so that invariant is easy to keep.
"""

import logging
from functools import partial

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

        self.stock_feed = DataFeed.SIP if settings.alpaca_stock_feed == "sip" else DataFeed.IEX
        self.option_feed = (
            OptionsFeed.OPRA if settings.alpaca_option_feed == "opra" else OptionsFeed.INDICATIVE
        )

    def make_stock_stream(self) -> StockDataStream:
        s = self.settings
        return StockDataStream(s.alpaca_api_key, s.alpaca_secret_key, feed=self.stock_feed)

    def make_option_stream(self) -> OptionDataStream:
        s = self.settings
        return OptionDataStream(s.alpaca_api_key, s.alpaca_secret_key, feed=self.option_feed)

    def make_trading_stream(self) -> TradingStream:
        s = self.settings
        return TradingStream(s.alpaca_api_key, s.alpaca_secret_key, paper=s.alpaca_paper)

    async def call(self, fn, /, *args, **kwargs):
        """Run a sync SDK call in the threadpool."""
        return await to_thread.run_sync(partial(fn, *args, **kwargs))
