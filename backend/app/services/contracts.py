"""Per-contract detail the chain endpoint does not carry: the contract's
own facts (style, size, open interest — a once-daily figure), the live
snapshot (bid/ask with sizes, last trade, IV, greeks) and today's volume.
Single-flight + TTL caches exactly like ChainService, so a phone polling a
position sheet cannot hammer the REST budget."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("app.contracts")

META_TTL_S = 3600.0
SNAPSHOT_TTL_S = 5.0
VOLUME_TTL_S = 60.0


class ContractsService:
    def __init__(self, alpaca, market):
        self.alpaca = alpaca
        self.market = market
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def _cached(self, key: str, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        if key in self._inflight:
            return await self._inflight[key]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await fetch()
        except Exception as exc:  # noqa: BLE001
            log.warning("contracts %s failed: %s", key, exc)
            value = None
        finally:
            self._inflight.pop(key, None)
        self._cache[key] = (time.monotonic(), value)
        if not fut.done():
            fut.set_result(value)
        return value

    async def contract_meta(self, occ: str) -> dict | None:
        async def fetch():
            c = await self.alpaca.call(self.alpaca.trading.get_option_contract, occ, retries=1)
            oi_date = getattr(c, "open_interest_date", None)
            return {
                "style": str(getattr(getattr(c, "style", None), "value", getattr(c, "style", None)) or "").lower() or None,
                "size": float(getattr(c, "size", 100) or 100),
                "open_interest": int(float(getattr(c, "open_interest", 0) or 0)),
                "open_interest_date": oi_date.isoformat() if hasattr(oi_date, "isoformat") else (str(oi_date) if oi_date else None),
                "tradable": bool(getattr(c, "tradable", True)),
                "close_price": float(c.close_price) if getattr(c, "close_price", None) is not None else None,
                "underlying": str(getattr(c, "underlying_symbol", "") or ""),
            }
        return await self._cached(f"meta:{occ}", META_TTL_S, fetch)

    async def snapshot(self, occ: str) -> dict | None:
        async def fetch():
            from alpaca.data.requests import OptionSnapshotRequest

            res = await self.alpaca.call(
                self.alpaca.option_data.get_option_snapshot,
                OptionSnapshotRequest(symbol_or_symbols=[occ], feed=getattr(self.alpaca, "option_feed", None)),
                retries=1,
            )
            snap = res.get(occ) if isinstance(res, dict) else None
            if snap is None:
                return None
            q = getattr(snap, "latest_quote", None)
            t = getattr(snap, "latest_trade", None)
            g = getattr(snap, "greeks", None)
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            ts = getattr(t, "timestamp", None)
            return {
                "bid": bid, "ask": ask,
                "bid_size": int(getattr(q, "bid_size", 0) or 0), "ask_size": int(getattr(q, "ask_size", 0) or 0),
                "mid": round((bid + ask) / 2, 4) if (bid or ask) else None,
                "last": float(getattr(t, "price", 0) or 0) or None,
                "last_size": int(getattr(t, "size", 0) or 0) or None,
                "last_ts": ts.isoformat() if hasattr(ts, "isoformat") else None,
                "iv": round(float(getattr(snap, "implied_volatility", 0) or 0), 4) or None,
                "delta": round(float(getattr(g, "delta", 0) or 0), 4) if g else None,
                "gamma": round(float(getattr(g, "gamma", 0) or 0), 5) if g else None,
                "theta": round(float(getattr(g, "theta", 0) or 0), 4) if g else None,
                "vega": round(float(getattr(g, "vega", 0) or 0), 4) if g else None,
            }
        return await self._cached(f"snap:{occ}", SNAPSHOT_TTL_S, fetch)

    async def day_volume(self, occ: str) -> int | None:
        async def fetch():
            from alpaca.data.requests import OptionBarsRequest
            from alpaca.data.timeframe import TimeFrame

            today = datetime.now(timezone.utc).date()
            res = await self.alpaca.call(
                self.alpaca.option_data.get_option_bars,
                OptionBarsRequest(symbol_or_symbols=[occ], timeframe=TimeFrame.Day,
                                  start=datetime(today.year, today.month, today.day, tzinfo=timezone.utc)),
                retries=1,
            )
            bars = getattr(res, "data", res)
            rows = bars.get(occ, []) if isinstance(bars, dict) else []
            return int(sum(float(getattr(b, "volume", 0) or 0) for b in rows))
        return await self._cached(f"vol:{occ}", VOLUME_TTL_S, fetch)

    async def detail(self, occ: str) -> dict:
        meta, snap, vol = await asyncio.gather(self.contract_meta(occ), self.snapshot(occ), self.day_volume(occ))
        quote = dict(snap) if snap else {}
        quote["volume"] = vol
        return {"contract": meta, "quote": quote}
