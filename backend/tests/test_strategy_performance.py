"""Per-strategy performance rollup, the paper twin, and the read-only source
endpoint."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.api.routes.strategies import catalog_source, twin
from app.db.session import Database
from app.models.trade import TradePlan
from app.services.risk import RiskService
from app.services.signals.events import EventBus
from app.services.signals.store import SignalStore
from app.services.strategy_runner import StrategyRunner


@pytest_asyncio.fixture
async def runner(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/perf.db")
    runner = StrategyRunner(
        db, EventBus(), SignalStore(db), trade=None, risk=RiskService(db),
        market=None, clock=None,
        settings=SimpleNamespace(strategies_enabled=True),
    )
    yield runner
    await runner.shutdown()
    await db.close()


def plan(strategy: str, status: str, realized: float | None) -> TradePlan:
    return TradePlan(
        underlying="AMD", strategy=strategy,
        legs=[{"symbol": "AMD", "side": 1, "ratio": 1, "entry": 500.0}],
        qty=2, entry_limit=500.0, tp_premium=550.0, sl_premium=475.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(days=1),
        status=status, realized_pnl=realized,
    )


@pytest.mark.asyncio
async def test_performance_rollup(runner):
    row = await runner.create("pead_flagship", "earn", {})
    async with runner.db.session() as session:
        session.add(plan("earn", "closed", 100.0))
        session.add(plan("earn", "closed", -40.0))
        session.add(plan("earn", "filled", None))     # still open
        session.add(plan("other", "closed", 999.0))   # different strategy
        await session.commit()

    perf = await runner.performance(row["id"])
    assert perf["name"] == "earn"
    assert perf["open"] == 1 and perf["closed"] == 2
    assert perf["win_rate"] == 0.5
    assert perf["realized_pnl"] == 60.0
    assert perf["avg_win"] == 100.0 and perf["avg_loss"] == -40.0
    assert len(perf["plans"]) == 3  # the stray "other" plan is excluded

    with pytest.raises(ValueError):
        await runner.performance("nope")


@pytest.mark.asyncio
async def test_twin_replays_any_kind_from_the_journal(runner):
    """The twin replaced a deck hardwired to one strategy kind. It reads
    journalled would-be trades and nothing else, so it works for any kind
    that journals one."""
    row = await runner.create("pead_flagship", "ref", {})
    await runner.journal_note(row["id"], {"would_trade": {
        "symbol": "AMD", "side": 1, "px": 100.0, "move_pct": 6.0,
        "verdict": {"direction": "bullish", "confidence": "medium",
                    "quality_flags": []},
        "bracket": {"sl_pct": 0.05, "tp_pct": 0.10},
        "intent": {"time_stop_utc":
                   (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()},
    }})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_runner=runner, market=None)))

    out = await twin(request, row["id"])
    assert out["signals"] == 1
    assert [p["policy"] for p in out["policies"]] == ["vol", "flat"]
    # No bars and no marks: the position stays open and marked at entry, so
    # the curve exists and is flat rather than absent.
    assert all(p["start_equity"] == out["params"]["equity"] for p in out["policies"])
    assert all(p["curve"] for p in out["policies"])

    with pytest.raises(HTTPException) as bad_id:
        await twin(request, "nope")
    assert bad_id.value.status_code == 404

    with pytest.raises(HTTPException) as bad_equity:
        await twin(request, row["id"], equity=0)
    assert bad_equity.value.status_code == 422


@pytest.mark.asyncio
async def test_catalog_source_serves_module_source():
    out = await catalog_source("pead_flagship")
    assert out["file"] == "pead_flagship.py"
    assert "class PeadFlagship" in out["source"]
    assert "SURPRISE_SCHEMA" in out["source"]

    with pytest.raises(HTTPException):
        await catalog_source("not_a_strategy")
