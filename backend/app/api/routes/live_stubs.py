"""Stub routers mounted ONLY on the live server (TRADING_MODE=live_manual).

The real strategies router cannot be mounted there: its handlers all
dereference app.state.strategy_runner, which the live bootstrap never
constructs, so every call would 500. This stub answers the whole
/api/strategies surface with a designed 409 — the UI reads it as "this
server does not do automation", not as a broken deploy."""

from fastapi import APIRouter, HTTPException

router = APIRouter()

_DETAIL = ("strategies are not available on the live server - "
           "manual entries and protective exits only")

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@router.api_route("/api/strategies", methods=_METHODS, include_in_schema=False)
@router.api_route("/api/strategies/{path:path}", methods=_METHODS,
                  include_in_schema=False)
async def strategies_stub(path: str = "") -> None:
    raise HTTPException(status_code=409, detail=_DETAIL)


@router.api_route("/api/signals", methods=_METHODS, include_in_schema=False)
@router.api_route("/api/signals/{path:path}", methods=_METHODS,
                  include_in_schema=False)
@router.api_route("/api/fund", methods=_METHODS, include_in_schema=False)
async def signals_stub(path: str = "") -> None:
    # The signal store and the fund view (per-strategy books) are part of
    # the strategy plane; they do not exist in the live process either.
    raise HTTPException(status_code=409, detail=_DETAIL)
