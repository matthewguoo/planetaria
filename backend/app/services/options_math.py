"""Options math core: Black-Scholes, multi-leg position valuation, first-passage
probabilities, EV. Mirrored in frontend/src/lib/optionsMath.ts — the two MUST
agree (tests/test_parity.py exports fixtures the frontend test suite replays).

Conventions (identical in TS mirror):
- tau is in TRADING years: hours_to_expiry / (252 * 6.5).
- Prices per share; position P/L multiplies by 100 * qty.
- Legs: {right: 'C'|'P', strike, qty, side: +1|-1, entry: premium/share, iv}.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TRADING_HOURS_PER_YEAR = 252 * 6.5
RISK_FREE = 0.05  # config later; must match TS mirror

SQRT_2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    if x > 8.0:
        return 1.0
    if x < -8.0:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, tau: float, sigma: float, right: str, r: float = RISK_FREE) -> float:
    """European BS price with intrinsic-value clamps at the boundaries."""
    if S <= 0:
        return 0.0 if right == "C" else K * math.exp(-r * max(tau, 0.0))
    if tau <= 0 or sigma <= 0:
        intrinsic = S - K if right == "C" else K - S
        return max(intrinsic, 0.0)
    sq = sigma * math.sqrt(tau)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / sq
    d2 = d1 - sq
    if right == "C":
        return S * norm_cdf(d1) - K * math.exp(-r * tau) * norm_cdf(d2)
    return K * math.exp(-r * tau) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S: float, K: float, tau: float, sigma: float, right: str, r: float = RISK_FREE) -> float:
    if tau <= 0 or sigma <= 0 or S <= 0:
        if right == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / (sigma * math.sqrt(tau))
    return norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0


def bs_greeks(S: float, K: float, tau: float, sigma: float, right: str,
              r: float = RISK_FREE) -> dict:
    """Per-share BS greeks in display-friendly units:
    delta (shares), gamma (per $), theta_day ($ per TRADING day),
    vega_pt ($ per 1 vol POINT), rho_pct ($ per +1% rate)."""
    if S <= 0 or tau <= 0 or sigma <= 0:
        return {"delta": bs_delta(S, K, tau, sigma, right, r),
                "gamma": 0.0, "theta_day": 0.0, "vega_pt": 0.0, "rho_pct": 0.0}
    sq = sigma * math.sqrt(tau)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * tau) / sq
    d2 = d1 - sq
    pdf = norm_pdf(d1)
    disc = math.exp(-r * tau)
    delta = norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0
    gamma = pdf / (S * sq)
    vega = S * pdf * math.sqrt(tau)  # per 1.00 of vol
    decay = -(S * pdf * sigma) / (2.0 * math.sqrt(tau))
    if right == "C":
        theta = decay - r * K * disc * norm_cdf(d2)
        rho = K * tau * disc * norm_cdf(d2)  # per 1.00 of rate
    else:
        theta = decay + r * K * disc * norm_cdf(-d2)
        rho = -K * tau * disc * norm_cdf(-d2)
    return {
        "delta": delta,
        "gamma": gamma,
        "theta_day": theta / 252.0,
        "vega_pt": vega / 100.0,
        "rho_pct": rho / 100.0,
    }


def implied_vol(price: float, S: float, K: float, tau: float, right: str, r: float = RISK_FREE) -> float | None:
    """Bisection IV solve; None when the quote is outside no-arbitrage bounds."""
    if tau <= 0 or price <= 0 or S <= 0:
        return None
    intrinsic = max((S - K) if right == "C" else (K - S), 0.0)
    if price <= intrinsic + 1e-10:
        return None
    lo, hi = 1e-4, 10.0
    if bs_price(S, K, tau, hi, right, r) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, tau, mid, right, r) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def trading_hours_to_expiry(expiry_iso: str, now_utc_ms: float) -> float:
    """Trading hours from now to expiry-day 16:00 ET. Approximation shared
    verbatim with the TS mirror: full 6.5h per intervening weekday + the
    remaining fraction of today's session (clamped to [0, 6.5])."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = _dt.datetime.fromtimestamp(now_utc_ms / 1000, tz=_dt.timezone.utc).astimezone(et)
    expiry = _dt.date.fromisoformat(expiry_iso)
    close_today = now.replace(hour=16, minute=0, second=0, microsecond=0)
    hours_today = max(0.0, min((close_today - now).total_seconds() / 3600.0, 6.5))
    days = 0
    d = now.date()
    while d < expiry:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    if expiry == now.date():
        return hours_today
    return hours_today + days * 6.5


@dataclass
class Leg:
    right: str  # 'C' | 'P'
    strike: float
    qty: int
    side: int  # +1 long, -1 short
    entry: float  # premium per share at entry
    iv: float

    @staticmethod
    def from_dict(d: dict) -> "Leg":
        return Leg(
            right=d["right"],
            strike=float(d["strike"]),
            qty=int(d.get("qty", 1)),
            side=int(d.get("side", 1)),
            entry=float(d.get("entry", 0.0)),
            iv=float(d.get("iv", 0.0)),
        )


def position_value(legs: list[Leg], S: float, tau: float, r: float = RISK_FREE) -> float:
    """Theoretical value per 1x position, per share (sum of leg values)."""
    return sum(
        leg.side * leg.qty * bs_price(S, leg.strike, tau, leg.iv, leg.right, r)
        for leg in legs
    )


def position_entry_cost(legs: list[Leg]) -> float:
    """Net debit (+) / credit (-) per share."""
    return sum(leg.side * leg.qty * leg.entry for leg in legs)


def position_pl(legs: list[Leg], S: float, tau: float, r: float = RISK_FREE) -> float:
    """P/L per share vs entry (multiply by 100 for per-contract-set dollars)."""
    return position_value(legs, S, tau, r) - position_entry_cost(legs)


def payoff_at_expiry(legs: list[Leg], S: float) -> float:
    total = 0.0
    for leg in legs:
        intrinsic = max((S - leg.strike) if leg.right == "C" else (leg.strike - S), 0.0)
        total += leg.side * leg.qty * intrinsic
    return total - position_entry_cost(legs)


def structural_max_loss(legs: list[Leg]) -> float | None:
    """Worst-case loss per share if held to expiry, as a positive number.
    None means unbounded (net short calls). Expiry payoff is piecewise linear
    with kinks only at strikes, so checking kinks + endpoints is exact."""
    slope_up = sum(leg.side * leg.qty for leg in legs if leg.right == "C")
    if slope_up < 0:
        return None
    strikes = [leg.strike for leg in legs]
    points = [0.0, *strikes, max(strikes) * 2.0]
    worst = min(payoff_at_expiry(legs, s) for s in points)
    return -min(worst, 0.0)


def bp_per_set_estimate(legs: list[Leg], spot: float) -> float:
    """Buying-power estimate per contract-set, broker-margin style (mirrors
    frontend bpPerSetEstimate — parity-tested).

    Defined-risk structures consume structural max loss. Uncovered short
    units use the standard naked-option margin
        max(20% * spot - OTM, 10% * strike-or-spot floor) * 100
    instead of full cash-secured/structural value. Net debit adds when paid.
    An estimate: the broker applies its real margin at submit.
    """
    entry = position_entry_cost(legs)
    debit = max(entry, 0.0) * 100
    structural = structural_max_loss(legs)

    def short_units(right: str) -> int:
        return max(-sum(l.side * l.qty for l in legs if l.right == right), 0)

    naked_calls = short_units("C")
    naked_puts = short_units("P")
    if naked_calls == 0 and naked_puts == 0 and structural is not None:
        return max(structural * 100, debit)

    margin = 0.0
    if spot > 0:
        # Uncovered short puts: CASH-SECURED strike value. Broker-verified:
        # an oversized jade-lizard probe was rejected with cost_basis
        # $70,087/set for a 700P — Alpaca charges ~strike*100, not 20% Reg-T.
        puts_left = naked_puts
        for leg in sorted((l for l in legs if l.right == "P" and l.side < 0),
                          key=lambda l: -l.strike):
            units = min(leg.qty, puts_left)
            if units <= 0:
                continue
            puts_left -= units
            margin += units * leg.strike * 100
        calls_left = naked_calls
        for leg in sorted((l for l in legs if l.right == "C" and l.side < 0),
                          key=lambda l: l.strike):
            units = min(leg.qty, calls_left)
            if units <= 0:
                continue
            calls_left -= units
            otm = max(leg.strike - spot, 0.0)
            margin += units * max(0.2 * spot - otm, 0.1 * spot) * 100
    covered_part = (
        structural * 100
        if structural is not None and naked_calls == 0 and naked_puts == 0
        else 0.0
    )
    return max(margin + debit + covered_part, debit, 1.0)


def breakevens(legs: list[Leg], lo: float, hi: float, steps: int = 2000) -> list[float]:
    """Zero crossings of expiry payoff, refined by bisection."""
    out: list[float] = []
    prev_s = lo
    prev_v = payoff_at_expiry(legs, lo)
    for i in range(1, steps + 1):
        s = lo + (hi - lo) * i / steps
        v = payoff_at_expiry(legs, s)
        if prev_v == 0.0:
            out.append(prev_s)
        elif (prev_v < 0) != (v < 0):
            a, b = prev_s, s
            for _ in range(60):
                m = 0.5 * (a + b)
                if (payoff_at_expiry(legs, m) < 0) == (prev_v < 0):
                    a = m
                else:
                    b = m
            out.append(0.5 * (a + b))
        prev_s, prev_v = s, v
    # Dedup within a cent.
    dedup: list[float] = []
    for be in out:
        if not dedup or abs(be - dedup[-1]) > 0.01:
            dedup.append(round(be, 4))
    return dedup


# ------------------------------------------------------------- probabilities

def prob_itm(S: float, K: float, tau: float, sigma: float, right: str, r: float = RISK_FREE) -> float:
    """Risk-neutral P(S_T beyond K): N(d2) for calls, N(-d2) for puts."""
    if tau <= 0 or sigma <= 0 or S <= 0:
        if right == "C":
            return 1.0 if S > K else 0.0
        return 1.0 if S < K else 0.0
    sq = sigma * math.sqrt(tau)
    d2 = (math.log(S / K) + (r - 0.5 * sigma * sigma) * tau) / sq
    return norm_cdf(d2) if right == "C" else norm_cdf(-d2)


def prob_above_at_expiry(S: float, level: float, tau: float, sigma: float, r: float = RISK_FREE) -> float:
    return prob_itm(S, level, tau, sigma, "C", r)


def prob_touch(S: float, barrier: float, tau: float, sigma: float, r: float = RISK_FREE) -> float:
    """P(GBM touches barrier before tau), WITH drift (reflection principle).

    nu = r - sigma^2/2 (risk-neutral log drift); b = log(barrier/S).
    Upper barrier (b>0):  P = N((nu*tau - b)/(sig*sqrt(tau)))
                             + exp(2*nu*b/sig^2) * N((-nu*tau - b)/(sig*sqrt(tau)))
    Lower barrier by symmetry (negate b and nu).
    """
    if tau <= 0 or sigma <= 0 or S <= 0 or barrier <= 0:
        return 0.0
    b = math.log(barrier / S)
    if abs(b) < 1e-12:
        return 1.0
    nu = r - 0.5 * sigma * sigma
    if b < 0:
        b, nu = -b, -nu
    sq = sigma * math.sqrt(tau)
    exponent = 2.0 * nu * b / (sigma * sigma)
    exponent = min(exponent, 700.0)  # overflow guard; result clamps to 1
    p = norm_cdf((nu * tau - b) / sq) + math.exp(exponent) * norm_cdf((-nu * tau - b) / sq)
    return min(max(p, 0.0), 1.0)


def premium_barrier_underlying(
    legs: list[Leg], target_premium: float, tau_eval: float,
    lo: float, hi: float, r: float = RISK_FREE,
) -> float | None:
    """Solve S* where position value == target premium at tau_eval (bisection).
    Used to map TP/SL premium levels onto underlying barriers. Returns None
    if no crossing exists in [lo, hi] (e.g. unreachable TP)."""
    f = lambda s: position_value(legs, s, tau_eval, r) - target_premium
    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    if (flo < 0) == (fhi < 0):
        return None
    a, b = lo, hi
    for _ in range(80):
        m = 0.5 * (a + b)
        if (f(m) < 0) == (flo < 0):
            a = m
        else:
            b = m
    return 0.5 * (a + b)


# ---------------------------------------------------------------------- EV

def terminal_ev(
    legs: list[Leg], S: float, tau: float, sigma: float,
    tp_premium: float | None, sl_premium: float | None,
    r: float = RISK_FREE, steps: int = 400,
) -> float:
    """EV per share under the risk-neutral terminal lognormal, with payoff
    truncated at TP/SL premium levels (approximation of path-wise exits;
    the touch probabilities quantify how often exits fire early).
    """
    if tau <= 0 or sigma <= 0:
        return payoff_at_expiry(legs, S)
    entry = position_entry_cost(legs)
    nu = (r - 0.5 * sigma * sigma) * tau
    sq = sigma * math.sqrt(tau)
    lo_z, hi_z = -5.0, 5.0
    total = 0.0
    dz = (hi_z - lo_z) / steps
    for i in range(steps + 1):
        z = lo_z + i * dz
        weight = norm_pdf(z) * dz * (0.5 if i in (0, steps) else 1.0)
        s_t = S * math.exp(nu + sq * z)
        pl = payoff_at_expiry(legs, s_t)
        if tp_premium is not None:
            pl = min(pl, tp_premium - entry)
        if sl_premium is not None:
            pl = max(pl, sl_premium - entry)
        total += weight * pl
    return total
