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

    app.state.reconcile_task = None
    app.state.reconcile_loop_task = None
    if alpaca.configured:
        stream = alpaca.make_trading_stream()
        stream.subscribe_trade_updates(trade.on_trade_update)
        app.state.trading_stream = stream
        app.state.trading_stream_task = asyncio.create_task(
            supervise("trading-stream", stream._run_forever), name="trading-stream"
        )
        # Reconcile in the background: it makes serial broker REST calls, and
        # while the lifespan is awaiting, uvicorn accepts NO connections — this
        # is exactly the "server up but nothing connects" startup window.
        app.state.reconcile_task = asyncio.create_task(
            _reconcile_with_retry(enforcer), name="startup-reconcile"
        )
        # Periodic REST truth-sync: the TradingStream is the fast path for
        # fills, but a fill landing during a stream gap must not leave a live
        # position unmanaged. Also re-arms any open plan missing its monitor.
        app.state.reconcile_loop_task = asyncio.create_task(
            enforcer.reconcile_loop(), name="reconcile-loop"
        )


async def _reconcile_with_retry(enforcer: ExitEnforcer, attempts: int = 5) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await enforcer.startup_reconcile()
            log.info("startup reconcile complete")
            return
        except Exception:
            log.exception("startup reconcile failed (attempt %d/%d)", attempt, attempts)
            await asyncio.sleep(min(2**attempt, 30))
    log.error("startup reconcile gave up after %d attempts", attempts)


async def shutdown(app: FastAPI) -> None:
    state = app.state
    if getattr(state, "reconcile_task", None):
        state.reconcile_task.cancel()
    if getattr(state, "reconcile_loop_task", None):
        state.reconcile_loop_task.cancel()
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
