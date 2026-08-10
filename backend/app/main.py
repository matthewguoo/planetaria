"""ASGI entrypoint. All construction/wiring lives in app.bootstrap; all
behavior lives in app.services.*. Keep this file boring."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import bootstrap
from app.api.routes.market_data import router as market_router
from app.api.routes.monitor import router as monitor_router
from app.api.routes.strategies import router as strategies_router
from app.api.routes.system import router as system_router
from app.api.routes.trading import router as trading_router
from app.config import get_settings

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
    await bootstrap.startup(app, settings)
    yield
    await bootstrap.shutdown(app)


app = FastAPI(title="planetaria", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The ENGINE API (trading commands + system ops) is always mounted; the
# UI-serving surface (the quote REST the MARKET page polls) is skipped in
# HEADLESS mode — engine-only deployments for reliability. The chain, bars
# REST and browser WebSocket fanout that also lived behind this flag went
# with the discretionary terminal (retired 2026-08-07).
app.include_router(trading_router)
app.include_router(system_router)
app.include_router(strategies_router)
app.include_router(monitor_router)
if not get_settings().headless:
    app.include_router(market_router)


@app.get("/api/health")
async def health(request: Request) -> dict:
    s = get_settings()
    reconcile = getattr(request.app.state, "reconcile_task", None)
    return {
        "status": "ok",
        "paper": s.alpaca_paper,
        "stock_feed": s.alpaca_stock_feed,
        "option_feed": s.alpaca_option_feed,
        "alpaca_keys_configured": bool(s.alpaca_api_key and s.alpaca_secret_key),
        "reconciled": reconcile is None or reconcile.done(),
    }
