import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    yield
    log.info("shutdown complete")


app = FastAPI(title="planetaria", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
