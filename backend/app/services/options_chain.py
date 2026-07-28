"""Options chain service: 0-3 DTE window, greeks/IV included (free indicative
feed), 5s TTL cache with single-flight coalescing so strike drags and multiple
clients never hammer the 200 req/min REST budget.
"""

import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

from alpaca.data.requests import OptionChainRequest

from app.services.alpaca import AlpacaService
from app.services.redis_client import RedisFacade

log = logging.getLogger("app.chain")

CHAIN_TTL_S = 5.0


def _contract_row(occ_symbol: str, snap) -> dict | None:
    """Flatten an alpaca OptionsSnapshot into our wire format."""
    try:
        # OCC: ROOT + YYMMDD + C/P + strike*1000 (8 digits)
        body = occ_symbol
        strike = int(body[-8:]) / 1000.0
        right = body[-9].upper()
        expiry = "20" + body[-15:-9]  # YYMMDD -> YYYYMMDD
        expiry_iso = f"{expiry[0:4]}-{expiry[4:6]}-{expiry[6:8]}"
    except Exception:
        return None

    quote = getattr(snap, "latest_quote", None)
    greeks = getattr(snap, "greeks", None)
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    mid = round((bid + ask) / 2, 4) if (bid or ask) else 0.0
    iv = float(getattr(snap, "implied_volatility", 0) or 0)
    return {
        "symbol": occ_symbol,
        "right": "C" if right == "C" else "P",
        "strike": strike,
        "expiry": expiry_iso,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "iv": round(iv, 4),
        "delta": round(float(getattr(greeks, "delta", 0) or 0), 4) if greeks else None,
        "gamma": round(float(getattr(greeks, "gamma", 0) or 0), 5) if greeks else None,
        "theta": round(float(getattr(greeks, "theta", 0) or 0), 4) if greeks else None,
        "vega": round(float(getattr(greeks, "vega", 0) or 0), 4) if greeks else None,
    }


class ChainService:
    def __init__(self, alpaca: AlpacaService, redis: RedisFacade, market):
        self.alpaca = alpaca
        self.redis = redis
        self.market = market
        self._cache: dict[str, tuple[float, dict]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def get_chain(self, underlying: str, dte_max: int = 3) -> dict:
        key = f"{underlying.upper()}:{dte_max}"
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CHAIN_TTL_S:
            return cached[1]

        # Single flight: concurrent callers await the same fetch.
        if key in self._inflight:
            return await self._inflight[key]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await self._fetch(underlying.upper(), dte_max)
            self._cache[key] = (time.monotonic(), result)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            del self._inflight[key]

    async def _fetch(self, underlying: str, dte_max: int) -> dict:
        if not self.alpaca.configured:
            return self._demo_chain(underlying, dte_max)

        today = datetime.now(timezone.utc).date()
        spot = await self._spot(underlying)
        request = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=today,
            expiration_date_lte=today + timedelta(days=dte_max + 1),
            strike_price_gte=round(spot * 0.94, 2) if spot else None,
            strike_price_lte=round(spot * 1.06, 2) if spot else None,
            feed=self.alpaca.option_feed,
        )
        snapshots = await self.alpaca.call(
            self.alpaca.option_data.get_option_chain, request
        )
        contracts = []
        for occ, snap in snapshots.items():
            row = _contract_row(occ, snap)
            if row:
                contracts.append(row)
        contracts.sort(key=lambda c: (c["expiry"], c["strike"]))
        self._enrich_iv(contracts, spot)
        expirations = sorted({c["expiry"] for c in contracts})
        return {
            "underlying": underlying,
            "spot": spot,
            "asof": int(time.time() * 1000),
            "expirations": expirations,
            "contracts": contracts,
            "demo": False,
        }

    def _enrich_iv(self, contracts: list[dict], spot: float) -> None:
        """IV fallback chain (mirrors the analytics assumptions):
        feed IV -> bisection-solve from quote mid -> strike interpolation.
        Off-hours the indicative feed often omits IV/greeks entirely."""
        from app.services.options_math import (
            TRADING_HOURS_PER_YEAR,
            implied_vol,
            trading_hours_to_expiry,
        )

        now_ms = time.time() * 1000
        taus: dict[str, float] = {}
        for contract in contracts:
            if contract["iv"] > 0:
                contract["iv_source"] = "feed"
                continue
            expiry = contract["expiry"]
            if expiry not in taus:
                taus[expiry] = trading_hours_to_expiry(expiry, now_ms) / TRADING_HOURS_PER_YEAR
            tau = taus[expiry]
            if contract["mid"] > 0 and spot > 0 and tau > 0:
                solved = implied_vol(contract["mid"], spot, contract["strike"], tau, contract["right"])
                if solved is not None and 0.01 < solved < 5.0:
                    contract["iv"] = round(solved, 4)
                    contract["iv_source"] = "solved"

        # Interpolation pass for anything still missing (illiquid wings).
        from collections import defaultdict

        groups: dict[tuple, list[dict]] = defaultdict(list)
        for contract in contracts:
            groups[(contract["expiry"], contract["right"])].append(contract)
        for group in groups.values():
            group.sort(key=lambda c: c["strike"])
            known = [(i, c["iv"]) for i, c in enumerate(group) if c["iv"] > 0]
            if not known:
                continue
            for i, contract in enumerate(group):
                if contract["iv"] > 0:
                    continue
                lower = max((k for k in known if k[0] < i), default=None, key=lambda k: k[0])
                upper = min((k for k in known if k[0] > i), default=None, key=lambda k: k[0])
                if lower and upper:
                    li, liv = lower
                    ui, uiv = upper
                    frac = (group[i]["strike"] - group[li]["strike"]) / (
                        group[ui]["strike"] - group[li]["strike"] or 1
                    )
                    contract["iv"] = round(liv + frac * (uiv - liv), 4)
                else:
                    contract["iv"] = round((lower or upper)[1], 4)
                contract["iv_source"] = "interpolated"

    async def _spot(self, underlying: str) -> float:
        quote = self.market.latest_quote(underlying) or await self.market.fetch_latest_stock_quote(underlying)
        if quote and quote.get("mid"):
            return float(quote["mid"])
        bars = self.market.bars.get_bars(underlying, "1m", limit=1)
        return float(bars[-1]["c"]) if bars else 0.0

    # ----------------------------------------------------------------- demo

    def _demo_chain(self, underlying: str, dte_max: int) -> dict:
        """Synthetic chain so the full designer works keyless/off-hours."""
        bars = self.market.bars.get_bars(underlying, "1m", limit=1)
        spot = float(bars[-1]["c"]) if bars else 450.0
        today = date.today()
        expirations = []
        d = today
        while len(expirations) < min(dte_max + 1, 4):
            if d.weekday() < 5:
                expirations.append(d.isoformat())
            d += timedelta(days=1)

        step = 1.0 if spot < 200 else (2.5 if spot < 600 else 5.0)
        atm = round(spot / step) * step
        strikes = [round(atm + i * step, 2) for i in range(-12, 13)]
        contracts = []
        from app.services.options_math import (
            TRADING_HOURS_PER_YEAR,
            trading_hours_to_expiry,
        )

        now_ms = time.time() * 1000
        for exp_i, expiry in enumerate(expirations):
            # Use the SAME trading-hours clock as the frontend designer.
            # A fake tau here makes demo premiums carry time value the P/L
            # surface says doesn't exist — producing impossible states like
            # "spot already below the stop-loss boundary at entry".
            tau = max(
                trading_hours_to_expiry(expiry, now_ms) / TRADING_HOURS_PER_YEAR, 2e-5
            )
            for strike in strikes:
                moneyness = math.log(strike / spot)
                iv = 0.18 + 0.35 * moneyness**2 + 0.02 * exp_i  # smile
                for right in ("C", "P"):
                    # Rough BS for demo premiums.
                    d1 = (math.log(spot / strike) + 0.5 * iv * iv * tau) / (iv * math.sqrt(tau))
                    d2 = d1 - iv * math.sqrt(tau)
                    ncdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
                    if right == "C":
                        mid = spot * ncdf(d1) - strike * ncdf(d2)
                        delta = ncdf(d1)
                    else:
                        mid = strike * ncdf(-d2) - spot * ncdf(-d1)
                        delta = ncdf(d1) - 1
                    mid = max(round(mid, 2), 0.01)
                    spread = max(0.02, mid * 0.04)
                    exp_compact = expiry.replace("-", "")[2:]
                    occ = f"{underlying}{exp_compact}{right}{int(round(strike * 1000)):08d}"
                    contracts.append(
                        {
                            "symbol": occ,
                            "right": right,
                            "strike": strike,
                            "expiry": expiry,
                            "bid": round(mid - spread / 2, 2),
                            "ask": round(mid + spread / 2, 2),
                            "mid": mid,
                            "iv": round(iv, 4),
                            "delta": round(delta, 4),
                            "gamma": None,
                            "theta": None,
                            "vega": None,
                        }
                    )
        return {
            "underlying": underlying,
            "spot": spot,
            "asof": int(time.time() * 1000),
            "expirations": expirations,
            "contracts": contracts,
            "demo": True,
        }
