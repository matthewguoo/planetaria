"""Account capabilities: the persisted per-account blob, the broker-flag
refresh, the human-clicked probe, and APPLY (widen stored risk settings
to what was verified). Mounted on both servers; the live one is where it
matters."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.capabilities import ProbeRunning

router = APIRouter(prefix="/api", tags=["capabilities"])


class ProbeIn(BaseModel):
    confirm: str | None = None
    only: list[str] | None = None


async def _status(request: Request) -> dict:
    caps = request.app.state.capabilities
    stored = await request.app.state.risk.get_stored_settings()
    return caps.status(stored_risk=stored)


@router.get("/capabilities")
async def get_capabilities(request: Request) -> dict:
    return await _status(request)


@router.post("/capabilities/refresh")
async def refresh_capabilities(request: Request) -> dict:
    await request.app.state.capabilities.refresh_broker()
    return await _status(request)


@router.post("/capabilities/probe", status_code=202)
async def start_probe(request: Request, body: ProbeIn) -> dict:
    caps = request.app.state.capabilities
    try:
        await caps.start_probe(confirm=body.confirm, only=body.only)
    except ProbeRunning as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return {"started": True}


@router.post("/capabilities/probe/abort")
async def abort_probe(request: Request) -> dict:
    if not await request.app.state.capabilities.abort():
        raise HTTPException(404, "no probe running")
    return {"aborted": True}


@router.post("/capabilities/apply")
async def apply_capabilities(request: Request) -> dict:
    patch = await request.app.state.capabilities.apply_to_risk(request.app.state.risk)
    out = await _status(request)
    out["applied"] = patch
    return out
