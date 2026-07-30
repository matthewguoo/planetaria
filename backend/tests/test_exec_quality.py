"""Execution-quality ledger: every fill is measured against the net fair
value snapshotted at SUBMIT time. The retail-options literature (Beckmeyer
et al. 2023; Bryzgalova et al. 2023) attributes the majority of retail
losses to spread friction, not direction — this ledger makes ours visible
per trade, using the effective-vs-quoted-spread framing of Muravyev &
Pearson (RFS 2020).

Sign conventions under signed premiums (+debit / -credit):
  entry cost = fill - fair   (paying a higher net premium is worse)
  exit  cost = fair - fill   (receiving less is worse)
  spread_capture = 1 - cost/half_spread (1 = at fair, 0 = crossed the touch)
"""

import pytest

from app.services.trade_service import quality_fill


def _snap(fair: float, hs: float) -> dict:
    return {"fair": fair, "half_spread": hs, "ts": "2026-07-29T16:00:00+00:00"}


def test_entry_debit_paying_up_is_a_cost():
    out = quality_fill(_snap(2.30, 0.06), "entry", 2.33)
    assert out["cost"] == pytest.approx(0.03)
    assert out["spread_capture"] == pytest.approx(0.5)


def test_entry_at_fair_captures_full_spread():
    out = quality_fill(_snap(2.30, 0.06), "entry", 2.30)
    assert out["cost"] == pytest.approx(0.0)
    assert out["spread_capture"] == pytest.approx(1.0)


def test_entry_credit_structure_worse_means_less_credit():
    # Credit entry: fair -1.20; filling at -1.15 collected LESS credit.
    out = quality_fill(_snap(-1.20, 0.05), "entry", -1.15)
    assert out["cost"] == pytest.approx(0.05)
    assert out["spread_capture"] == pytest.approx(0.0)


def test_exit_receiving_less_than_fair_is_a_cost():
    out = quality_fill(_snap(3.00, 0.08), "exit", 2.90)
    assert out["cost"] == pytest.approx(0.10)
    assert out["spread_capture"] == pytest.approx(-0.25)  # worse than the touch


def test_exit_price_improvement_scores_above_one():
    out = quality_fill(_snap(3.00, 0.08), "exit", 3.02)
    assert out["cost"] == pytest.approx(-0.02)
    assert out["spread_capture"] == pytest.approx(1.25)


def test_zero_half_spread_omits_capture():
    out = quality_fill(_snap(2.30, 0.0), "entry", 2.31)
    assert out["cost"] == pytest.approx(0.01)
    assert "spread_capture" not in out
