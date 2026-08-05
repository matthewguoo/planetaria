"""Live call-flow monitor: what the process is saying to whom, right now.
Three categories from one ring (see services/call_log.py): data-source
HTTP, broker REST, internal engine chokepoints."""

from fastapi import APIRouter

from app.services.call_log import CALL_LOG

router = APIRouter(prefix="/api", tags=["monitor"])


@router.get("/monitor/calls")
async def calls(category: str | None = None, limit: int = 100) -> dict:
    return {
        "status": CALL_LOG.status(),
        "calls": CALL_LOG.recent(category, min(limit, 300)),
    }
