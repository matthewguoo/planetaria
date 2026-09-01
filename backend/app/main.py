"""ASGI entrypoint. All construction/wiring lives in app.bootstrap; all
behavior lives in app.services.*. Keep this file boring."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import bootstrap
from app.api.routes.market_data import router as market_router
from app.api.routes.monitor import router as monitor_router
from app.api.routes.options import router as options_router
from app.api.routes.strategies import router as strategies_router
from app.api.routes.system import router as system_router
from app.api.routes.trading import router as trading_router
from app.api.websocket import router as ws_router
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
# UI-serving surface (chain, bars REST, browser WebSocket fanout, and the
# built frontend itself) is skipped in HEADLESS mode — engine-only
# deployments for reliability.
app.include_router(trading_router)
app.include_router(system_router)
app.include_router(strategies_router)
app.include_router(monitor_router)
if not get_settings().headless:
    app.include_router(market_router)
    app.include_router(options_router)
    app.include_router(ws_router)


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


# Serve the built frontend (ops console at /, terminal at /terminal.html)
# from this process so no node/vite tooling is needed at runtime. A "/"
# mount matches every path, so it MUST be registered after all API routes
# (registration order is match order); StaticFiles reads from disk per
# request, so `npm run build` redeploys the UI without a restart.
_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if not get_settings().headless and _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")
