"""Portfolio-level risk: dollars-at-stop, factor exposures, correlations.

Answers three questions the per-order risk guards can't:
1. How much of the account is at risk RIGHT NOW if every open position
   stops out? (simple sum AND correlation-adjusted — three SPY/QQQ/IWM
   positions are nearly one position, not three independent bets)
2. What is the portfolio exposed to? Aggregate greeks: net delta dollars,
   SPY-beta-weighted delta dollars, vega/theta, and rho (interest-rate
   sensitivity in $ per +1% rates).
3. How correlated are the underlyings — to each other, to SPY, and to
   rates (daily corr vs 10y-yield changes, ^TNX)?

Return histories come from Yahoo's keyless daily chart (15-min cache) so
this works identically with or without Alpaca keys; when unreachable,
correlations degrade to the conservative ρ=1 (corr-adjusted == simple sum)
and the payload says so.
"""

import asyncio
import logging
import math
import time

import httpx

from app.models.trade import TradePlan
from app.services.demo_feed import YAHOO_CHART_URL, YAHOO_HEADERS
from app.services.options_math import (
    TRADING_HOURS_PER_YEAR,
    bs_greeks,
    trading_hours_to_expiry,
)

log = logging.getLogger("app.prisk")

HIST_TTL_S = 900.0
SNAPSHOT_TTL_S = 20.0
RATE_SYMBOL = "^TNX"  # CBOE 10y treasury yield index
FALLBACK_IV = 0.30


# ------------------------------------------------------------ pure math


def log_returns(closes: list[float]) -> list[float]:
    out = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def diffs(values: list[float]) -> list[float]:
    return [cur - prev for prev, cur in zip(values, values[1:])]


def _align(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
    n = min(len(a), len(b))
    return a[-n:], b[-n:]


def corr(a: list[float], b: list[float]) -> float | None:
    a, b = _align(a, b)
    n = len(a)
    if n < 10:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return max(-1.0, min(1.0, cov / math.sqrt(va * vb)))


def beta(asset: list[float], market: list[float]) -> float | None:
    asset, market = _align(asset, market)
    n = len(asset)
    if n < 10:
        return None
    ma, mm = sum(asset) / n, sum(market) / n
    cov = sum((x - ma) * (y - mm) for x, y in zip(asset, market))
    var = sum((y - mm) ** 2 for y in market)
    if var <= 0:
        return None
    return cov / var


def combine_correlated(risks: dict[str, float], rho: dict[tuple[str, str], float | None],
                       default_rho: float = 1.0) -> float:
    """sqrt(r' ρ r) over per-underlying stop-risk dollars. Unknown pairwise
    correlations use `default_rho` (1.0 = conservative, no diversification
    credit). Never reports less than the largest single risk."""
    symbols = sorted(risks)
    total = 0.0
    for i, si in enumerate(symbols):
        for sj in symbols[i:]:
            if si == sj:
                total += risks[si] * risks[sj]
                continue
            r = rho.get((si, sj), rho.get((sj, si)))
            r = default_rho if r is None else r
            total += 2.0 * risks[si] * risks[sj] * r
    combined = math.sqrt(max(total, 0.0))
    biggest = max(risks.values(), default=0.0)
    return max(combined, biggest)


def plan_stop_risk(plan: TradePlan) -> float:
    """Dollars lost if this plan exits exactly at its stop (per current or
    would-be quantity). Gap-through can exceed this — it is the PLANNED risk."""
    basis = plan.fill_premium if plan.fill_premium is not None else plan.entry_limit
    per_set = max(basis - plan.sl_premium, 0.0) * 100.0
    qty = plan.effective_qty if plan.filled_qty is not None else plan.qty
    return per_set * max(qty, 0)


# ------------------------------------------------------------- service


class PortfolioRisk:
    def __init__(self, trade, market):
        self.trade = trade
        self.market = market
        self._http: httpx.AsyncClient | None = None
        self._hist: dict[str, tuple[float, list[float]]] = {}
        self._snapshot: tuple[float, dict] | None = None
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers=YAHOO_HEADERS, timeout=8.0)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def daily_closes(self, symbol: str) -> list[float]:
        cached = self._hist.get(symbol)
        if cached and time.monotonic() - cached[0] < HIST_TTL_S:
            return cached[1]
        try:
            resp = await self._client().get(
                YAHOO_CHART_URL.format(symbol=symbol),
                params={"range": "3mo", "interval": "1d"},
            )
            resp.raise_for_status()
            result = (resp.json().get("chart") or {}).get("result") or [{}]
            quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            closes = [float(c) for c in (quote.get("close") or []) if c is not None]
        except Exception as exc:
            log.warning("daily history unavailable for %s: %s", symbol, exc)
            closes = []
        self._hist[symbol] = (time.monotonic(), closes)
        return closes

    def _spot(self, underlying: str) -> float | None:
        quote = self.market.latest_quote(underlying)
        if quote and quote.get("mid"):
            return float(quote["mid"])
        bars = self.market.bars.get_bars(underlying, "1m")
        return float(bars[-1]["c"]) if bars else None

    def _plan_greeks(self, plan: TradePlan, spot: float, now_ms: float) -> dict:
        """Aggregate $-greeks for one plan (per its full quantity)."""
        qty = plan.effective_qty if plan.filled_qty is not None else plan.qty
        agg = {"delta_dollars": 0.0, "vega_per_pt": 0.0, "theta_per_day": 0.0, "rho_per_pct": 0.0}
        for leg in plan.legs:
            tau = trading_hours_to_expiry(leg["expiry"], now_ms) / TRADING_HOURS_PER_YEAR
            sigma = float(leg.get("iv") or 0) or FALLBACK_IV
            g = bs_greeks(spot, float(leg["strike"]), tau, sigma, leg["right"])
            contracts = qty * leg.get("ratio", 1) * leg["side"]
            agg["delta_dollars"] += contracts * 100.0 * g["delta"] * spot
            agg["vega_per_pt"] += contracts * 100.0 * g["vega_pt"]
            agg["theta_per_day"] += contracts * 100.0 * g["theta_day"]
            agg["rho_per_pct"] += contracts * 100.0 * g["rho_pct"]
        return agg

    async def snapshot(self) -> dict:
        if self._snapshot and time.monotonic() - self._snapshot[0] < SNAPSHOT_TTL_S:
            return self._snapshot[1]
        async with self._lock:
            if self._snapshot and time.monotonic() - self._snapshot[0] < SNAPSHOT_TTL_S:
                return self._snapshot[1]
            snap = await self._build()
            self._snapshot = (time.monotonic(), snap)
            return snap

    async def _build(self) -> dict:
        plans = await self.trade.risk.open_plans()
        account = await self.trade.get_account()
        equity = float(account.get("equity") or 0.0)
        now_ms = time.time() * 1000

        per_plan: list[dict] = []
        risk_by_underlying: dict[str, float] = {}
        greeks_by_underlying: dict[str, dict] = {}
        totals = {"delta_dollars": 0.0, "vega_per_pt": 0.0, "theta_per_day": 0.0, "rho_per_pct": 0.0}
        for plan in plans:
            dollars = plan_stop_risk(plan)
            per_plan.append({
                "id": plan.id,
                "underlying": plan.underlying,
                "status": plan.status,
                "risk_dollars": round(dollars, 2),
                "risk_pct": round(dollars / equity * 100.0, 3) if equity > 0 else None,
            })
            risk_by_underlying[plan.underlying] = risk_by_underlying.get(plan.underlying, 0.0) + dollars
            spot = self._spot(plan.underlying)
            if spot:
                g = self._plan_greeks(plan, spot, now_ms)
                agg = greeks_by_underlying.setdefault(
                    plan.underlying,
                    {"delta_dollars": 0.0, "vega_per_pt": 0.0, "theta_per_day": 0.0, "rho_per_pct": 0.0},
                )
                for key in agg:
                    agg[key] += g[key]
                    totals[key] += g[key]

        # ---- correlations / factor betas (keyless public daily closes)
        underlyings = sorted(risk_by_underlying)
        fetch_symbols = sorted(set(underlyings) | {"SPY", RATE_SYMBOL})
        closes = dict(zip(fetch_symbols, await asyncio.gather(
            *[self.daily_closes(s) for s in fetch_symbols]
        )))
        returns = {s: log_returns(closes.get(s, [])) for s in fetch_symbols if s != RATE_SYMBOL}
        rate_changes = diffs(closes.get(RATE_SYMBOL, []))
        spy_returns = returns.get("SPY", [])
        history_ok = len(spy_returns) >= 10

        pair_rho: dict[tuple[str, str], float | None] = {}
        matrix: list[list[float | None]] = []
        for i, si in enumerate(underlyings):
            row: list[float | None] = []
            for j, sj in enumerate(underlyings):
                if i == j:
                    row.append(1.0)
                    continue
                r = corr(returns.get(si, []), returns.get(sj, []))
                pair_rho[(si, sj)] = r
                row.append(round(r, 3) if r is not None else None)
            matrix.append(row)

        underlying_rows = []
        beta_weighted_delta = 0.0
        for sym in underlyings:
            b = beta(returns.get(sym, []), spy_returns)
            spy_c = corr(returns.get(sym, []), spy_returns)
            rate_c = corr(returns.get(sym, []), rate_changes)
            delta_d = greeks_by_underlying.get(sym, {}).get("delta_dollars", 0.0)
            beta_weighted_delta += delta_d * (b if b is not None else 1.0)
            underlying_rows.append({
                "symbol": sym,
                "risk_dollars": round(risk_by_underlying[sym], 2),
                "risk_pct": round(risk_by_underlying[sym] / equity * 100.0, 3) if equity > 0 else None,
                "beta_spy": round(b, 2) if b is not None else None,
                "corr_spy": round(spy_c, 2) if spy_c is not None else None,
                "corr_rate": round(rate_c, 2) if rate_c is not None else None,
                "delta_dollars": round(delta_d, 2),
            })

        total_dollars = sum(risk_by_underlying.values())
        corr_dollars = combine_correlated(risk_by_underlying, pair_rho)
        concentration = (max(risk_by_underlying.values(), default=0.0) / total_dollars * 100.0
                         if total_dollars > 0 else 0.0)

        def pct(x: float) -> float | None:
            return round(x / equity * 100.0, 3) if equity > 0 else None

        return {
            "asof": int(now_ms),
            "equity": equity,
            "history_ok": history_ok,
            "total": {
                "risk_dollars": round(total_dollars, 2),
                "risk_pct": pct(total_dollars),
                "corr_risk_dollars": round(corr_dollars, 2),
                "corr_risk_pct": pct(corr_dollars),
                "concentration_pct": round(concentration, 1),
                "open_plans": len(plans),
            },
            "greeks": {
                "delta_dollars": round(totals["delta_dollars"], 2),
                "beta_weighted_delta_dollars": round(beta_weighted_delta, 2),
                "vega_per_pt": round(totals["vega_per_pt"], 2),
                "theta_per_day": round(totals["theta_per_day"], 2),
                "rho_per_pct": round(totals["rho_per_pct"], 2),
            },
            "per_plan": per_plan,
            "underlyings": underlying_rows,
            "matrix": {"symbols": underlyings, "rho": matrix},
        }
