"""The MANUAL BOOKS: per-asset-class capital envelopes for discretionary
trades (plans with no strategy_id), layered under the global gates. The
share swing book ($11k) and the options book ($5k) are separate bankrolls
with separate breakers, mirroring the real accounts they rehearse for."""

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


def _opt_base(**overrides):
    merged = dict(
        legs=OPT_LEGS, underlying="SPY", asset_class="option",
        expiry_date_et=(datetime.now(ET) + timedelta(days=30)).strftime("%Y-%m-%d"),
        entry_cost_dollars=500.0, max_loss_dollars=100.0,
    )
    merged.update(overrides)
    return _base(**merged)


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


async def _seed_manual_option(risk, *, entry=2.0, qty=5, status="filled",
                              realized=None, asset_class="option"):
    plan = TradePlan(
        id=uuid.uuid4().hex,
        underlying="SPY",
        strategy="long_call",
        strategy_id=None,
        asset_class=asset_class,  # None simulates pre-migration rows
        legs=OPT_LEGS,
        qty=qty,
        entry_limit=entry,
        tp_premium=entry * 2,
        sl_premium=entry * 0.5,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=6),
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
    mb = (await risk.get_settings())["manual_book"]
    assert mb["enabled"] is True
    assert mb["equity"]["equity_usd"] == 11_000.0
    assert mb["option"]["equity_usd"] == 5_000.0
    # Partial patch of one class keeps the other class AND the sibling keys.
    await risk.update_settings({"manual_book": {"equity": {"equity_usd": 9_000.0}}})
    mb = (await risk.get_settings())["manual_book"]
    assert mb["equity"]["equity_usd"] == 9_000.0
    assert mb["equity"]["max_open_plans"] == 4
    assert mb["option"]["equity_usd"] == 5_000.0
    # A second patch to the OTHER class keeps the first patch's value.
    await risk.update_settings({"manual_book": {"option": {"equity_usd": 6_000.0}}})
    mb = (await risk.get_settings())["manual_book"]
    assert mb["equity"]["equity_usd"] == 9_000.0
    assert mb["option"]["equity_usd"] == 6_000.0


@pytest.mark.asyncio
async def test_settings_bounds(risk):
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"equity": {"equity_usd": 100.0}}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"option": {"max_open_plans": 0}}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"nonsense": 1}})
    with pytest.raises(ValueError):
        await risk.update_settings({"manual_book": {"equity": {"nonsense": 1}}})
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
    await risk.update_settings({"manual_book": {"require_stop_equity": False}})
    v = await risk.validate_new_trade(**_base(max_loss_dollars=None))
    assert not any("require" in s and "stop" in s for s in v)


@pytest.mark.asyncio
async def test_per_trade_max_loss_binds_at_book_not_account(risk):
    # $300 loss: fine vs the $2,000 global cap (2% of $100k), refused vs
    # the equity book's $220 (2% of $11k).
    v = await risk.validate_new_trade(**_base(max_loss_dollars=300.0))
    assert any("manual equity book" in s and "max loss" in s for s in v)
    assert not any(s.startswith("max loss") for s in v)


@pytest.mark.asyncio
async def test_option_book_binds_separately(risk):
    # $300 loss on OPTIONS: option book cap is 5% of $5k = $250 -> refused.
    v = await risk.validate_new_trade(**_opt_base(max_loss_dollars=300.0))
    assert any("manual option book" in s and "max loss" in s for s in v)
    # $200 passes the option book.
    v = await risk.validate_new_trade(**_opt_base(max_loss_dollars=200.0))
    assert not any("manual option book" in s for s in v)


@pytest.mark.asyncio
async def test_envelope_counts_open_manual_capital(risk):
    # $8k already deployed (100 sh x $80): a $4k entry busts the $11k book.
    await _seed_manual_equity(risk, entry=80.0, qty=100)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=4_000.0))
    assert any("envelope" in s for s in v)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=2_000.0))
    assert not any("envelope" in s for s in v)


@pytest.mark.asyncio
async def test_books_do_not_share_dollars(risk):
    # $8k of open EQUITY does not consume the OPTIONS envelope...
    await _seed_manual_equity(risk, entry=80.0, qty=100)
    v = await risk.validate_new_trade(**_opt_base(entry_cost_dollars=4_000.0))
    assert not any("envelope" in s for s in v)
    # ...and $4k of open manual OPTIONS (20 x $2 x 100) blocks a $1.5k
    # options entry ($5k book) but not a $2k equity entry ($11k book).
    await _seed_manual_option(risk, entry=2.0, qty=20)
    v = await risk.validate_new_trade(**_opt_base(entry_cost_dollars=1_500.0))
    assert any("manual option book" in s and "envelope" in s for s in v)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=2_000.0))
    assert not any("envelope" in s for s in v)


@pytest.mark.asyncio
async def test_null_asset_class_counts_as_option(risk):
    # Pre-migration manual rows (asset_class NULL) belong to the option book.
    await _seed_manual_option(risk, entry=2.0, qty=20, asset_class=None)  # $4k
    v = await risk.validate_new_trade(**_opt_base(entry_cost_dollars=1_500.0))
    assert any("manual option book" in s and "envelope" in s for s in v)


@pytest.mark.asyncio
async def test_short_capital_charged_at_reg_t(risk):
    # 80 sh x $80 short = $6.4k notional -> $9.6k at 150%; +$2k busts $11k.
    await _seed_manual_equity(risk, entry=80.0, qty=80, side=-1)
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=2_000.0))
    assert any("envelope" in s for s in v)


@pytest.mark.asyncio
async def test_strategy_plans_exempt_and_not_counted(risk):
    await _seed_manual_equity(risk, entry=80.0, qty=130, strategy_id="strat-1")
    v = await risk.validate_new_trade(**_base(entry_cost_dollars=4_000.0))
    assert not any("book" in s for s in v)
    v = await risk.validate_new_trade(
        **_base(entry_cost_dollars=10_500.0, max_loss_dollars=1_500.0,
                strategy_id="strat-1")
    )
    assert not any("book" in s for s in v)


@pytest.mark.asyncio
async def test_max_open_manual_plans_per_class(risk):
    await risk.update_settings({"manual_book": {"equity": {"max_open_plans": 2}},
                                "max_positions": 20})
    await _seed_manual_equity(risk, qty=1)
    await _seed_manual_equity(risk, qty=1)
    v = await risk.validate_new_trade(**_base())
    assert any("open manual plans" in s for s in v)
    # The OPTIONS book is not full.
    v = await risk.validate_new_trade(**_opt_base())
    assert not any("open manual plans" in s for s in v)


@pytest.mark.asyncio
async def test_manual_daily_loss_breaker_is_per_class(risk):
    # -$400 realized on the EQUITY book trips its $330 breaker; the OPTIONS
    # book (and the account-wide breaker) are untouched.
    await _seed_manual_equity(risk, status="closed", realized=-400.0, qty=1)
    v = await risk.validate_new_trade(**_base())
    assert any("manual equity book" in s and "daily loss" in s for s in v)
    assert not any(s.startswith("daily circuit breaker") for s in v)
    v = await risk.validate_new_trade(**_opt_base())
    assert not any("daily loss" in s for s in v)


@pytest.mark.asyncio
async def test_disabled_book_gates_nothing(risk):
    await risk.update_settings({"manual_book": {"enabled": False}})
    v = await risk.validate_new_trade(
        **_base(max_loss_dollars=1_500.0, entry_cost_dollars=4_500.0)
    )
    assert not any("book" in s for s in v)


# ------------------------------------------------------------- state

@pytest.mark.asyncio
async def test_manual_book_state_math(risk):
    await _seed_manual_equity(risk, entry=80.0, qty=25)               # $2,000 equity
    await _seed_manual_equity(risk, entry=50.0, qty=20, side=-1)      # $1,500 at 1.5x
    await _seed_manual_equity(risk, status="closed", realized=-120.0, qty=1)
    await _seed_manual_option(risk, entry=2.0, qty=5)                 # $1,000 option
    await _seed_manual_equity(risk, entry=2.0, qty=1, strategy_id="s")  # not manual
    state = await risk.manual_book_state()
    eq, op = state["equity"], state["option"]
    assert eq["open_plans"] == 2
    assert eq["used_usd"] == pytest.approx(3_500.0)
    assert eq["remaining_usd"] == pytest.approx(11_000.0 - 3_500.0)
    assert eq["realized_today"] == pytest.approx(-120.0)
    assert eq["per_trade_max_loss_usd"] == pytest.approx(220.0)
    assert op["open_plans"] == 1
    assert op["used_usd"] == pytest.approx(1_000.0)
    assert op["remaining_usd"] == pytest.approx(4_000.0)
    assert op["per_trade_max_loss_usd"] == pytest.approx(250.0)
