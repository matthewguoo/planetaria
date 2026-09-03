from fastapi import APIRouter, HTTPException, Query, Request

from app.services.symbols import search as symbol_search

router = APIRouter(prefix="/api", tags=["market-data"])


@router.get("/symbols/search")
async def symbols_search(q: str = Query(""), limit: int = Query(8, ge=1, le=25)) -> dict:
    return {"results": symbol_search(q, limit)}


@router.get("/quote/{symbol}")
async def get_quote(request: Request, symbol: str) -> dict:
    market = request.app.state.market
    quote = await market.fetch_latest_stock_quote(symbol.upper())
    if quote is None:
        raise HTTPException(503, "quote unavailable (keys not configured or feed down)")
    return quote
