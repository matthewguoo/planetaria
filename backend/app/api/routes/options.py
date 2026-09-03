from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api", tags=["options"])


@router.get("/options/chain/{underlying}")
async def get_chain(request: Request, underlying: str, dte_max: int = Query(3, ge=0, le=7)) -> dict:
    try:
        return await request.app.state.chain.get_chain(underlying, dte_max)
    except Exception as exc:
        raise HTTPException(502, f"chain fetch failed: {exc}")
