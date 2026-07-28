"""Risk settings + server-side order validation. The server is the enforcer;
client-side numbers are advisory display only.
"""

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.trade import OPEN_STATUSES, AppSetting, TradePlan, utcnow

log = logging.getLogger("app.risk")

ET = ZoneInfo("America/New_York")

DEFAULT_RISK = {
    "max_loss_pct": 0.02,        # per trade, % of equity
    "daily_loss_pct": 0.06,      # circuit breaker
    "max_positions": 3,
    "bp_cap_pct": 0.25,
    "default_tp_pct": 1.00,      # +100% of entry premium
    "default_sl_pct": 0.50,      # -50% of entry premium
    "time_stop_et": "15:50",     # non-expiry-day default
    "expiry_time_stop_et": "15:15",  # Alpaca force-liquidates expiring pos ~15:30
}

RISK_KEY = "risk"


class RiskService:
    def __init__(self, db):
        self.db = db

    async def get_settings(self) -> dict:
        async with self.db.session() as session:
            row = await session.get(AppSetting, RISK_KEY)
            merged = dict(DEFAULT_RISK)
            if row:
                merged.update(row.value)
            return merged

    async def update_settings(self, patch: dict) -> dict:
        clean: dict = {}
        for key, value in patch.items():
            if key not in DEFAULT_RISK:
                raise ValueError(f"unknown setting {key!r}")
            if key in ("time_stop_et", "expiry_time_stop_et"):
                time.fromisoformat(str(value))  # validate HH:MM
                clean[key] = str(value)
            elif key == "max_positions":
                iv = int(value)
                if not 1 <= iv <= 20:
                    raise ValueError("max_positions must be 1-20")
                clean[key] = iv
            else:
                fv = float(value)
                limits = {
                    "max_loss_pct": (0.001, 0.10),
                    "daily_loss_pct": (0.005, 0.25),
                    "bp_cap_pct": (0.01, 1.0),
                    "default_tp_pct": (0.05, 10.0),
                    "default_sl_pct": (0.05, 0.95),
                }[key]
                if not limits[0] <= fv <= limits[1]:
                    raise ValueError(f"{key} out of bounds {limits}")
                clean[key] = fv
        async with self.db.session() as session:
            row = await session.get(AppSetting, RISK_KEY)
            if row is None:
                merged_value = {**clean}
                session.add(AppSetting(key=RISK_KEY, value=merged_value))
            else:
                row.value = {**row.value, **clean}
            await session.commit()
        return await self.get_settings()

    # ------------------------------------------------------------ guards

    async def open_plans(self) -> list[TradePlan]:
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(TradePlan.status.in_(OPEN_STATUSES))
            )
            return list(result.scalars())

    async def todays_realized_pnl(self) -> float:
        """Realized P/L for plans closed today (ET session day)."""
        now_et = datetime.now(ET)
        session_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = session_start_et.astimezone(timezone.utc)
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(
                    TradePlan.status == "closed",
                    TradePlan.updated_at >= start_utc,
                )
            )
            return sum(plan.realized_pnl or 0.0 for plan in result.scalars())

    async def validate_new_trade(
        self,
        *,
        account_equity: float,
        entry_cost_dollars: float,
        max_loss_dollars: float,
        time_stop_utc: datetime,
        expiry_date_et: str,
    ) -> list[str]:
        """Returns a list of violations; empty list = trade allowed."""
        cfg = await self.get_settings()
        violations: list[str] = []

        if max_loss_dollars > account_equity * cfg["max_loss_pct"] + 0.01:
            violations.append(
                f"max loss ${max_loss_dollars:.0f} exceeds "
                f"{cfg['max_loss_pct']:.1%} of equity (${account_equity * cfg['max_loss_pct']:.0f})"
            )
        if entry_cost_dollars > account_equity * cfg["bp_cap_pct"] + 0.01:
            violations.append(
                f"cost ${entry_cost_dollars:.0f} exceeds BP cap {cfg['bp_cap_pct']:.0%}"
            )

        open_count = len(await self.open_plans())
        if open_count >= cfg["max_positions"]:
            violations.append(f"max concurrent positions ({cfg['max_positions']}) reached")

        realized = await self.todays_realized_pnl()
        if realized <= -account_equity * cfg["daily_loss_pct"]:
            violations.append(
                f"daily circuit breaker tripped (realized {realized:+.0f} today)"
            )

        # Time stop must be inside today's session and before the hard cutoff.
        now = utcnow()
        if time_stop_utc <= now:
            violations.append("time stop is in the past")
        stop_et = time_stop_utc.astimezone(ET)
        is_expiry_day = stop_et.strftime("%Y-%m-%d") == expiry_date_et
        cutoff_str = cfg["expiry_time_stop_et"] if is_expiry_day else cfg["time_stop_et"]
        cutoff = time.fromisoformat(cutoff_str)
        if stop_et.time() > cutoff:
            violations.append(
                f"time stop {stop_et.strftime('%H:%M')} ET after cutoff {cutoff_str} ET"
                + (" (expiry day)" if is_expiry_day else "")
            )
        return violations
