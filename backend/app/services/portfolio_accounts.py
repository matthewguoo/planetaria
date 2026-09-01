"""Cross-account PORTFOLIO view: read-only aggregation over EVERY paper
account registered in .env (AccountService.registry), not just the one the
engine trades. Throwaway REST clients per refresh — keys never persist
anywhere new, nothing here can place an order (TradingClient is used for
account/positions/history reads only).

Engine-side context joins from our own DB: plans are stamped with the
account name they were placed on, so each row also carries the engine's
closed-plan count and realized P/L for that account.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.models.trade import TradePlan

log = logging.getLogger("app.portfolio_accounts")

CACHE_TTL_S = 30.0


def _fetch_account_sync(name: str, api_key: str, secret_key: str,
                        period: str, timeframe: str) -> dict:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    client = TradingClient(api_key, secret_key, paper=True)
    acct = client.get_account()
    positions = client.get_all_positions()
    try:
        hist = client.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        )
        history = {
            "timestamps": list(hist.timestamp or []),
            "equity": [float(v) if v is not None else None for v in (hist.equity or [])],
        }
    except Exception as exc:  # history is decoration; the row still stands
        log.warning("portfolio history failed for %r: %s", name, exc)
        history = {"timestamps": [], "equity": []}

    equity = float(acct.equity)
    last_equity = float(acct.last_equity) if acct.last_equity is not None else equity
    unrealized = sum(
        float(p.unrealized_pl) for p in positions if p.unrealized_pl is not None
    )
    return {
        "name": name,
        "status": str(acct.status),
        "equity": equity,
        "cash": float(acct.cash) if acct.cash is not None else None,
        "buying_power": float(acct.buying_power) if acct.buying_power is not None else None,
        "day_pl": equity - last_equity,
        "positions": len(positions),
        "unrealized_pl": unrealized,
        "history": history,
    }


class PortfolioAccounts:
    def __init__(self, accounts, db):
        self.accounts = accounts  # AccountService (registry + applied name)
        self.db = db
        self._cache: tuple[float, str, dict] | None = None
        self._lock = asyncio.Lock()

    async def _engine_stats(self) -> dict[str, dict]:
        """Per-account engine history from OUR plans (stamped account name).
        Pre-stamping plans (account NULL) land under 'default'."""
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(TradePlan.status == "closed")
            )
            out: dict[str, dict] = {}
            for plan in result.scalars():
                name = plan.account or "default"
                row = out.setdefault(name, {"plans_closed": 0, "realized_pnl": 0.0})
                row["plans_closed"] += 1
                row["realized_pnl"] += plan.realized_pnl or 0.0
            for row in out.values():
                row["realized_pnl"] = round(row["realized_pnl"], 2)
            return out

    async def snapshot(self, period: str = "1M", timeframe: str = "1D") -> dict:
        cache_key = f"{period}:{timeframe}"
        async with self._lock:
            if self._cache is not None:
                ts, key, snap = self._cache
                if key == cache_key and time.monotonic() - ts < CACHE_TTL_S:
                    return snap

            registry = self.accounts.registry()
            active = self.accounts.applied_name

            async def fetch(name: str, cfg: dict) -> dict:
                try:
                    row = await asyncio.to_thread(
                        _fetch_account_sync, name, cfg["api_key"],
                        cfg["secret_key"], period, timeframe,
                    )
                except Exception as exc:
                    log.warning("portfolio fetch failed for %r: %s", name, exc)
                    row = {"name": name, "error": str(exc)}
                row["active"] = name == active
                return row

            rows = await asyncio.gather(
                *(fetch(name, cfg) for name, cfg in sorted(registry.items()))
            )
            stats = await self._engine_stats()
            for row in rows:
                row.update(stats.get(row["name"], {"plans_closed": 0, "realized_pnl": 0.0}))

            ok = [r for r in rows if "error" not in r]
            snap = {
                "asof": time.time(),
                "accounts": list(rows),
                "totals": {
                    "equity": round(sum(r["equity"] for r in ok), 2),
                    "day_pl": round(sum(r["day_pl"] for r in ok), 2),
                    "unrealized_pl": round(sum(r["unrealized_pl"] for r in ok), 2),
                    "positions": sum(r["positions"] for r in ok),
                    "accounts": len(rows),
                    "errors": len(rows) - len(ok),
                },
            }
            self._cache = (time.monotonic(), cache_key, snap)
            return snap
