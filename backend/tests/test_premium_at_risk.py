"""Premium at risk: an intrinsic-cap plan (long option, no stop) used to
report $0 at risk everywhere. Its whole debit is the maximum loss."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.trade import TradePlan
from app.services.portfolio_risk import (
    plan_premium_at_risk,
    plan_protection,
    plan_stop_risk,
    plan_unstopped_notional,
)
from app.services.trade_service import merge_holdings


def _plan(**kw) -> TradePlan:
    base = dict(
        underlying="NVDA", strategy="adopted",
        legs=[{"symbol": "NVDA260904P00230000", "right": "P", "strike": 230.0,
               "expiry": "2026-09-04", "side": 1, "ratio": 1, "entry": 2.07, "iv": 0.0}],
        qty=1, entry_limit=2.07, tp_premium=None, sl_premium=None,
        time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
        status="filled", fill_premium=2.07, filled_qty=1,
    )
    base.update(kw)
    return TradePlan(**base)


def test_intrinsic_cap_is_the_whole_premium():
    plan = _plan()
    assert plan_stop_risk(plan) == 0.0
    assert plan_premium_at_risk(plan) == pytest.approx(207.0)
    assert plan_protection(plan) == "premium"
    assert plan_protection(plan.to_dict()) == "premium"


def test_a_stop_moves_the_risk_to_the_stop_figure():
    plan = _plan(sl_premium=1.0)
    assert plan_premium_at_risk(plan) == 0.0
    assert plan_stop_risk(plan) == pytest.approx(107.0)
    assert plan_protection(plan) == "stop"


def test_short_leg_is_not_premium_capped():
    legs = [
        {"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0, "expiry": "2026-07-31",
         "side": 1, "ratio": 1, "entry": 3.0, "iv": 0.2},
        {"symbol": "SPY260731C00455000", "right": "C", "strike": 455.0, "expiry": "2026-07-31",
         "side": -1, "ratio": 1, "entry": 1.5, "iv": 0.2},
    ]
    plan = _plan(underlying="SPY", legs=legs, entry_limit=1.5, fill_premium=1.5)
    assert plan_premium_at_risk(plan) == 0.0
    assert plan_protection(plan) == "none"


def test_equity_without_stop_reports_notional_not_premium():
    plan = _plan(underlying="AVGG", asset_class="equity",
                 legs=[{"symbol": "AVGG", "side": 1, "ratio": 1, "entry": 24.73}],
                 qty=100, filled_qty=100, entry_limit=24.73, fill_premium=24.73)
    assert plan_premium_at_risk(plan) == 0.0
    assert plan_unstopped_notional(plan) == pytest.approx(2473.0)
    assert plan_protection(plan) == "none"


def test_partial_fill_uses_filled_qty():
    plan = _plan(qty=3, filled_qty=2, status="partially_filled")
    assert plan_premium_at_risk(plan) == pytest.approx(414.0)


def test_merge_holdings_carries_protection_tri_state():
    positions = [
        {"symbol": "NVDA260904P00230000", "qty": 1.0, "occ": {"underlying": "NVDA"}},
        {"symbol": "AVGG", "qty": 100.0, "occ": None},
        {"symbol": "SPCU", "qty": 110.0, "occ": None},
    ]
    plans = [
        _plan().to_dict(),
        _plan(underlying="AVGG", asset_class="equity", sl_premium=22.0,
              legs=[{"symbol": "AVGG", "side": 1, "ratio": 1, "entry": 24.73}],
              qty=100, filled_qty=100, entry_limit=24.73, fill_premium=24.73).to_dict(),
    ]
    rows = {r["symbol"]: r for r in merge_holdings(positions, plans)}
    assert rows["NVDA260904P00230000"]["protection"] == "premium"
    assert rows["NVDA260904P00230000"]["protected"] is False
    assert rows["AVGG"]["protection"] == "stop" and rows["AVGG"]["protected"] is True
    assert rows["SPCU"]["protection"] == "none" and rows["SPCU"]["plan_id"] is None


@pytest.mark.asyncio
async def test_snapshot_counts_untracked_long_option_premium():
    """A long option held outside any plan is its whole premium at risk; a
    share position is a notional nothing will exit. Both are the account,
    not the plans the engine owns."""
    from types import SimpleNamespace

    from app.services.portfolio_risk import PortfolioRisk

    class Risk:
        async def open_plans(self):
            return []

    class Trade:
        risk = Risk()

        async def get_account(self):
            return {"equity": 10_000.0}

        async def untracked_positions(self, plans=None):
            return [
                {"symbol": "NVDA260904P00230000", "qty": 1.0, "asset_class": "option", "avg_entry_price": 2.07},
                {"symbol": "AVGG", "qty": 100.0, "asset_class": "stock", "avg_entry_price": 24.73},
                {"symbol": "SPY260918C00700000", "qty": -1.0, "asset_class": "option", "avg_entry_price": 1.0},
            ]

    market = SimpleNamespace(spot=lambda s: None, latest_quote=lambda s: None)
    pr = PortfolioRisk(Trade(), market)

    async def no_closes(symbol):
        return []

    pr.daily_closes = no_closes
    snap = await pr._build()
    t = snap["total"]
    assert t["untracked_premium_dollars"] == pytest.approx(207.0)
    assert t["untracked_premium_positions"] == 1          # the short call is not premium-capped
    assert t["untracked_notional_dollars"] == pytest.approx(2473.0)
    assert t["max_loss_dollars"] == pytest.approx(207.0)
    assert t["max_loss_pct"] == pytest.approx(2.07)
