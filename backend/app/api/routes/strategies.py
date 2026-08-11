"""Strategy control plane. Engine API — mounted unconditionally (survives
HEADLESS); the GUI is just another client of these routes with no privileged
path. Thin adapters over app.state.strategy_runner, same error mapping as
trading.py (ValueError -> 422)."""

import inspect
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.signals.events import Event
from app.services.sim_account import (
    DEFAULT_EQUITY,
    DEFAULT_FLAT_PCT,
    signals_from_decisions,
    simulate,
)
from app.strategies import REGISTRY

router = APIRouter(prefix="/api", tags=["strategies"])

# The twin replays the whole journal, not a page of it — a curve built from
# the most recent 100 decisions is not the account's history, it is a
# suffix of it.
TWIN_MAX_DECISIONS = 5_000


class CreateIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=24)
    params: dict = Field(default_factory=dict)


class ParamsIn(BaseModel):
    params: dict


class AllocationIn(BaseModel):
    mode: str = Field(pattern="^(pct|usd)$")
    value: float = Field(gt=0)


class BreakerIn(BaseModel):
    enabled: bool = True
    # `pct` is of the strategy's ALLOCATION, not of the account: a breaker
    # must not silently loosen because the account grew.
    mode: str = Field(pattern="^(pct|usd)$")
    value: float = Field(gt=0)


class KillAllIn(BaseModel):
    flatten: bool = False


class TriggerIn(BaseModel):
    payload: dict = Field(default_factory=dict)


@router.get("/strategies/catalog")
async def catalog(request: Request) -> dict:
    """Every registered kind, plus what the platform currently provides
    against what those kinds declare they need."""
    return {
        "kinds": [
            {
                "kind": cls.kind,
                "subscriptions": list(cls.subscriptions),
                "default_params": cls.default_params,
                "requires": sorted(cls.requires),
                "doc": (cls.__doc__ or "").strip().split("\n")[0],
                "registered": cls.registered,
            }
            for cls in REGISTRY.values()
        ],
        "capabilities": await request.app.state.strategy_runner.capability_state(),
    }


@router.get("/strategies/catalog/{kind}/source")
async def catalog_source(kind: str) -> dict:
    """The strategy's actual module source — what the machine is really
    running, served read-only so the UI can show it next to the journal.
    Module (not class) source on purpose: schemas, constants, and helpers
    are part of the behavior."""
    cls = REGISTRY.get(kind)
    if cls is None:
        raise HTTPException(404, f"unknown strategy kind {kind!r}")
    module = inspect.getmodule(cls)
    try:
        source = inspect.getsource(module)
        file = Path(inspect.getsourcefile(module) or "?").name
    except (OSError, TypeError) as exc:
        raise HTTPException(500, f"source unavailable: {exc}")
    return {"kind": kind, "module": module.__name__, "file": file,
            "source": source}


@router.get("/strategies")
async def list_instances(request: Request) -> dict:
    return {"instances": await request.app.state.strategy_runner.instances()}


@router.post("/strategies")
async def create_instance(request: Request, body: CreateIn) -> dict:
    try:
        return await request.app.state.strategy_runner.create(
            body.kind, body.name, body.params
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/strategies/{row_id}")
async def get_instance(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/strategies/{row_id}")
async def delete_instance(request: Request, row_id: str) -> dict:
    """Remove an instance and its decision journal. Refused while it holds
    open plans — flatten first."""
    try:
        return await request.app.state.strategy_runner.delete(row_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.patch("/strategies/{row_id}")
async def patch_params(request: Request, row_id: str, body: ParamsIn) -> dict:
    try:
        return await request.app.state.strategy_runner.update_params(row_id, body.params)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.put("/strategies/{row_id}/allocation")
async def set_allocation(request: Request, row_id: str, body: AllocationIn) -> dict:
    """How much of the account this strategy may deploy — a percent of equity
    or a fixed dollar ceiling. Read per-decision through ctx.account() and
    enforced in execute_intent, so a change lands on the next event without
    restarting a strategy mid-night."""
    try:
        return await request.app.state.strategy_runner.set_allocation(
            row_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/strategies/{row_id}/capital")
async def capital(request: Request, row_id: str) -> dict:
    """Allocated / deployed / available, plus the drawdown against this
    strategy's circuit breaker."""
    runner = request.app.state.strategy_runner
    try:
        await runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {**await runner.allocation_state(row_id),
            "breaker": await runner.breaker_state(row_id)}


@router.put("/strategies/{row_id}/breaker")
async def set_breaker(request: Request, row_id: str, body: BreakerIn) -> dict:
    """The drawdown that flattens this strategy's book and pauses it.

    This is what replaced the per-position stop for strategies that run
    unbracketed: a stop bounds one trade and costs edge on every trade, a
    breaker bounds the strategy and costs nothing until it fires.
    """
    try:
        return await request.app.state.strategy_runner.set_breaker(
            row_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/enable")
async def enable(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.set_state(row_id, "enabled")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/pause")
async def pause(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.set_state(row_id, "paused")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/{row_id}/flatten")
async def flatten(request: Request, row_id: str) -> dict:
    try:
        return await request.app.state.strategy_runner.flatten(row_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/strategies/kill-all")
async def kill_all(request: Request, body: KillAllIn) -> dict:
    return await request.app.state.strategy_runner.kill_all(body.flatten)


@router.post("/strategies/{row_id}/trigger")
async def trigger(request: Request, row_id: str, body: TriggerIn) -> dict:
    """Journal a manual signal and publish it scoped to this instance —
    the operator's entry point for a command the strategy understands, not a
    trading path -- strategies are autonomous."""
    runner = request.app.state.strategy_runner
    try:
        instance = await runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if instance["state"] != "enabled":
        raise HTTPException(422, f"instance is {instance['state']}, not enabled")
    store = request.app.state.signal_store
    now = datetime.now(timezone.utc)
    event = Event(
        type="manual", ts=now, source="api-trigger",
        key=f"{row_id}:{now.isoformat()}", symbols=(),
        payload={"strategy_id": row_id, **body.payload},
    )
    event, _ = await store.record(event)
    request.app.state.event_bus.publish(event)
    return {"ok": True, "signal_id": event.signal_id}


@router.get("/strategies/{row_id}/decisions")
async def decisions(request: Request, row_id: str, limit: int = 100) -> dict:
    return {"decisions": await request.app.state.strategy_runner.decisions(row_id, limit)}


@router.get("/strategies/{row_id}/performance")
async def performance(request: Request, row_id: str) -> dict:
    """Executions + outcomes for this instance: its plans (orders, fills,
    exits) and the realized-P&L rollup."""
    try:
        return await request.app.state.strategy_runner.performance(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/strategies/{row_id}/twin")
async def twin(request: Request, row_id: str,
               equity: float = DEFAULT_EQUITY, risk_pct: float = 0.5,
               flat_pct: float = DEFAULT_FLAT_PCT) -> dict:
    """The paper twin: this instance's journaled decisions compounded into an
    equity curve, under the live vol-weighted allocator and a flat-notional
    one.

    Note-mode strategies place nothing, so `performance` above is empty for
    them by construction and a shadow run has no readable outcome. This is
    that outcome: what the account WOULD have done. It reads the decision
    journal and nothing else, so it works for any strategy kind that journals
    a would-be trade — it is deliberately not the earnings-specific deck it
    replaced.
    """
    if equity <= 0:
        raise HTTPException(422, "equity must be positive")
    runner = request.app.state.strategy_runner
    try:
        await runner.get(row_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    decisions = await runner.decisions(row_id, TWIN_MAX_DECISIONS)
    signals = signals_from_decisions(decisions)
    market = getattr(request.app.state, "market", None)

    def bars_for(symbol: str) -> list[dict]:
        return market.bars.get_bars(symbol, "1m") if market else []

    def mark_for(symbol: str) -> float | None:
        quote = market.latest_quote(symbol) if market else None
        return quote.get("mid") if quote else None

    return {
        "signals": len(signals),
        "params": {"equity": equity, "risk_pct": risk_pct, "flat_pct": flat_pct},
        "policies": [
            simulate(signals, bars_for, mark_for, policy=policy,
                     start_equity=equity, risk_pct=risk_pct, flat_pct=flat_pct)
            for policy in ("vol", "flat")
        ],
    }


@router.get("/fund")
async def fund(request: Request) -> dict:
    """The fund root: every instance's allocation envelope and open
    positions, plus the account-level exposure rollup (directional for
    shares, margin-at-risk for options, betas and pairwise correlations for
    the held names) that no single instance's view aggregates."""
    return await request.app.state.strategy_runner.fund_state()


@router.get("/signals")
async def signals(
    request: Request, limit: int = 100, type: str | None = None,
    symbol: str | None = None,
) -> dict:
    return {"signals": await request.app.state.signal_store.recent(limit, type, symbol)}
