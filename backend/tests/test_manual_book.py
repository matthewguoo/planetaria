"""The MANUAL BOOK: an $11k capital envelope for discretionary trades
(plans with no strategy_id), layered under the global gates. Mirrors the
real account this book rehearses for, so every gate binds at realistic
dollars instead of the paper account's $100k."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.session import Database
from app.models.trade import TradePlan
from app.services.risk import RiskService

ET = ZoneInfo("America/New_York")

EQ_LEGS = [{"symbol": "TQQQ", "side": 1, "ratio": 1, "entry": 80.0, "half_spread": 0.01}]
OPT_LEGS = [
    {"symbol": "SPY260931C00450000", "right": "C", "strike": 450.0, "expiry": "2026-09-31",
     "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2, "half_spread": 0.05},
]


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/mb.db")
    yield RiskService(db)
    await db.close()


def _base(**overrides):
    stop = (datetime.now(ET) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    base = dict(
        account_equity=100_000.0,
        entry_cost_dollars=2_000.0,
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


async def _seed_manual_equity(risk, *, entry=80.0, qty=25, side=1, status="filled",
                              strategy_id=None, realized=None, symbol=None):
    plan = TradePlan(
        id=uuid.uuid4().hex,
        underlying=symbol or f"EQ{uuid.uuid4().hex[:4].upper()}",
        strategy="manual_equity",
        strategy_id=strategy_id,
        asset_class="equity",
        legs=[{"symbol": symbol or "TQQQ", "side": side, "ratio": 1, "entry": side * entry}],
        qty=qty,
        entry_limit=side * entry,
        tp_premium=None,
        sl_premium=side * entry * 0.95,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(days=5),
        status=status,
        realized_pnl=realized,
    )
    async with risk.db.session() as session:
        session.add(plan)
        await session.commit()
    return plan


# ------------------------------------------------------------- settings

@pytest.mark.asyncio
async def test_defaults_present_and_deep_merged(risk):
    cfg = await risk.get_settings()
    mb = cfg["manual_book"]
    assert mb["enabled"] is True
    assert mb["equity_usd"] == 11_000.0
    # Partial patch keeps the other subkeys (deep merge, not clobber).
    await risk.update_settings({"manual_book": {"equity_usd": 9_000.0}})
    mb = (await risk.get_settings())["manual_book"]
    assert mb["equity_usd"] == 9_000.0
    assert mb["max_open_plans"] == 4
    # And a second partial patch keeps the first one's value.
    await risk.update_settings({"manual_book": {"max_open_plans": 2}})
    mb = (await risk.get_settings())["manual_book"]
    assert mb["equity_usd"] == 9_000.0
    assert mb["max_open_plans"] == 2


@pytest.mark.asyncio
async def test_settings_bounds(risk):
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"equity_usd": 500.0}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"max_open_plans": 0}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"nonsense": 1}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": "not-a-dict"})


# ------------------------------------------------------------- gates

@pytest.mark.asyncio
async def test_clean_manual_equity_passes(risk):
    assert await risk.validate_new_trade(**_base()) == []


@pytest.mark.asyncio
async def test_equity_requires_stop(risk):
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None))
    assert any("require" in s and "stop" in s for s in v)
    # The discipline rule is a setting.
    await risk.update_settings({"manual_book": {"require_stop_equity": False}})
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None))
    assert not any("require" in s and "stop" in s for s in v)


@pytest.mark.asyncio
async def test_per_trade_max_loss_binds_at_book_not_account(risk):
    # $300 loss: fine vs the $2,000 global cap (2% of $100k), refused vs
    # the book's $220 (2% of $11k).
    v = await risk.validate_new_trade(**_base(max_loss_dollars=300.0))
    assert any("manual book" in s and "max loss" in s for s in v)
    assert not any(s.startswith("max loss") for s in v)


@pytest.mark.asyncio
async def test_envelope_counts_open_manual_capital(risk):
    # $8k already deployed (100 sh x $80): a $4k entry busts the $11k book.
    await _seed_manual_equity(risk, entry=80.0, qty=100)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=4_000.0))
    assert any("envelope" in s for s in v)
    # $2k still fits.
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=2_000.0))
    assert not any("envelope" in s for s in v)


@pytest.mark.asyncio
async def test_short_capital_charged_at_reg_t(risk):
    # 80 sh x $80 short = $6.4k notional -> $9.6k at 150%; +$2k busts $11k.
    await _seed_manual_equity(risk, entry=80.0, qty=80, side=-1)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=2_000.0))
    assert any("envelope" in s for s in v)


@pytest.mark.asyncio
async def test_strategy_plans_exempt_and_not_counted(risk):
    # A strategy's own open capital does not consume the manual envelope...
    await _seed_manual_equity(risk, entry=80.0, qty=130, strategy_id="strat-1")
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=4_000.0))
    assert not any("manual book" in s for s in v)
    # ...and a strategy trade is never gated by the book.
    v = await risk.validate_new_trade(
        **_base(entry_cost_dollars=10_500.0, max_loss_dollars=1_500.0,
                strategy_id="strat-1")
    )
    assert not any("manual book" in s for s in v)


@pytest.mark.asyncio
async def test_max_open_manual_plans(risk):
    await risk.update_settings({"manual_book": {"max_open_plans": 2},
                                "max_positions": 20})
    await _seed_manual_equity(risk, qty=1)
    await _seed_manual_equity(risk, qty=1)
    v = await risk.validate_new_trade(**_base())
    assert any("open manual plans" in s for s in v)


@pytest.mark.asyncio
async def test_manual_daily_loss_breaker_is_manual_only(risk):
    # -$400 realized on the MANUAL book today trips its $330 breaker even
    # though the account-wide 6%-of-$100k breaker is nowhere near.
    await _seed_manual_equity(risk, status="closed", realized=-400.0, qty=1)
    v = await risk.validate_new_trade(**_base())
    assert any("manual book" in s and "daily loss" in s for s in v)
    assert not any(s.startswith("daily circuit breaker") for s in v)


@pytest.mark.asyncio
async def test_disabled_book_gates_nothing(risk):
    await risk.update_settings({"manual_book": {"enabled": False}})
    v = await risk.validate_new_trade(
        **_base(max_loss_dollars=1_500.0, entry_cost_dollars=4_500.0)
    )
    assert not any("manual book" in s for s in v)


# ------------------------------------------------------------- state

@pytest.mark.asyncio
async def test_manual_book_state_math(risk):
    await _seed_manual_equity(risk, entry=80.0, qty=25)               # $2,000
    await _seed_manual_equity(risk, entry=50.0, qty=20, side=-1)      # $1,500 at 1.5x
    await _seed_manual_equity(risk, status="closed", realized=-120.0, qty=1)
    await _seed_manual_equity(risk, entry=2.0, qty=1, strategy_id="s")  # not manual
    state = await risk.manual_book_state()
    assert state["open_plans"] == 2
    assert state["used_usd"] == pytest.approx(2_000.0 + 1_500.0)
    assert state["remaining_usd"] == pytest.approx(11_000.0 - 3_500.0)
    assert state["realized_today"] == pytest.approx(-120.0)
    assert state["per_trade_max_loss_usd"] == pytest.approx(220.0)


@pytest.mark.asyncio
async def test_options_count_against_the_envelope_too(risk):
    # A manual OPTION plan (contract multiplier 100) consumes book capital.
    plan = TradePlan(
        id=uuid.uuid4().hex,
        underlying="SPY",
        strategy="long_call",
        strategy_id=None,
        asset_class="option",
        legs=OPT_LEGS,
        qty=5,
        entry_limit=2.0,
        tp_premium=4.0,
        sl_premium=1.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=6),
        status="filled",
    )
    async with risk.db.session() as session:
        session.add(plan)
        await session.commit()
    state = await risk.manual_book_state()
    assert state["used_usd"] == pytest.approx(2.0 * 100 * 5)
