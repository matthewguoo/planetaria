from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api", tags=["market-data"])


@router.get("/quote/{symbol}")
async def get_quote(request: Request, symbol: str) -> dict:
    market = request.app.state.market
    quote = await market.fetch_latest_stock_quote(symbol.upper())
    if quote is None:
        raise HTTPException(503, "quote unavailable (keys not configured or feed down)")
    return quote
