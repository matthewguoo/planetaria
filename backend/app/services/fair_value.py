"""Fair-value estimation for option positions — the price that stop/target
triggers act on.

This is the estimator stack production desks actually run, not a smoothing
heuristic:

1. MICROPRICE, not mid (Stoikov). The size-weighted quote price
       I = bid_size / (bid_size + ask_size)
       micro = I * ask + (1 - I) * bid
   leans toward the side with resting pressure — a heavy bid pushes fair
   value toward the ask. It is a strictly better short-horizon predictor
   of the next trade price than the raw mid. Quotes without sizes fall
   back to the mid.

2. SPREAD-GATED TRUST. A quote's information content is bounded by its
   spread: measurement variance R = (position half-spread)^2. A 4c-wide
   real market snaps the estimate; a junk one-sided or blown-out quote
   barely moves it. Crossed/locked markets are treated as wide (suspect),
   the same way an exchange marks an unreliable NBBO.

3. THEO-DRIFT PREDICTION. Between option quotes, the state is moved by
   the CHANGE in model value (BSM off underlying ticks — the underlying
   streams constantly even when the contracts don't). Using differences
   cancels any level bias in the model; only its dynamics are trusted.

Together: a 1-D Kalman filter whose process model is the theoretical
value and whose observations are microprices weighted by quote quality.
"""

from __future__ import annotations

# A quote missing one side (or crossed) carries little information: its
# effective half-spread is floored at this fraction of the price level.
ONE_SIDED_HS_FRAC = 0.25
CROSSED_HS_FRAC = 0.10
MIN_HALF_SPREAD = 0.01


def leg_microprice(quote: dict) -> float | None:
    """Size-weighted quote price for one leg; mid when sizes are absent."""
    bid, ask = quote.get("bid"), quote.get("ask")
    if not bid or not ask:
        return quote.get("mid")
    bs, asz = quote.get("bid_size"), quote.get("ask_size")
    if not bs or not asz or bs <= 0 or asz <= 0:
        return quote.get("mid")
    imb = bs / (bs + asz)
    return imb * ask + (1 - imb) * bid


def leg_half_spread(quote: dict) -> float:
    """Effective half-spread of one leg's quote — its uncertainty."""
    bid, ask = quote.get("bid"), quote.get("ask")
    mid = abs(quote.get("mid") or 0.0)
    if not bid or not ask:
        return max(mid * ONE_SIDED_HS_FRAC, MIN_HALF_SPREAD)
    hs = (ask - bid) / 2
    if hs <= 0:  # crossed/locked: the book disagrees with itself
        return max(mid * CROSSED_HS_FRAC, MIN_HALF_SPREAD)
    return max(hs, MIN_HALF_SPREAD)


def position_quote_stats(
    legs: list[dict], quotes: dict[str, dict]
) -> tuple[float, float] | None:
    """(microprice value, half-spread) of the whole position, per share.
    None when any leg has no usable quote — same evaluability rule as
    position_mid_from_quotes."""
    value = 0.0
    half_spread = 0.0
    for leg in legs:
        quote = quotes.get(leg["symbol"])
        if not quote or not (quote.get("bid") or quote.get("ask")):
            return None
        micro = leg_microprice(quote)
        if micro is None:
            return None
        weight = leg["side"] * leg.get("ratio", 1)
        value += weight * micro
        half_spread += abs(weight) * leg_half_spread(quote)
    return value, half_spread


class FairValueFilter:
    """1-D Kalman filter over a position's premium.

    State x = fair value, variance P.
    Predict: on_model_value() applies the model-value CHANGE (theo drift
    from underlying ticks) plus a small step uncertainty.
    Update: on_quote() blends in a microprice observation with
    R = half_spread^2; process noise accumulated between quotes at
    q_rate (variance/second, scaled by the caller to the bracket span).
    """

    def __init__(self) -> None:
        self.x: float | None = None
        self.P: float = 0.0
        self._last_quote_t: float | None = None
        self._last_model: float | None = None

    @property
    def value(self) -> float | None:
        return self.x

    def on_model_value(self, mv: float | None) -> None:
        if mv is None:
            return
        if self._last_model is not None and self.x is not None:
            step = mv - self._last_model
            self.x += step
            # Trusting the model's dynamics, not its level: uncertainty
            # grows with the size of the step it claims.
            self.P += (0.25 * abs(step)) ** 2
        self._last_model = mv

    def on_quote(self, micro: float, half_spread: float, now: float, q_rate: float) -> float:
        hs = max(half_spread, MIN_HALF_SPREAD)
        R = hs * hs
        if self.x is None:
            self.x = micro
            self.P = R
            self._last_quote_t = now
            return self.x
        dt = max(now - (self._last_quote_t if self._last_quote_t is not None else now), 0.0)
        self._last_quote_t = now
        self.P += q_rate * max(dt, 1e-3)
        K = self.P / (self.P + R)
        self.x += K * (micro - self.x)
        self.P *= 1 - K
        return self.x
