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
    # MFT leak-pluggers:
    "max_spread_pct": 0.15,      # per-leg half-spread / mid cap (illiquidity guard)
    "entry_ttl_min": 5,          # unfilled entries auto-cancel (no stale chasing)
    "max_trades_per_day": 20,    # overtrading breaker
    # SL denoising: a breach must PERSIST this long before firing (median-
    # filtered mids; deep breaches >=25% of the TP-SL span fire instantly).
    # 0 disables the dwell — every breach fires immediately.
    "sl_confirm_s": 3.0,
    # Equity (shares) guards — notional-based, no margin formulas:
    "equity_max_notional_per_name_pct": 0.05,  # per trade, % of equity
    "equity_gross_exposure_pct": 0.50,         # all open equity plans
    # Shorting is CAPABLE but gated: flip equity_long_only to false to allow
    # short entries (still subject to the broker's shortable/ETB flags,
    # checked per-symbol at entry). equity_short_overnight stays false until
    # real-time overnight data exists — on the current tier the overnight
    # tape is ~15min delayed, which means delayed stops under UNBOUNDED gap
    # risk on the short side. Exits (covers) are never gated.
    "equity_long_only": True,
    "equity_short_overnight": False,
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
            elif key in ("equity_long_only", "equity_short_overnight"):
                clean[key] = bool(value)
            elif key in ("max_positions", "entry_ttl_min", "max_trades_per_day"):
                iv = int(value)
                bounds = {
                    "max_positions": (1, 20),
                    "entry_ttl_min": (1, 120),
                    "max_trades_per_day": (1, 200),
                }[key]
                if not bounds[0] <= iv <= bounds[1]:
                    raise ValueError(f"{key} must be {bounds[0]}-{bounds[1]}")
                clean[key] = iv
            else:
                fv = float(value)
                limits = {
                    "max_loss_pct": (0.001, 0.10),
                    "daily_loss_pct": (0.005, 0.25),
                    "bp_cap_pct": (0.01, 1.0),
                    "default_tp_pct": (0.05, 10.0),
                    "default_sl_pct": (0.05, 0.95),
                    "max_spread_pct": (0.01, 0.50),
                    "sl_confirm_s": (0.0, 30.0),
                    "equity_max_notional_per_name_pct": (0.005, 0.50),
                    "equity_gross_exposure_pct": (0.01, 1.0),
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

    async def trades_created_today(self) -> int:
        """All plans created this ET session day, regardless of status."""
        now_et = datetime.now(ET)
        start_utc = now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        )
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(TradePlan.created_at >= start_utc)
            )
            return len(list(result.scalars()))

    async def recent_duplicate(
        self, underlying: str, leg_symbols: list[str], window_s: float = 10.0
    ) -> bool:
        """An open plan with identical legs created within the window — almost
        always a double-click or a retry racing its own success."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(
                    TradePlan.underlying == underlying.upper(),
                    TradePlan.status.in_(OPEN_STATUSES),
                    TradePlan.created_at >= cutoff,
                )
            )
            wanted = sorted(leg_symbols)
            for plan in result.scalars():
                if sorted(leg["symbol"] for leg in plan.legs) == wanted:
                    return True
        return False

    async def validate_strategy_budget(
        self,
        *,
        strategy_name: str,
        budget: dict | None,
        intent_notional: float,
        symbol: str,
    ) -> list[str]:
        """Per-strategy budget, layered UNDER the global guards (both must
        pass). Budget lives in strategy_instances.params["budget"]:
        {max_open_plans, max_daily_loss_usd, max_trade_notional_usd,
        max_gross_notional_usd, allowed_symbols}. No budget = only the
        global guards apply. Scoped by the TradePlan.strategy label until
        the strategy_id FK lands."""
        if not budget:
            return []
        violations: list[str] = []

        allowed = budget.get("allowed_symbols")
        if allowed and symbol.upper() not in {s.upper() for s in allowed}:
            violations.append(f"{symbol} not in strategy allowlist {sorted(allowed)}")

        max_trade = budget.get("max_trade_notional_usd")
        if max_trade is not None and intent_notional > float(max_trade) + 0.01:
            violations.append(
                f"trade notional ${intent_notional:.0f} exceeds strategy cap "
                f"${float(max_trade):.0f}"
            )

        async with self.db.session() as session:
            open_plans = list((await session.execute(
                select(TradePlan).where(
                    TradePlan.strategy == strategy_name,
                    TradePlan.status.in_(OPEN_STATUSES),
                )
            )).scalars())

        max_open = budget.get("max_open_plans")
        if max_open is not None and len(open_plans) >= int(max_open):
            violations.append(
                f"strategy has {len(open_plans)} open plans (max {int(max_open)})"
            )

        max_gross = budget.get("max_gross_notional_usd")
        if max_gross is not None:
            gross = sum(
                abs(p.entry_limit) * p.contract_multiplier * p.effective_qty
                for p in open_plans
            )
            if gross + intent_notional > float(max_gross) + 0.01:
                violations.append(
                    f"gross notional ${gross + intent_notional:.0f} would exceed "
                    f"strategy cap ${float(max_gross):.0f}"
                )

        max_daily_loss = budget.get("max_daily_loss_usd")
        if max_daily_loss is not None:
            realized = await self._todays_realized_for(strategy_name)
            if realized <= -abs(float(max_daily_loss)):
                violations.append(
                    f"strategy daily loss breaker tripped (realized {realized:+.0f})"
                )
        return violations

    async def _todays_realized_for(self, strategy_name: str) -> float:
        now_et = datetime.now(ET)
        start_utc = now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        )
        async with self.db.session() as session:
            result = await session.execute(
                select(TradePlan).where(
                    TradePlan.strategy == strategy_name,
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
        legs: list[dict] | None = None,
        underlying: str | None = None,
        stream_age_s: float | None = None,
        daytrade_count: int = 0,
        asset_class: str = "option",
        # Equity-short context, supplied by the caller (which owns broker
        # metadata and session state): None = not checked / not applicable.
        shortable: bool | None = None,
        overnight_session: bool = False,
    ) -> list[str]:
        """Returns a list of violations; empty list = trade allowed."""
        cfg = await self.get_settings()
        violations: list[str] = []
        is_equity = asset_class == "equity"

        # ---- MFT leak-pluggers -------------------------------------------
        # Stale market data: quotes drive the exits; trading blind on stale
        # (or absent) data is how limit fills turn into instant losers.
        if stream_age_s is not None and stream_age_s > 30:
            violations.append(
                "market data stale "
                + ("(no ticks received yet)" if stream_age_s == float("inf")
                   else f"({stream_age_s:.0f}s since last tick)")
            )
        # Illiquidity: wide legs make TP/SL execution unreliable and the
        # round-trip spread eats the edge.
        if legs:
            for leg in legs:
                half_spread = leg.get("half_spread")
                mid = abs(leg.get("entry", 0) or 0)
                if half_spread and mid > 0 and half_spread / mid > cfg["max_spread_pct"]:
                    violations.append(
                        f"{leg.get('symbol', '?')}: half-spread {half_spread / mid:.0%} of mid "
                        f"exceeds {cfg['max_spread_pct']:.0%} cap (illiquid - exits unreliable)"
                    )
        # PDT: a 4th day trade on a sub-$25k account gets the account frozen.
        if account_equity < 25_000 and daytrade_count >= 3:
            violations.append("PDT guard: 4th day trade on a sub-$25k account")
        # Overtrading breaker.
        trades_today = await self.trades_created_today()
        if trades_today >= cfg["max_trades_per_day"]:
            violations.append(
                f"overtrading breaker: {trades_today} trades today "
                f"(max {cfg['max_trades_per_day']})"
            )
        # Double-submit guard.
        if underlying and legs:
            if await self.recent_duplicate(underlying, [l["symbol"] for l in legs]):
                violations.append("duplicate guard: identical order submitted seconds ago")

        if is_equity:
            # Shares: structure + notional guards. No margin formulas, no
            # expiry coupling; leg dicts have no "right" key, so the option
            # structure checks below must not run.
            is_short = any(leg["side"] < 0 for leg in (legs or []))
            if is_short and cfg.get("equity_long_only", True):
                violations.append(
                    "equity shorting is disabled (flip equity_long_only in risk settings)"
                )
            elif is_short:
                # Borrow gate: the broker only shorts easy-to-borrow names.
                # None means the caller could not verify — treat as refusal;
                # trading on an unverified borrow is how shorts bounce at the
                # broker mid-strategy. (SSR needs no code here: a name down
                # >10% takes short LIMIT entries but fills only on upticks —
                # the entry TTL cancels the ones that never do.)
                if shortable is not True:
                    violations.append(
                        "short refused: symbol not confirmed shortable/easy-to-borrow "
                        "at the broker"
                    )
                if overnight_session and not cfg.get("equity_short_overnight", False):
                    violations.append(
                        "short entries disabled in the overnight session: stops run on "
                        "~15min-delayed tape against unbounded gap risk "
                        "(equity_short_overnight)"
                    )
            per_name_cap = account_equity * cfg["equity_max_notional_per_name_pct"]
            if entry_cost_dollars > per_name_cap + 0.01:
                violations.append(
                    f"equity notional ${entry_cost_dollars:.0f} exceeds per-name cap "
                    f"{cfg['equity_max_notional_per_name_pct']:.1%} of equity "
                    f"(${per_name_cap:.0f})"
                )
            open_equity_gross = sum(
                abs(p.entry_limit) * p.effective_qty
                for p in await self.open_plans()
                if p.asset_class == "equity"
            )
            gross_cap = account_equity * cfg["equity_gross_exposure_pct"]
            if open_equity_gross + entry_cost_dollars > gross_cap + 0.01:
                violations.append(
                    f"equity gross exposure ${open_equity_gross + entry_cost_dollars:.0f} "
                    f"would exceed {cfg['equity_gross_exposure_pct']:.0%} of equity "
                    f"(${gross_cap:.0f})"
                )
        else:
            # Uncovered short calls are unplaceable at Alpaca L3 (verified against
            # the paper API: "account not eligible to trade uncovered option
            # contracts") — fail here with a fix hint instead of at the broker.
            naked_calls = max(
                -sum(l["side"] * l.get("ratio", 1) for l in legs if l["right"] == "C"), 0
            )
            if naked_calls > 0:
                violations.append(
                    "uncovered short calls not permitted at this account level - "
                    "add a long call wing to cover"
                )

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

        # Time stop must be in the future. Options additionally pin it inside
        # the session and before the hard cutoff (expiry-day liquidation
        # pressure); equities hold multi-day by design, so no cutoff applies.
        now = utcnow()
        if time_stop_utc <= now:
            violations.append("time stop is in the past")
        if not is_equity:
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
