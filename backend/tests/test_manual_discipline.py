"""The discretionary discipline rule: MANUAL equity entries (no strategy_id)
require a stop loss. Capital separation is done with REAL accounts (one per
book), so the %-of-equity gates bind at the right dollars by construction —
the virtual per-class books were retired 2026-09-01."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import AppSetting
from app.services.risk import RISK_KEY, RiskService

ET = ZoneInfo("America/New_York")

EQ_LEGS = [{"symbol": "TQQQ", "side": 1, "ratio": 1, "entry": 80.0, "half_spread": 0.01}]


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/md.db")
    yield RiskService(db)
    await db.close()


def _base(**overrides):
    stop = (datetime.now(ET) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    base = dict(
        account_equity=11_000.0,  # a real account sized for the book
        entry_cost_dollars=500.0,
        max_loss_dollars=100.0,
        time_stop_utc=stop,
        expiry_date_et="",
        legs=EQ_LEGS,
        underlying="TQQQ",
        stream_age_s=1.0,
        daytrade_count=0,
        asset_class="equity",
        strategy_id=None,
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_manual_equity_requires_stop(risk):
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None))
    assert any("require a stop loss" in s for s in v)
    # With a stop: clean.
    assert await risk.validate_new_trade(**_base()) == []


@pytest.mark.asyncio
async def test_strategy_equity_may_be_stopless(risk):
    # Strategies are bounded by allocation/breaker instead (the PEAD result).
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None, strategy_id="s1"))
    assert not any("require a stop loss" in s for s in v)


@pytest.mark.asyncio
async def test_rule_is_a_setting(risk):
    await risk.update_settings({"manual_equity_require_stop": False})
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None))
    assert not any("require a stop loss" in s for s in v)


@pytest.mark.asyncio
async def test_gates_bind_at_the_real_account(risk):
    # On an $11k account the global 2% cap IS $220 — no virtual book needed.
    v = await risk.validate_new_trade(**_base(max_loss_dollars=300.0))
    assert any(s.startswith("max loss") for s in v)


@pytest.mark.asyncio
async def test_retired_manual_book_key_refused_and_scrubbed(risk):
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"enabled": True}})
    # A stored row from the retired era is ignored on read and dropped on
    # the next write.
    async with risk.db.session() as session:
        session.add(AppSetting(key=RISK_KEY, value={"manual_book": {"enabled": True},
                                                    "max_positions": 5}))
        await session.commit()
    cfg = await risk.get_settings()
    assert "manual_book" not in cfg
    assert cfg["max_positions"] == 5
    await risk.update_settings({"max_positions": 6})
    async with risk.db.session() as session:
        row = await session.get(AppSetting, RISK_KEY)
        assert "manual_book" not in row.value
