"""Portfolio risk math: returns/beta/correlation, correlated risk
aggregation, per-plan stop risk, and BS rho sanity."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.models.trade import TradePlan
from app.services.options_math import bs_greeks
from app.services.portfolio_risk import (
    beta,
    combine_correlated,
    corr,
    diffs,
    log_returns,
    plan_stop_risk,
)


def _walk(seed: int, n: int = 70, drift: float = 0.0) -> list[float]:
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(drift, 0.01))
        out.append(price)
    return out


class TestSeriesMath:
    def test_log_returns_and_diffs(self):
        assert log_returns([100, 110]) == [pytest.approx(math.log(1.1))]
        assert diffs([4.0, 4.25, 4.1]) == [pytest.approx(0.25), pytest.approx(-0.15)]

    def test_corr_of_identical_series_is_one(self):
        r = log_returns(_walk(1))
        assert corr(r, r) == pytest.approx(1.0)

    def test_corr_of_inverse_series_is_minus_one(self):
        r = log_returns(_walk(2))
        assert corr(r, [-x for x in r]) == pytest.approx(-1.0)

    def test_corr_requires_history(self):
        assert corr([0.01] * 5, [0.01] * 5) is None

    def test_beta_scales_with_leverage(self):
        market = log_returns(_walk(3))
        levered = [2.0 * x for x in market]
        assert beta(levered, market) == pytest.approx(2.0)
        assert beta(market, market) == pytest.approx(1.0)


class TestCombineCorrelated:
    def test_perfect_correlation_is_simple_sum(self):
        risks = {"SPY": 300.0, "QQQ": 400.0}
        assert combine_correlated(risks, {("QQQ", "SPY"): 1.0}) == pytest.approx(700.0)

    def test_zero_correlation_is_root_sum_square(self):
        risks = {"A": 300.0, "B": 400.0}
        assert combine_correlated(risks, {("A", "B"): 0.0}) == pytest.approx(500.0)

    def test_unknown_pairs_default_conservative(self):
        risks = {"A": 300.0, "B": 400.0}
        assert combine_correlated(risks, {}) == pytest.approx(700.0)

    def test_never_below_largest_single_risk(self):
        risks = {"A": 500.0, "B": 100.0}
        out = combine_correlated(risks, {("A", "B"): -1.0})
        assert out >= 500.0

    def test_empty(self):
        assert combine_correlated({}, {}) == 0.0


class TestPlanStopRisk:
    def _plan(self, **kw) -> TradePlan:
        base = dict(
            underlying="SPY", strategy="long_call",
            legs=[{"symbol": "SPY260731C00450000", "right": "C", "strike": 450.0,
                   "expiry": "2026-07-31", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.2}],
            qty=2, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
            status="filled",
        )
        base.update(kw)
        return TradePlan(**base)

    def test_uses_fill_premium_when_available(self):
        plan = self._plan(fill_premium=2.5, filled_qty=2)
        # (2.5 - 1.0) * 100 * 2
        assert plan_stop_risk(plan) == pytest.approx(300.0)

    def test_falls_back_to_entry_limit_pre_fill(self):
        plan = self._plan(status="submitted")
        assert plan_stop_risk(plan) == pytest.approx(200.0)

    def test_partial_fill_uses_filled_qty(self):
        plan = self._plan(status="partially_filled", fill_premium=2.0, filled_qty=1)
        assert plan_stop_risk(plan) == pytest.approx(100.0)

    def test_credit_structure_risk(self):
        # Short at -1.00 credit, stop at -2.50 position value:
        # risk per set = (-1.0 - (-2.5)) * 100 = 150
        plan = self._plan(entry_limit=-1.0, sl_premium=-2.5, tp_premium=-0.2,
                          fill_premium=-1.0, filled_qty=2)
        assert plan_stop_risk(plan) == pytest.approx(300.0)


class TestPlanGreeks:
    def test_short_dated_otm_call_delta_is_sane(self):
        """Regression: tau must be a YEAR fraction, not raw trading hours —
        the hours bug pushed every delta to ~1.0."""
        from app.services.portfolio_risk import PortfolioRisk
        import time as _time

        # Expiry must stay in the future or the option prices as expired
        # (delta 0) and the assertions go vacuous.
        expiry_dt = datetime.now(timezone.utc) + timedelta(days=3)
        plan = TradePlan(
            underlying="SPY", strategy="long_call",
            legs=[{"symbol": f"SPY{expiry_dt:%y%m%d}C00745000", "right": "C", "strike": 745.0,
                   "expiry": f"{expiry_dt:%Y-%m-%d}", "side": 1, "ratio": 1, "entry": 2.0, "iv": 0.18}],
            qty=2, entry_limit=2.0, tp_premium=4.0, sl_premium=1.0,
            time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
            status="filled", fill_premium=2.0, filled_qty=2,
        )
        pr = PortfolioRisk(trade=None, market=None)
        spot = 740.0
        g = pr._plan_greeks(plan, spot, _time.time() * 1000)
        max_delta_dollars = 2 * 100 * spot  # delta == 1.0
        # Slightly-OTM short-dated call: delta well inside (0, 0.7).
        assert 0 < g["delta_dollars"] < 0.7 * max_delta_dollars
        assert g["theta_per_day"] < 0
        assert abs(g["rho_per_pct"]) < 100  # near-expiry rho is tiny


class TestRho:
    def test_call_rho_positive_put_rho_negative(self):
        call = bs_greeks(450.0, 450.0, 0.05, 0.25, "C")
        put = bs_greeks(450.0, 450.0, 0.05, 0.25, "P")
        assert call["rho_pct"] > 0
        assert put["rho_pct"] < 0

    def test_rho_matches_finite_difference(self):
        from app.services.options_math import bs_price

        S, K, tau, sig = 450.0, 455.0, 0.08, 0.22
        base = bs_price(S, K, tau, sig, "C", r=0.05)
        bumped = bs_price(S, K, tau, sig, "C", r=0.06)
        fd = bumped - base  # per +1% rate
        assert bs_greeks(S, K, tau, sig, "C", r=0.05)["rho_pct"] == pytest.approx(fd, rel=0.02)

    def test_theta_is_decay(self):
        g = bs_greeks(450.0, 450.0, 0.05, 0.25, "C")
        assert g["theta_day"] < 0
