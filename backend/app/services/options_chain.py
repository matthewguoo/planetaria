"""Options chain service: 0-3 DTE window, greeks/IV included (free indicative
feed), 5s TTL cache with single-flight coalescing so strike drags and multiple
clients never hammer the 200 req/min REST budget.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import OptionChainRequest

from app.services.alpaca import AlpacaService
from app.services.redis_client import RedisFacade
from app.services.trade_service import parse_occ_symbol


def weekdays_ahead(day, n: int):
    """The date `n` weekdays after `day` - the far edge of a "0-n DTE"
    chain window. Counted in weekdays, not calendar days, so a Friday still
    sees next week's expiries: with calendar days a 3-DTE window from a
    Friday held only that day's 0DTE, and across a long weekend nothing
    else at all. Exchange holidays are not skipped (they only widen the
    window by a day, never narrow it)."""
    out = day
    left = max(int(n), 0)
    while left > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out

log = logging.getLogger("app.chain")

CHAIN_TTL_S = 5.0


def _contract_row(occ_symbol: str, snap) -> dict | None:
    """Flatten an alpaca OptionsSnapshot into our wire format."""
    occ = parse_occ_symbol(occ_symbol)
    if occ is None:
        return None

    quote = getattr(snap, "latest_quote", None)
    greeks = getattr(snap, "greeks", None)
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    mid = round((bid + ask) / 2, 4) if (bid or ask) else 0.0
    iv = float(getattr(snap, "implied_volatility", 0) or 0)
    return {
        "symbol": occ_symbol,
        "right": occ["right"],
        "strike": occ["strike"],
        "expiry": occ["expiry"],
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
            # No synthetic fallback (demo feed removed 2026-08-05): a chain
            # that LOOKS tradeable but prices nothing real is a staleness
            # trap. Keyless mode gets an honest error instead.
            raise ValueError("options chain requires Alpaca keys (no keyless fallback)")

        today = datetime.now(timezone.utc).date()
        spot = await self._spot(underlying)
        request = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=today,
            expiration_date_lte=weekdays_ahead(today, dte_max),
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
        """IV priority: solve-from-mid (OUR convention) -> feed -> interpolate.

        The solve is FIRST, not a fallback: every consumer prices with our
        trading-time BS convention, so the invariant that matters is
        bs_price(spot, K, tau_ours, iv) == mid. Alpaca's feed IV lives in a
        different convention (calendar time, their spot, their model) and can
        sit 2-3x away from what our pricer needs — off-hours we observed a
        1DTE call carrying feed IV 26.5% against a mid implying 11%, making
        positions appear instantly worth 3x entry. Feed IV survives only where
        no usable mid exists (empty/crossed book)."""
        from app.services.options_math import (
            TRADING_HOURS_PER_YEAR,
            implied_vol,
            trading_hours_to_expiry,
        )

        now_ms = time.time() * 1000
        taus: dict[str, float] = {}
        for contract in contracts:
            feed_iv = contract["iv"]
            expiry = contract["expiry"]
            if expiry not in taus:
                taus[expiry] = trading_hours_to_expiry(expiry, now_ms) / TRADING_HOURS_PER_YEAR
            tau = taus[expiry]
            solved = None
            if contract["mid"] > 0 and spot > 0 and tau > 0:
                solved = implied_vol(contract["mid"], spot, contract["strike"], tau, contract["right"])
            if solved is not None and 0.01 < solved < 5.0:
                contract["iv"] = round(solved, 4)
                contract["iv_source"] = "solved"
            elif feed_iv > 0:
                contract["iv_source"] = "feed"

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
        # Refresh a stale cached quote (throttled inside), then take the
        # freshest of quote-vs-tape — a frozen overnight quote must not
        # anchor the chain 2 points off the printing premarket bars.
        await self.market.fetch_latest_stock_quote(underlying)
        return self.market.spot(underlying)

