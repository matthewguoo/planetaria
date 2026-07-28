"""Composition root: constructs and wires every service, owns startup and
shutdown order. main.py stays a thin ASGI entrypoint."""

import asyncio
import logging

from fastapi import FastAPI

from app.config import Settings
from app.db.session import Database
from app.services.alpaca import AlpacaService
from app.services.bar_store import BarStore
from app.services.broadcast import Broadcaster
from app.services.exit_enforcer import ExitEnforcer
from app.services.market_data import MarketDataService
from app.services.options_chain import ChainService
from app.services.redis_client import RedisFacade
from app.services.risk import RiskService
from app.services.supervision import supervise
from app.services.trade_service import TradeService

log = logging.getLogger("app.bootstrap")


async def startup(app: FastAPI, settings: Settings) -> None:
    redis = RedisFacade(settings.redis_url)
    await redis.connect()

    db = Database()
    await db.connect(settings.database_url)

    alpaca = AlpacaService(settings)
    broadcaster = Broadcaster()
    bars = BarStore(redis, max_1m_bars=settings.bar_cache_days * 3900)
    market = MarketDataService(settings, alpaca, redis, bars, broadcaster)
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)

    app.state.settings = settings
    app.state.redis = redis
    app.state.db = db
    app.state.alpaca = alpaca
    app.state.broadcaster = broadcaster
    app.state.market = market
    app.state.chain = ChainService(alpaca, redis, market)
    app.state.risk = risk
    app.state.trade = trade
    app.state.enforcer = enforcer
    app.state.trading_stream = None
    app.state.trading_stream_task = None

    await market.start()

    if alpaca.configured:
        stream = alpaca.make_trading_stream()
        stream.subscribe_trade_updates(trade.on_trade_update)
        app.state.trading_stream = stream
        app.state.trading_stream_task = asyncio.create_task(
            supervise("trading-stream", stream._run_forever), name="trading-stream"
        )
        await enforcer.startup_reconcile()


async def shutdown(app: FastAPI) -> None:
    state = app.state
    await state.enforcer.shutdown()
    if state.trading_stream_task:
        state.trading_stream_task.cancel()
    if state.trading_stream is not None:
        try:
            await state.trading_stream.close()
        except Exception:
            pass
    await state.market.stop()
    await state.db.close()
    await state.redis.close()
    log.info("shutdown complete")
