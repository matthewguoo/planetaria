import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.market_data import router as market_router
from app.api.websocket import router as ws_router
from app.config import get_settings
from app.services.alpaca import AlpacaService
from app.services.bar_store import BarStore
from app.services.broadcast import Broadcaster
from app.services.market_data import MarketDataService
from app.services.redis_client import RedisFacade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "starting (paper=%s, stock_feed=%s, option_feed=%s)",
        settings.alpaca_paper,
        settings.alpaca_stock_feed,
        settings.alpaca_option_feed,
    )

    redis = RedisFacade(settings.redis_url)
    await redis.connect()

    alpaca = AlpacaService(settings)
    broadcaster = Broadcaster()
    bars = BarStore(redis, max_1m_bars=settings.bar_cache_days * 3900)
    market = MarketDataService(settings, alpaca, redis, bars, broadcaster)

    app.state.settings = settings
    app.state.redis = redis
    app.state.alpaca = alpaca
    app.state.broadcaster = broadcaster
    app.state.market = market

    await market.start()
    yield
    await market.stop()
    await redis.close()
    log.info("shutdown complete")


app = FastAPI(title="planetaria", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "paper": s.alpaca_paper,
        "stock_feed": s.alpaca_stock_feed,
        "option_feed": s.alpaca_option_feed,
        "alpaca_keys_configured": bool(s.alpaca_api_key and s.alpaca_secret_key),
    }
