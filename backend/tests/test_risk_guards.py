"""MFT risk guards: stale data, illiquid spreads, PDT, overtrading breaker,
and the duplicate-order guard."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.risk import RiskService

GOOD_LEGS = [
    {"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0, "expiry": "2026-07-31",
     "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2, "half_spread": 0.05},
]


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/risk.db")
    yield RiskService(db)
    await db.close()


def _base(**overrides):
    # Clock-independent: stop tomorrow at 14:00 ET (before the 15:50 cutoff,
    # always in the future), expiry days later (non-expiry-day cutoff applies).
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    stop = (datetime.now(et) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    base = dict(
        account_equity=100_000.0,
        entry_cost_dollars=200.0,
        max_loss_dollars=100.0,
        time_stop_utc=stop,
        expiry_date_et=(datetime.now(et) + timedelta(days=5)).strftime("%Y-%m-%d"),
        legs=GOOD_LEGS,
        underlying="SPY",
        stream_age_s=1.0,
        daytrade_count=0,
    )
    base.update(overrides)
    return base


async def _seed_plan(risk: RiskService, created_at=None, symbols=None, status="submitted"):
    plan = TradePlan(
        id=uuid.uuid4().hex,
        underlying="SPY",
        strategy="long_call",
        legs=[{"symbol": s, "right": "C", "strike": 450.0, "expiry": "2026-07-31",
               "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}
              for s in (symbols or ["SPY260731C00450000"])],
        qty=1,
        entry_limit=2.0,
        tp_premium=4.0,
        sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=1),
        status=status,
    )
    if created_at is not None:
        plan.created_at = created_at
    async with risk.db.session() as session:
        session.add(plan)
        await session.commit()


@pytest.mark.asyncio
async def test_clean_trade_passes(risk):
    assert await risk.validate_new_trade(**_base()) == []


@pytest.mark.asyncio
async def test_stale_and_missing_market_data_block(risk):
    v = await risk.validate_new_trade(**_base(stream_age_s=120.0))
    assert any("stale" in s for s in v)
    v = await risk.validate_new_trade(**_base(stream_age_s=float("inf")))
    assert any("no ticks" in s for s in v)


@pytest.mark.asyncio
async def test_wide_spread_blocks(risk):
    wide = [dict(GOOD_LEGS[0], half_spread=0.60)]  # 30% of a 2.00 mid
    v = await risk.validate_new_trade(**_base(legs=wide))
    assert any("illiquid" in s for s in v)


@pytest.mark.asyncio
async def test_pdt_guard(risk):
    v = await risk.validate_new_trade(**_base(account_equity=10_000.0, daytrade_count=3))
    assert any("PDT" in s for s in v)
    # Big account: no PDT constraint.
    v = await risk.validate_new_trade(**_base(account_equity=100_000.0, daytrade_count=3))
    assert not any("PDT" in s for s in v)


@pytest.mark.asyncio
async def test_overtrading_breaker(risk):
    await risk.update_settings({"max_trades_per_day": 2, "max_positions": 20})
    for _ in range(2):
        await _seed_plan(risk, symbols=[f"SPY260731C0045{uuid.uuid4().hex[:4]}"], status="closed")
    v = await risk.validate_new_trade(**_base())
    assert any("overtrading" in s for s in v)


@pytest.mark.asyncio
async def test_duplicate_guard(risk):
    await _seed_plan(risk, symbols=["SPY260731C00450000"])
    v = await risk.validate_new_trade(**_base())
    assert any("duplicate" in s for s in v)
    # Different legs: not a duplicate.
    other = [dict(GOOD_LEGS[0], symbol="SPY260731C00460000")]
    v = await risk.validate_new_trade(**_base(legs=other))
    assert not any("duplicate" in s for s in v)


@pytest.mark.asyncio
async def test_new_settings_validate(risk):
    updated = await risk.update_settings(
        {"max_spread_pct": 0.2, "entry_ttl_min": 10, "max_trades_per_day": 5}
    )
    assert updated["max_spread_pct"] == 0.2
    assert updated["entry_ttl_min"] == 10
    with pytest.raises(ValueError):
        await risk.update_settings({"entry_ttl_min": 0})
    with pytest.raises(ValueError):
        await risk.update_settings({"max_spread_pct": 0.9})
