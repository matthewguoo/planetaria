"""Position analytics assembled from the math core: sizing, probabilities,
heatmap surfaces. Single source of truth at order time — the TS mirror gives
instant drag feedback, this module gives the authoritative numbers.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from app.services.options_math import (
    RISK_FREE,
    TRADING_HOURS_PER_YEAR,
    Leg,
    breakevens,
    payoff_at_expiry,
    position_entry_cost,
    position_pl,
    position_value,
    premium_barrier_underlying,
    prob_above_at_expiry,
    prob_touch,
    structural_max_loss,
    terminal_ev,
)


@dataclass
class SizingResult:
    contracts: int
    entry_cost: float          # dollars, total (per contract-set * contracts)
    max_loss_at_stop: float    # dollars at SL
    max_loss_structural: float # dollars if held to worst case (long: full debit)
    buying_power_pct: float
    per_contract_risk: float
    reasons: list[str]


def position_iv(legs: list[Leg]) -> float:
    """Risk IV for the underlying process: entry-cost-weighted leg IV."""
    weights = [abs(leg.qty * leg.entry) for leg in legs]
    total = sum(weights)
    if total <= 0:
        ivs = [leg.iv for leg in legs if leg.iv > 0]
        return sum(ivs) / len(ivs) if ivs else 0.0
    return sum(w * leg.iv for w, leg in zip(weights, legs)) / total


def compute_sizing(
    legs: list[Leg],
    account_equity: float,
    max_loss_pct: float,
    sl_premium: float,
    bp_cap_pct: float,
) -> SizingResult:
    reasons: list[str] = []
    entry = position_entry_cost(legs)  # per share, one contract-set (+debit / -credit)
    if abs(entry) < 0.01:
        return SizingResult(0, 0, 0, 0, 0, 0, ["net premium is zero - nothing to size"])

    # Risk per set is stop-based and sign-agnostic: SL premium sits below entry
    # on the position-value axis for debit AND credit structures alike.
    per_set_risk = max((entry - sl_premium) * 100, 0.0)
    if per_set_risk <= 0:
        return SizingResult(0, 0, 0, 0, 0, 0, ["stop-loss premium must be below entry"])

    # Capital consumed per set: debit paid for longs; margin (structural max
    # loss) for defined-risk credit; stop-risk proxy for undefined-risk shorts.
    structural = structural_max_loss(legs)
    if entry > 0:
        per_set_cost = entry * 100
        per_set_structural = structural * 100 if structural is not None else entry * 100
    else:
        if structural is None:
            per_set_cost = per_set_risk * 3
            per_set_structural = per_set_cost
            reasons.append("undefined risk (net short calls) - stop-based sizing only")
        else:
            per_set_cost = structural * 100
            per_set_structural = structural * 100

    budget = account_equity * max_loss_pct
    contracts = int(budget // per_set_risk)
    if contracts < 1:
        reasons.append(
            f"risk per contract ${per_set_risk:.0f} exceeds budget ${budget:.0f}"
        )
        contracts = 0

    bp_budget = account_equity * bp_cap_pct
    if contracts * per_set_cost > bp_budget and per_set_cost > 0:
        capped = int(bp_budget // per_set_cost)
        if capped < contracts:
            contracts = capped
            reasons.append(f"capped by buying-power limit {bp_cap_pct:.0%}")

    return SizingResult(
        contracts=contracts,
        entry_cost=round(contracts * per_set_cost, 2),
        max_loss_at_stop=round(contracts * per_set_risk, 2),
        max_loss_structural=round(contracts * per_set_structural, 2),
        buying_power_pct=round(contracts * per_set_cost / account_equity, 4) if account_equity else 0.0,
        per_contract_risk=round(per_set_risk, 2),
        reasons=reasons,
    )


def compute_probabilities(
    legs: list[Leg],
    spot: float,
    hours_to_expiry: float,
    tp_premium: float | None,
    sl_premium: float | None,
) -> dict:
    tau = max(hours_to_expiry, 0.0) / TRADING_HOURS_PER_YEAR
    sigma = position_iv(legs)
    entry = position_entry_cost(legs)

    lo, hi = spot * 0.75, spot * 1.25
    bes = breakevens(legs, lo, hi)

    # P(profit at expiry): sum probability mass over profitable regions.
    edges = [lo] + bes + [hi]
    p_profit = 0.0
    for a, b in zip(edges[:-1], edges[1:]):
        mid = 0.5 * (a + b)
        if payoff_at_expiry(legs, mid) > 0:
            pa = prob_above_at_expiry(spot, a, tau, sigma)
            pb = prob_above_at_expiry(spot, b, tau, sigma)
            p_profit += pa - pb

    # Map TP/SL premiums to underlying barriers at mid-horizon, then use
    # first-passage. Assumption (shown in UI): barrier evaluated at tau/2.
    tau_eval = tau / 2
    p_tp = p_sl = None
    tp_barrier = sl_barrier = None
    if tp_premium is not None:
        tp_barrier = premium_barrier_underlying(legs, tp_premium, tau_eval, lo, hi)
        if tp_barrier is not None:
            p_tp = prob_touch(spot, tp_barrier, tau, sigma)
    if sl_premium is not None:
        sl_barrier = premium_barrier_underlying(legs, sl_premium, tau_eval, lo, hi)
        if sl_barrier is not None:
            p_sl = prob_touch(spot, sl_barrier, tau, sigma)

    ev = terminal_ev(legs, spot, tau, sigma, tp_premium, sl_premium) * 100

    reward = (tp_premium - entry) * 100 if tp_premium is not None else None
    risk = (entry - sl_premium) * 100 if sl_premium is not None else None
    rr = round(reward / risk, 2) if reward and risk and risk > 0 else None

    return {
        "p_profit_expiry": round(p_profit, 4),
        "breakevens": [round(b, 2) for b in bes],
        "p_touch_tp": round(p_tp, 4) if p_tp is not None else None,
        "p_touch_sl": round(p_sl, 4) if p_sl is not None else None,
        "tp_barrier": round(tp_barrier, 2) if tp_barrier is not None else None,
        "sl_barrier": round(sl_barrier, 2) if sl_barrier is not None else None,
        "ev_per_contract": round(ev, 2),
        "reward_per_contract": round(reward, 2) if reward is not None else None,
        "risk_per_contract": round(risk, 2) if risk is not None else None,
        "rr": rr,
        "sigma_used": round(sigma, 4),
        "assumptions": [
            "sticky-strike IV held constant",
            "risk-neutral GBM drift r=%.2f" % RISK_FREE,
            "touch barriers mapped at half-horizon",
            "EV truncated at TP/SL on terminal distribution",
        ],
    }


def compute_heatmap(
    legs: list[Leg],
    spot: float,
    hours_to_expiry: float,
    price_lo: float,
    price_hi: float,
    price_steps: int = 80,
    time_steps: int = 60,
) -> dict:
    """P/L surface: rows = time (now -> expiry), cols = price (lo -> hi).
    Values are dollars per contract-set."""
    price_steps = max(2, min(price_steps, 200))
    time_steps = max(2, min(time_steps, 150))
    prices = [price_lo + (price_hi - price_lo) * i / (price_steps - 1) for i in range(price_steps)]
    hours = [hours_to_expiry * (1 - i / (time_steps - 1)) for i in range(time_steps)]
    grid: list[list[float]] = []
    for h in hours:
        tau = max(h, 0.0) / TRADING_HOURS_PER_YEAR
        row = [round(position_pl(legs, s, tau) * 100, 2) for s in prices]
        grid.append(row)
    flat = [v for row in grid for v in row]
    return {
        "prices": [round(p, 4) for p in prices],
        "hours": [round(h, 4) for h in hours],
        "pl": grid,
        "min_pl": min(flat),
        "max_pl": max(flat),
    }


def legs_from_payload(payload: list[dict]) -> list[Leg]:
    legs = [Leg.from_dict(d) for d in payload]
    if not legs:
        raise ValueError("at least one leg required")
    for leg in legs:
        if leg.right not in ("C", "P"):
            raise ValueError(f"bad right {leg.right!r}")
        if leg.strike <= 0 or leg.qty <= 0 or leg.side not in (1, -1):
            raise ValueError("bad leg parameters")
        if leg.iv <= 0 or not math.isfinite(leg.iv):
            raise ValueError("leg missing IV")
    return legs
