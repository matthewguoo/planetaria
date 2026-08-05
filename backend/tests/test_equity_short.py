"""Equity SHORT readiness: the capability exists, the gates own the risk.

Shorts ride the signed position-value convention the options engine already
manages daily: a short share entry is a negative entry_limit (credit), the
SL sits BELOW it on the value axis (price above entry), and pnl_at stays the
one P/L formula. What these tests pin:

  - the four gates: equity_long_only master toggle, shortable/ETB broker
    check (None = refuse), the overnight-session short-entry block, and
    Reg-T-aware sizing (shorts claim 1.5x notional of the caps);
  - order construction: SELL entry, BUY cover, extended_hours preserved;
  - signed math: bracket ordering and P/L for a short round trip.

Broker facts encoded here get verified live by scripts/verify_short_paths.py.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services.risk import RiskService
from tests.test_equity_paths import equity_plan, make_trade_service

pytestmark = pytest.mark.asyncio


def short_plan(**overrides):
    """Short 2 shares at 700: entry credit -700/share, TP buys back at 696
    (value -696 > -700), SL covers at 704 (value -704 < -700)."""
    base = dict(
        id="shplan1",
        legs=[{"symbol": "SPY", "side": -1, "ratio": 1, "entry": -700.0}],
        entry_limit=-700.0, tp_premium=-696.0, sl_premium=-704.0,
        fill_premium=-700.0,
    )
    base.update(overrides)
    return equity_plan(**base)


# ------------------------------------------------------------- signed math


class TestShortMath:
    async def test_bracket_ordering_holds_for_credits(self):
        plan = short_plan()
        assert plan.sl_premium < plan.entry_limit < plan.tp_premium

    async def test_pnl_of_a_short_round_trip(self):
        plan = short_plan()
        # Cover cheaper at 696 -> value -696: (-696 - -700) * 1 * 2 = +8
        assert plan.pnl_at(-696.0) == pytest.approx(8.0)
        # Stopped at 704 -> value -704: loss of 8
        assert plan.pnl_at(-704.0) == pytest.approx(-8.0)


# ------------------------------------------------------- order construction


class TestShortOrders:
    async def test_short_entry_is_sell_limit_extended_hours(self):
        trade, alpaca = make_trade_service()
        await trade._submit_entry(short_plan())
        request = alpaca.submitted[0]
        assert str(request.side).lower().endswith("sell")
        assert request.position_intent is None
        assert request.limit_price == pytest.approx(700.0)  # unsigned on the wire
        assert request.extended_hours is True

    async def test_short_close_is_buy_to_cover(self):
        trade, _ = make_trade_service()
        request = trade._close_request(short_plan(), -696.0, "cid")
        assert str(request.side).lower().endswith("buy")
        assert request.limit_price == pytest.approx(696.0)
        assert request.extended_hours is True


# ------------------------------------------------------------------- gates


@pytest_asyncio.fixture
async def risk(tmp_path):
    db = Database()
    await db.connect(f"sqlite+aiosqlite:///{tmp_path}/risk.db")
    yield RiskService(db)
    await db.close()


SHORT_LEGS = [{"symbol": "SPY", "side": -1, "ratio": 1, "entry": -700.0}]


def validate(risk, **overrides):
    base = dict(
        account_equity=100_000.0,
        entry_cost_dollars=2_100.0,  # 2sh x 700 x 1.5
        max_loss_dollars=8.0,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=6),
        expiry_date_et="",
        legs=SHORT_LEGS,
        underlying="SPY",
        stream_age_s=1.0,
        asset_class="equity",
        shortable=True,
        overnight_session=False,
    )
    base.update(overrides)
    return risk.validate_new_trade(**base)


class TestShortGates:
    async def test_long_only_default_blocks_shorts(self, risk):
        violations = await validate(risk)
        assert any("equity_long_only" in v for v in violations)

    async def test_flipping_the_toggle_allows_verified_shorts(self, risk):
        await risk.update_settings({"equity_long_only": False})
        assert await validate(risk) == []

    async def test_unverified_borrow_refuses(self, risk):
        await risk.update_settings({"equity_long_only": False})
        for value in (False, None):
            violations = await validate(risk, shortable=value)
            assert any("shortable" in v for v in violations), value

    async def test_overnight_short_entry_blocked_by_default(self, risk):
        await risk.update_settings({"equity_long_only": False})
        violations = await validate(risk, overnight_session=True)
        assert any("overnight" in v for v in violations)
        await risk.update_settings({"equity_short_overnight": True})
        assert await validate(risk, overnight_session=True) == []

    async def test_longs_never_touch_the_short_gates(self, risk):
        violations = await validate(
            risk,
            legs=[{"symbol": "SPY", "side": 1, "ratio": 1, "entry": 700.0}],
            entry_cost_dollars=1_400.0,
            shortable=None,
            overnight_session=True,
        )
        assert violations == []


# ------------------------------------------------------- shortable lookup


class TestShortableLookup:
    async def test_shortable_requires_both_flags_and_caches(self):
        trade, alpaca = make_trade_service()
        calls = []

        def get_asset(symbol):
            calls.append(symbol)
            return SimpleNamespace(shortable=True, easy_to_borrow=True)

        alpaca.trading.get_asset = get_asset
        assert await trade._asset_shortable("spy") is True
        assert await trade._asset_shortable("SPY") is True  # cached
        assert calls == ["SPY"]

    async def test_htb_is_not_shortable_for_us(self):
        trade, alpaca = make_trade_service()
        alpaca.trading.get_asset = lambda s: SimpleNamespace(
            shortable=True, easy_to_borrow=False)
        assert await trade._asset_shortable("HTB") is False

    async def test_lookup_failure_returns_none(self):
        trade, alpaca = make_trade_service()

        def boom(symbol):
            raise RuntimeError("api down")

        alpaca.trading.get_asset = boom
        assert await trade._asset_shortable("SPY") is None
