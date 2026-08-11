"""Ladder state: the pre-registration band vs the instance's own record.

Pure functions over TradePlan rows and decision timestamps — no DB, no
broker — so the math is unit-testable and the runner stays thin. The
consumer is GET /api/strategies/{id}/performance, which renders the
pre-reg ladder as a progress bar (docs/briefs/fund-capital-scheduling.md
§7).

Deliberately NOT computed here: a live Sharpe. At ladder sample sizes a
Sharpe is noise with a confidence interval wider than itself, and an early
scary/euphoric one beside a frozen backtest number is goal-pressure by UI.
The registered metric (per-trade / per-day bp) is the yardstick both
pre-regs actually name.

Known coarseness, accepted for v1: `sessions_observed` counts every ET
date with journal or plan activity since the instance existed — it does
not reset when the instance climbs a ladder stage (live-flip dates are not
stored). The operator judges stage boundaries; the counter shows sample
size.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.trade import as_utc

ET = ZoneInfo("America/New_York")


def _et_date(dt: datetime) -> str:
    # as_utc: SQLite round-trips DateTime(timezone=True) offset-naive.
    return as_utc(dt).astimezone(ET).date().isoformat()


def _trade_bp(plan) -> float | None:
    """One closed plan's realized net return, in bp of its entry notional."""
    if plan.realized_pnl is None:
        return None
    entry = plan.fill_premium if plan.fill_premium is not None else plan.entry_limit
    notional = abs(entry or 0.0) * (plan.effective_qty or 0) * plan.contract_multiplier
    if notional <= 0:
        return None
    return plan.realized_pnl / notional * 1e4


def _short_strike(legs: list | None) -> float | None:
    """The fly's ATM anchor: the strike its short legs sit on."""
    for leg in legs or []:
        if (leg.get("side") or 0) < 0 and leg.get("strike"):
            return float(leg["strike"])
    return None


def _per_day_underlying_bp(closed: list) -> list[float]:
    """The fly pre-reg's metric: each traded day's P&L in bp of the
    underlying notional (short strike x 100 per set) — the same basis the
    study's +bp-of-S/day numbers use. Days without a priced anchor are
    dropped rather than guessed."""
    days: dict[str, list] = {}
    for plan in closed:
        days.setdefault(_et_date(plan.created_at), []).append(plan)
    out: list[float] = []
    for plans in days.values():
        pnl = 0.0
        denom = 0.0
        for plan in plans:
            strike = _short_strike(plan.legs)
            if strike is None:
                continue
            denom += strike * 100.0 * (plan.effective_qty or 0)
            pnl += plan.realized_pnl or 0.0
        if denom > 0:
            out.append(pnl / denom * 1e4)
    return out


def ladder_state(registered: dict | None, params: dict, plans: list,
                 decision_ts: list[datetime]) -> dict | None:
    """The band-vs-live card's numbers. None for unregistered kinds — the
    console shows UNREGISTERED instead of inventing a yardstick."""
    if not registered:
        return None
    closed = [p for p in plans if p.realized_pnl is not None]
    sessions = {_et_date(ts) for ts in decision_ts if ts is not None}
    sessions.update(_et_date(p.created_at) for p in plans if p.created_at)

    metric = registered.get("metric")
    if metric == "bp_of_underlying_per_day":
        values = _per_day_underlying_bp(closed)
    elif metric == "net_bp_per_trade":
        values = [bp for p in closed if (bp := _trade_bp(p)) is not None]
    else:
        # Registered on a yardstick the engine does not measure yet (the
        # nosip drift hit rate needs verdict-vs-next-session joins). The
        # card must say "not yet measured" — never fall back to a metric
        # the pre-reg did not name.
        values = []
    metric_computed = metric in ("bp_of_underlying_per_day", "net_bp_per_trade")
    running = sum(values) / len(values) if values else None

    band = registered.get("band") or [None, None]
    if running is None or band[0] is None:
        band_status = None
    elif running < band[0]:
        band_status = "below"
    elif running > band[1]:
        band_status = "above"
    else:
        band_status = "in"

    live = bool(params.get("live"))
    ladder = registered.get("ladder") or []
    stage_idx = 1 if live else 0
    stage = ladder[stage_idx] if stage_idx < len(ladder) else {}
    return {
        "stage": stage.get("stage") or ("live" if live else "note-mode"),
        "target_sessions": stage.get("sessions"),
        "sessions_observed": len(sessions),
        "trades_closed": len(closed),
        "samples": len(values),
        "metric": metric,
        "metric_computed": metric_computed,
        "metric_label": registered.get("metric_label"),
        "running_metric": round(running, 2) if running is not None else None,
        "band": registered.get("band"),
        "band_status": band_status,
    }
