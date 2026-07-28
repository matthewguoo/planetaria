from fastapi import APIRouter, HTTPException, Query, Request

from app.services.bar_store import TF_MS
from app.services.symbols import search as symbol_search

router = APIRouter(prefix="/api", tags=["market-data"])


@router.get("/symbols/search")
async def symbols_search(q: str = Query(""), limit: int = Query(8, ge=1, le=25)) -> dict:
    return {"results": symbol_search(q, limit)}


@router.get("/bars/{symbol}")
async def get_bars(
    request: Request,
    symbol: str,
    tf: str = Query("1m"),
    limit: int = Query(1000, ge=1, le=50_000),
) -> dict:
    if tf not in TF_MS:
        raise HTTPException(400, f"unknown timeframe {tf!r}; valid: {sorted(TF_MS)}")
    market = request.app.state.market
    symbol = symbol.upper()
    await market.subscribe_stock(symbol)  # idempotent; kicks backfill on first touch
    await market.unsubscribe_stock(symbol)
    if market.alpaca.configured:
        await market.ensure_backfilled(symbol)  # REST callers want data in-hand
    return {"symbol": symbol, "tf": tf, "bars": market.bars.get_bars(symbol, tf, limit)}


@router.get("/quote/{symbol}")
async def get_quote(request: Request, symbol: str) -> dict:
    market = request.app.state.market
    quote = await market.fetch_latest_stock_quote(symbol.upper())
    if quote is None:
        raise HTTPException(503, "quote unavailable (keys not configured or feed down)")
    return quote


@router.get("/status")
async def get_status(request: Request) -> dict:
    return request.app.state.market.status()
