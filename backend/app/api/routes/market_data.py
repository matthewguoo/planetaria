from fastapi import APIRouter, HTTPException, Query, Request

from app.services.symbols import AssetUniverse, search as symbol_search

router = APIRouter(prefix="/api", tags=["market-data"])


def _universe(request: Request) -> AssetUniverse | None:
    universe = getattr(request.app.state, "symbols", None)
    if universe is not None:
        universe.ensure()  # background load / daily refresh, never awaited
    return universe


@router.get("/symbols/search")
async def symbols_search(
    request: Request, q: str = Query(""), limit: int = Query(8, ge=1, le=25)
) -> dict:
    """Curated names first, then the broker's whole active-equity list, every
    hit stamped with its tradability flags (see services/symbols.py)."""
    universe = _universe(request)
    if universe is None:
        return {"results": symbol_search(q, limit), "universe": False}
    return {"results": universe.search(q, limit), "universe": universe.loaded}


@router.get("/symbols/{symbol}")
async def symbol_info(request: Request, symbol: str) -> dict:
    """Tradability of ONE symbol — the free-text guard: a typed ticker is
    only accepted once the broker confirms it exists and is tradable."""
    universe = _universe(request)
    row = await universe.lookup(symbol) if universe is not None else None
    if row is None:
        raise HTTPException(404, f"{symbol.upper()} is not a tradable asset at the broker")
    return row


@router.get("/quote/{symbol}")
async def get_quote(request: Request, symbol: str) -> dict:
    market = request.app.state.market
    quote = await market.fetch_latest_stock_quote(symbol.upper())
    if quote is None:
        raise HTTPException(503, "quote unavailable (keys not configured or feed down)")
    return quote
