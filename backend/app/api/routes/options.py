from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services.analytics import (
    compute_heatmap,
    compute_probabilities,
    compute_sizing,
    legs_from_payload,
)

router = APIRouter(prefix="/api", tags=["options"])


@router.get("/options/chain/{underlying}")
async def get_chain(request: Request, underlying: str, dte_max: int = Query(3, ge=0, le=7)) -> dict:
    try:
        return await request.app.state.chain.get_chain(underlying, dte_max)
    except Exception as exc:
        raise HTTPException(502, f"chain fetch failed: {exc}")


class LegIn(BaseModel):
    right: str
    strike: float
    qty: int = 1
    side: int = 1
    entry: float = 0.0
    iv: float = 0.0


class AnalyticsIn(BaseModel):
    legs: list[LegIn] = Field(min_length=1, max_length=4)
    spot: float
    hours_to_expiry: float
    tp_premium: float | None = None
    sl_premium: float | None = None


class HeatmapIn(AnalyticsIn):
    price_lo: float
    price_hi: float
    price_steps: int = 80
    time_steps: int = 60


class SizingIn(AnalyticsIn):
    account_equity: float
    max_loss_pct: float = 0.02
    bp_cap_pct: float = 0.25


@router.post("/analytics/probabilities")
async def analytics_probabilities(body: AnalyticsIn) -> dict:
    try:
        legs = legs_from_payload([leg.model_dump() for leg in body.legs])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return compute_probabilities(
        legs, body.spot, body.hours_to_expiry, body.tp_premium, body.sl_premium
    )


@router.post("/analytics/heatmap")
async def analytics_heatmap(body: HeatmapIn) -> dict:
    try:
        legs = legs_from_payload([leg.model_dump() for leg in body.legs])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return compute_heatmap(
        legs, body.spot, body.hours_to_expiry,
        body.price_lo, body.price_hi, body.price_steps, body.time_steps,
    )


@router.post("/analytics/sizing")
async def analytics_sizing(body: SizingIn) -> dict:
    try:
        legs = legs_from_payload([leg.model_dump() for leg in body.legs])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if body.sl_premium is None:
        raise HTTPException(422, "sl_premium required for sizing")
    from dataclasses import asdict

    return asdict(
        compute_sizing(legs, body.account_equity, body.max_loss_pct, body.sl_premium, body.bp_cap_pct)
    )
