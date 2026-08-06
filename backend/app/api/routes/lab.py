"""LAB — the observation deck for the LLM-gated strategy.

One aggregated, cursor-paged read of everything a live paper test produces:
the LLM calls (signals of type `analysis`), the decisions those calls led to
(strategy_decisions), the orders that resulted (trade_plans), and the paper
twin's two allocator curves (services/sim_account).

Read-only and derived by construction — this module writes nothing and owns
no state, so the deck can never become a second version of the truth. It is
mounted with the engine API (not behind HEADLESS) because it is how a live
night is watched, and a headless engine is exactly the one you cannot see
into otherwise.
"""

from fastapi import APIRouter, HTTPException, Request

from app.models.strategies import StrategyDecisionRow, StrategyInstanceRow
from app.models.trade import TradePlan
from app.services.sim_account import (
    DEFAULT_EQUITY,
    DEFAULT_FLAT_PCT,
    signals_from_decisions,
    simulate,
)

router = APIRouter(prefix="/api/lab", tags=["lab"])

LAB_KINDS = ("earnings_reaction",)
MAX_LIMIT = 500


async def _instances(request: Request) -> list[dict]:
    from sqlalchemy import select

    db = request.app.state.db
    async with db.session() as session:
        rows = (await session.scalars(
            select(StrategyInstanceRow).order_by(StrategyInstanceRow.created_at)
        )).all()
    runner = getattr(request.app.state, "strategy_runner", None)
    out = []
    for row in rows:
        if row.kind not in LAB_KINDS:
            continue
        data = row.to_dict()
        if runner is not None:
            data["runtime"] = runner._runtime_status(runner._running.get(row.id))
        out.append(data)
    return out


async def _decisions(request: Request, ids: list[str], since: int,
                     limit: int) -> list[dict]:
    from sqlalchemy import select

    if not ids:
        return []
    async with request.app.state.db.session() as session:
        rows = (await session.scalars(
            select(StrategyDecisionRow)
            .where(StrategyDecisionRow.strategy_id.in_(ids),
                   StrategyDecisionRow.id > since)
            .order_by(StrategyDecisionRow.id.desc())
            .limit(limit)
        )).all()
    return [r.to_dict() for r in rows]


@router.get("/stream")
async def stream(request: Request, since_decision: int = 0,
                 since_signal: int = 0, limit: int = 200) -> dict:
    """Incremental feed. Pass back the returned cursor to get only what is
    new; pass 0/0 for a cold snapshot. Newest-first within each list."""
    from sqlalchemy import select

    limit = max(1, min(limit, MAX_LIMIT))
    instances = await _instances(request)
    ids = [i["id"] for i in instances]
    names = [i["name"] for i in instances]
    decisions = await _decisions(request, ids, since_decision, limit)

    store = request.app.state.signal_store
    analyses = [s for s in await store.recent(limit, "analysis")
                if (s.get("id") or 0) > since_signal]

    async with request.app.state.db.session() as session:
        plans = (await session.scalars(
            select(TradePlan)
            .where(TradePlan.strategy.in_(names or [""]))
            .order_by(TradePlan.created_at.desc())
            .limit(100)
        )).all()

    market = getattr(request.app.state, "market", None)
    marks: dict[str, float] = {}
    if market is not None:
        for symbol in {p.underlying for p in plans} | {
                s for d in decisions
                for s in [((d.get("detail") or {}).get("would_trade")
                           or (d.get("detail") or {}).get("decision")
                           or {}).get("symbol")] if s}:
            quote = market.latest_quote(symbol)
            if quote and quote.get("mid"):
                marks[symbol] = quote["mid"]

    return {
        "instances": instances,
        "decisions": decisions,
        "analyses": analyses,
        "plans": [p.to_dict() for p in plans],
        "marks": marks,
        "llm": {
            "backend": request.app.state.settings.llm_backend,
            "model": request.app.state.settings.llm_model,
            "configured": request.app.state.strategy_runner.llm.configured,
        },
        "feed": {"stock": request.app.state.settings.alpaca_stock_feed},
        "cursor": {
            "decision": max([d["id"] for d in decisions] + [since_decision]),
            "signal": max([a["id"] for a in analyses] + [since_signal]),
        },
    }


@router.get("/sim")
async def sim(request: Request, strategy_id: str | None = None,
              equity: float = DEFAULT_EQUITY, risk_pct: float = 0.5,
              flat_pct: float = DEFAULT_FLAT_PCT) -> dict:
    """The paper twin: the same decision stream compounded under the live
    vol-weighted allocator and a flat-notional allocator."""
    if equity <= 0:
        raise HTTPException(422, "equity must be positive")
    instances = await _instances(request)
    if strategy_id:
        instances = [i for i in instances if i["id"] == strategy_id]
        if not instances:
            raise HTTPException(404, f"no lab instance {strategy_id!r}")
    decisions = await _decisions(request, [i["id"] for i in instances], 0, MAX_LIMIT)
    signals = signals_from_decisions(decisions)

    market = getattr(request.app.state, "market", None)

    def bars_for(symbol: str) -> list[dict]:
        return market.bars.get_bars(symbol, "1m") if market else []

    def mark_for(symbol: str) -> float | None:
        quote = market.latest_quote(symbol) if market else None
        return quote.get("mid") if quote else None

    return {
        "instances": [i["name"] for i in instances],
        "signals": len(signals),
        "params": {"equity": equity, "risk_pct": risk_pct, "flat_pct": flat_pct},
        "policies": [
            simulate(signals, bars_for, mark_for, policy=policy,
                     start_equity=equity, risk_pct=risk_pct, flat_pct=flat_pct)
            for policy in ("vol", "flat")
        ],
    }
