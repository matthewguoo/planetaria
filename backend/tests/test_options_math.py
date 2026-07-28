import math
import random

import pytest

from app.services.analytics import compute_probabilities, compute_sizing, position_iv
from app.services.options_math import (
    Leg,
    breakevens,
    bs_price,
    implied_vol,
    payoff_at_expiry,
    position_pl,
    premium_barrier_underlying,
    prob_itm,
    prob_touch,
)


def test_bs_known_values():
    # Hull-style reference: S=42, K=40, r=0.10, sigma=0.2, T=0.5 -> C≈4.759, P≈0.808
    c = bs_price(42, 40, 0.5, 0.2, "C", r=0.10)
    p = bs_price(42, 40, 0.5, 0.2, "P", r=0.10)
    assert abs(c - 4.759) < 0.01
    assert abs(p - 0.808) < 0.01


def test_put_call_parity():
    S, K, tau, sigma, r = 452.0, 455.0, 2.5 / 252, 0.19, 0.05
    c = bs_price(S, K, tau, sigma, "C", r)
    p = bs_price(S, K, tau, sigma, "P", r)
    assert abs((c - p) - (S - K * math.exp(-r * tau))) < 1e-9


def test_bs_boundaries():
    assert bs_price(450, 455, 0.0, 0.2, "C") == 0.0
    assert bs_price(460, 455, 0.0, 0.2, "C") == 5.0
    assert bs_price(450, 455, 0.01, 0.0, "P") == 5.0
    assert bs_price(0.0, 455, 0.01, 0.2, "C") == 0.0
    # Deep ITM/OTM don't blow up.
    assert bs_price(1000, 10, 0.1, 0.2, "C") > 980
    assert bs_price(10, 1000, 0.1, 0.2, "C") < 1e-6


def test_implied_vol_roundtrip():
    S, K, tau, r = 452.0, 455.0, 3.0 / 252, 0.05
    for sigma in (0.08, 0.19, 0.45, 1.2):
        price = bs_price(S, K, tau, sigma, "C", r)
        solved = implied_vol(price, S, K, tau, "C", r)
        assert solved is not None and abs(solved - sigma) < 1e-4


def test_implied_vol_rejects_bad_quotes():
    assert implied_vol(0.0, 450, 455, 0.01, "C") is None
    assert implied_vol(4.9, 460, 455, 0.01, "C") is None  # below intrinsic 5


def long_call(strike=455.0, entry=2.0, iv=0.2) -> list[Leg]:
    return [Leg("C", strike, 1, 1, entry, iv)]


def call_debit_spread() -> list[Leg]:
    return [Leg("C", 450.0, 1, 1, 4.0, 0.2), Leg("C", 455.0, 1, -1, 1.8, 0.19)]


def test_payoff_and_breakevens_long_call():
    legs = long_call()
    assert payoff_at_expiry(legs, 450) == -2.0
    assert payoff_at_expiry(legs, 460) == 3.0
    bes = breakevens(legs, 400, 500)
    assert len(bes) == 1 and abs(bes[0] - 457.0) < 0.01


def test_payoff_and_breakevens_spread():
    legs = call_debit_spread()  # net debit 2.2, BE 452.2, max profit 2.8
    assert abs(payoff_at_expiry(legs, 440) + 2.2) < 1e-9
    assert abs(payoff_at_expiry(legs, 470) - 2.8) < 1e-9
    bes = breakevens(legs, 400, 500)
    assert len(bes) == 1 and abs(bes[0] - 452.2) < 0.01


def test_position_pl_decays_toward_expiry_payoff():
    legs = long_call(entry=2.0)
    tau = 2.0 * 6.5 / (252 * 6.5)
    early = position_pl(legs, 455.0, tau)
    late = position_pl(legs, 455.0, tau / 50)
    assert early > late  # ATM time value decays
    assert abs(position_pl(legs, 455.0, 0.0) - (-2.0)) < 1e-9


def test_prob_itm_sane():
    tau = 2.0 / 252
    p_atm = prob_itm(452, 452, tau, 0.2, "C")
    assert 0.45 < p_atm < 0.52
    assert prob_itm(452, 400, tau, 0.2, "C") > 0.99
    assert prob_itm(452, 500, tau, 0.2, "C") < 0.01
    assert abs(prob_itm(452, 455, tau, 0.2, "C") + prob_itm(452, 455, tau, 0.2, "P") - 1.0) < 1e-9


def mc_touch_probability(S, barrier, tau, sigma, r, paths=60_000, steps=250, seed=7):
    rng = random.Random(seed)
    nu = (r - 0.5 * sigma * sigma)
    dt = tau / steps
    up = barrier > S
    hits = 0
    for _ in range(paths):
        x = math.log(S)
        target = math.log(barrier)
        hit = False
        for _ in range(steps):
            x += nu * dt + sigma * math.sqrt(dt) * rng.gauss(0, 1)
            if (up and x >= target) or (not up and x <= target):
                hit = True
                break
        hits += hit
    return hits / paths


@pytest.mark.parametrize(
    "barrier",
    [462.0, 445.0],  # upper and lower
)
def test_prob_touch_vs_monte_carlo(barrier):
    S, tau, sigma, r = 452.0, 3.0 / 252, 0.25, 0.05
    analytic = prob_touch(S, barrier, tau, sigma, r)
    mc = mc_touch_probability(S, barrier, tau, sigma, r)
    # Discrete-time MC underestimates continuous touch slightly; loose tolerance.
    assert abs(analytic - mc) < 0.03


def test_prob_touch_bounds():
    assert prob_touch(452, 452.0000001, 2 / 252, 0.2) > 0.99
    assert prob_touch(452, 900, 2 / 252, 0.2) < 1e-6
    assert prob_touch(452, 445, 0.0, 0.2) == 0.0


def test_premium_barrier_solve():
    legs = long_call(strike=455.0, entry=2.0, iv=0.2)
    tau_eval = 1.0 / 252
    target = 4.0  # find S where the call is worth 4.00 at tau_eval
    s_star = premium_barrier_underlying(legs, target, tau_eval, 300, 600)
    assert s_star is not None
    assert abs(bs_price(s_star, 455.0, tau_eval, 0.2, "C") - 4.0) < 1e-6


def test_compute_probabilities_consistency():
    legs = long_call(strike=455.0, entry=2.0, iv=0.2)
    out = compute_probabilities(legs, 452.0, 13.0, tp_premium=4.0, sl_premium=1.0)
    assert 0.0 < out["p_profit_expiry"] < 0.6
    assert out["p_touch_tp"] is not None and 0 < out["p_touch_tp"] < 1
    assert out["p_touch_sl"] is not None and 0 < out["p_touch_sl"] < 1
    assert out["tp_barrier"] > 452.0 > out["sl_barrier"]
    assert out["rr"] == pytest.approx(2.0)
    assert out["breakevens"] == [457.0]


def test_sizing_respects_budgets():
    legs = long_call(entry=2.0)
    sizing = compute_sizing(legs, 25_000, 0.02, sl_premium=1.0, bp_cap_pct=0.25)
    # risk/contract = (2-1)*100 = $100; budget = $500 -> 5 contracts; cost $1000 < bp cap
    assert sizing.contracts == 5
    assert sizing.max_loss_at_stop == 500.0
    assert sizing.entry_cost == 1000.0

    capped = compute_sizing(legs, 25_000, 0.02, sl_premium=1.9, bp_cap_pct=0.02)
    # risk $10 -> 50 contracts; bp cap $500 // $200 = 2 contracts
    assert capped.contracts == 2
    assert any("buying-power" in reason for reason in capped.reasons)


def test_sizing_refuses_credit_and_inverted_stops():
    credit = [Leg("C", 450, 1, -1, 2.0, 0.2)]
    assert compute_sizing(credit, 25_000, 0.02, 1.0, 0.25).contracts == 0
    legs = long_call(entry=2.0)
    assert compute_sizing(legs, 25_000, 0.02, sl_premium=2.5, bp_cap_pct=0.25).contracts == 0


def test_position_iv_weighting():
    legs = [Leg("C", 450, 1, 1, 4.0, 0.20), Leg("C", 455, 1, -1, 2.0, 0.30)]
    iv = position_iv(legs)
    assert abs(iv - (4 * 0.20 + 2 * 0.30) / 6) < 1e-9
