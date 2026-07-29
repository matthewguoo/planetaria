"""The fair-value estimator the SL/TP triggers act on: microprice math,
spread-gated quote trust, and theo-drift prediction."""

import pytest

from app.services.fair_value import (
    FairValueFilter,
    leg_half_spread,
    leg_microprice,
    position_quote_stats,
)

LEG = [{"symbol": "X", "side": 1, "ratio": 1}]


def test_microprice_leans_toward_book_pressure():
    # Heavy bid = buying pressure: fair value sits near the ask.
    q = {"bid": 1.00, "ask": 1.10, "mid": 1.05, "bid_size": 90, "ask_size": 10}
    assert leg_microprice(q) == pytest.approx(1.09)
    # Heavy ask = selling pressure: fair value sits near the bid.
    q = {"bid": 1.00, "ask": 1.10, "mid": 1.05, "bid_size": 10, "ask_size": 90}
    assert leg_microprice(q) == pytest.approx(1.01)


def test_microprice_falls_back_to_mid_without_sizes():
    q = {"bid": 1.00, "ask": 1.10, "mid": 1.05}
    assert leg_microprice(q) == pytest.approx(1.05)


def test_half_spread_penalizes_one_sided_and_crossed_quotes():
    assert leg_half_spread({"bid": 1.00, "ask": 1.10, "mid": 1.05}) == pytest.approx(0.05)
    # One-sided: barely informative, wide effective spread.
    assert leg_half_spread({"bid": 0, "ask": 1.10, "mid": 1.10}) == pytest.approx(0.275)
    # Crossed book disagrees with itself: treated as suspect.
    assert leg_half_spread({"bid": 1.20, "ask": 1.10, "mid": 1.15}) == pytest.approx(0.115)


def test_position_stats_require_every_leg():
    stats = position_quote_stats(
        [{"symbol": "A", "side": 1, "ratio": 1}, {"symbol": "B", "side": -1, "ratio": 1}],
        {"A": {"bid": 2.0, "ask": 2.1, "mid": 2.05}},
    )
    assert stats is None


def test_position_stats_aggregate_value_and_spread():
    value, hs = position_quote_stats(
        [{"symbol": "A", "side": 1, "ratio": 1}, {"symbol": "B", "side": -1, "ratio": 1}],
        {
            "A": {"bid": 2.0, "ask": 2.2, "mid": 2.1},
            "B": {"bid": 0.9, "ask": 1.1, "mid": 1.0},
        },
    )
    assert value == pytest.approx(1.1)
    assert hs == pytest.approx(0.2)  # both legs' uncertainty adds


Q_RATE = (0.05 * 3.0) ** 2  # bracket span 3.0, as the monitor computes it


def test_wide_junk_print_barely_moves_the_filter():
    f = FairValueFilter()
    f.on_quote(2.0, 0.01, now=0.0, q_rate=Q_RATE)
    # One junk observation: mid crashed to 0.8 but the market is 0.6 wide.
    fv = f.on_quote(0.8, 0.6, now=1.0, q_rate=Q_RATE)
    assert fv > 1.85, f"junk print moved fair value to {fv}"


def test_tight_real_move_tracks_almost_immediately():
    f = FairValueFilter()
    f.on_quote(2.0, 0.01, now=0.0, q_rate=Q_RATE)
    fv = f.on_quote(0.8, 0.01, now=1.0, q_rate=Q_RATE)
    assert fv < 0.9, f"tight quote not trusted: {fv}"


def test_persistent_wide_quotes_eventually_win():
    # Information beats suspicion: a genuinely wide market sitting at a new
    # level converges the estimate there, just slowly.
    f = FairValueFilter()
    f.on_quote(2.0, 0.01, now=0.0, q_rate=Q_RATE)
    fv = 2.0
    for i in range(1, 121):
        fv = f.on_quote(0.8, 0.6, now=float(i), q_rate=Q_RATE)
    assert fv < 1.0, f"filter never converged: {fv}"


def test_model_drift_moves_value_between_quotes():
    f = FairValueFilter()
    f.on_quote(2.0, 0.01, now=0.0, q_rate=Q_RATE)
    f.on_model_value(1.9)   # first model mark: reference only
    f.on_model_value(1.4)   # model says the position lost 0.50
    assert f.value == pytest.approx(1.5)


def test_model_level_bias_cancels():
    # The model's LEVEL is 0.30 rich vs the market; only its CHANGES apply.
    f = FairValueFilter()
    f.on_quote(2.0, 0.01, now=0.0, q_rate=Q_RATE)
    f.on_model_value(2.3)
    f.on_model_value(2.3)
    assert f.value == pytest.approx(2.0)
