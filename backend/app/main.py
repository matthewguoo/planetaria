"""ASGI entrypoint. All construction/wiring lives in app.bootstrap; all
behavior lives in app.services.*. Keep this file boring."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import bootstrap
from app.api.routes.admin import router as admin_router
from app.api.routes.capabilities import router as capabilities_router
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


def _attach_file_logging() -> None:
    """The engine owns its log file: size-capped rotation, independent of
    however stdout/stderr are (or are not) being captured. Incident
    2026-09-03: the NSSM service wrote no log files after a restart's
    rotation, so a missed exit had to be diagnosed from broker order
    history; separately, a DNS outage once spewed 259MB of reconnect
    tracebacks into an uncapped stderr file. PLANETARIA_LOG_DIR overrides
    the default; PLANETARIA_LOG_DIR=off disables."""
    import os
    from logging.handlers import RotatingFileHandler
    from pathlib import Path as _Path

    configured = os.environ.get("PLANETARIA_LOG_DIR", "")
    if configured.lower() == "off":
        return
    base = configured or os.path.join(
        os.environ.get("LOCALAPPDATA", "."), "planetaria-logs")
    try:
        _Path(base).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(base, "engine.log"),
            maxBytes=50 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError as exc:  # a broken log dir must never stop the engine
        logging.getLogger("app").warning("file logging disabled: %s", exc)


_attach_file_logging()
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "starting (mode=%s, paper=%s, stock_feed=%s, option_feed=%s)",
        settings.trading_mode,
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
# deployments for reliability. On the LIVE server the strategies surface
# is replaced by a 409 stub: the runner is never constructed there, and
# the real router would 500 against the missing app.state.
app.include_router(trading_router)
app.include_router(system_router)
app.include_router(capabilities_router)
app.include_router(admin_router)
if get_settings().trading_mode == "paper":
    app.include_router(strategies_router)
else:
    from app.api.routes.live_stubs import router as live_stubs_router

    app.include_router(live_stubs_router)
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
        "mode": s.trading_mode,
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
    from fastapi.responses import FileResponse

    # Clean path for the cockpit: /terminal (the Vite entry is still
    # terminal.html on disk — a build artifact name, not a URL to type).
    @app.get("/terminal", include_in_schema=False)
    async def terminal_page():
        return FileResponse(_dist / "terminal.html")

    # The server administration window (deploy/live/admin-window.sh opens
    # it on the box's own screen): stats, the engine feed, every broker call.
    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        return FileResponse(_dist / "admin.html")

    # LIVE server: the root is the account — holdings, equity, protection —
    # not the strategy console (there is no strategy plane in this process).
    # The terminal bundle boots into its OVERVIEW view on "/"; the ops
    # console stays reachable at /index.html for the SYSTEM page.
    if get_settings().trading_mode == "live_manual":

        @app.get("/", include_in_schema=False)
        async def live_root():
            return FileResponse(_dist / "terminal.html")

    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")
