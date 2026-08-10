import math
import random

import pytest

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


def test_bp_estimate_margin_model():
    from app.services.options_math import bp_per_set_estimate

    # Uncovered short puts are CASH-SECURED at this broker (empirically:
    # jade-lizard probe rejected with cost_basis $70,087/set for a 700P).
    jade = [
        Leg("P", 700.0, 1, -1, 0.115, 0.31),
        Leg("C", 778.0, 1, -1, 0.035, 0.18),
        Leg("C", 779.0, 1, 1, 0.015, 0.17),
    ]
    bp = bp_per_set_estimate(jade, 741.83)
    assert abs(bp - 70_087) / 70_087 < 0.01  # within 1% of Alpaca's number

    rr = [Leg("P", 750.0, 1, -1, 9.3, 0.2), Leg("C", 744.0, 1, 1, 1.6, 0.2)]
    assert abs(bp_per_set_estimate(rr, 743.57) - 75_000.0) < 1.0

    # Defined-risk debit spread: BP = structural max loss (the debit).
    debit = [Leg("C", 450.0, 1, 1, 4.0, 0.2), Leg("C", 455.0, 1, -1, 1.8, 0.2)]
    assert abs(bp_per_set_estimate(debit, 452.0) - 220.0) < 1e-6

    # Cash-secured put: full strike value.
    csp = [Leg("P", 300.0, 1, -1, 0.10, 0.3)]
    assert abs(bp_per_set_estimate(csp, 743.57) - 300 * 100) < 1e-6


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


def test_structural_max_loss():
    from app.services.options_math import structural_max_loss

    # Long call: max loss = debit.
    assert structural_max_loss(long_call(entry=2.0)) == pytest.approx(2.0)
    # Call credit spread: width - credit.
    spread = [Leg("C", 450, 1, -1, 2.0, 0.2), Leg("C", 455, 1, 1, 0.8, 0.19)]
    assert structural_max_loss(spread) == pytest.approx(5.0 - 1.2)
    # Naked short call: unbounded.
    assert structural_max_loss([Leg("C", 450, 1, -1, 2.0, 0.2)]) is None
    # Short put: bounded by strike (S=0).
    short_put = [Leg("P", 450, 1, -1, 3.0, 0.2)]
    assert structural_max_loss(short_put) == pytest.approx(450.0 - 3.0)
    # Iron condor: width - net credit.
    condor = [
        Leg("P", 440, 1, 1, 0.5, 0.2),
        Leg("P", 445, 1, -1, 1.2, 0.2),
        Leg("C", 460, 1, -1, 1.1, 0.2),
        Leg("C", 465, 1, 1, 0.4, 0.2),
    ]
    net_credit = 1.2 + 1.1 - 0.5 - 0.4
    assert structural_max_loss(condor) == pytest.approx(5.0 - net_credit)
