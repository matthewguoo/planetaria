"""Paper twin: what the LLM-gated decision stream would have done to an
account, under two allocation policies, marked in real time.

The engine's own paper account answers "did the orders that were actually
sent make money". This answers the question underneath it — "is the SIZING
rule earning its complexity" — by replaying the SAME decision stream through
two allocators and letting them compound side by side:

  vol   — the engine's live math (strategies/earnings_reaction._size_position):
          risk_pct of current equity at the stop, so notional = equity *
          risk_pct / sl_eff, then conviction multipliers (confidence,
          quality flags, spent-move). Wilder name -> wider stop -> fewer
          shares -> identical dollars at risk.
  flat  — equal notional per name, flat_pct of current equity, no conviction
          scaling and no vol scaling.

Both allocators see identical entries, identical brackets, and identical
exits. Only position size differs, so the equity-curve gap IS the sizing
rule's contribution — nothing else can leak in.

DERIVED, NEVER STORED. Every number here is recomputed from rows the engine
already writes (strategy_decisions for the signals, the 1m bar cache for the
path, the live quote for the open mark). There is no sim table to drift out
of sync with reality, and no restart can corrupt it — the worst a restart
costs is bar-cache depth, which degrades the path resolution, not the
ledger. Honesty rails:

- STOP-FIRST on same-bar touches (a bar that trades through both brackets
  is scored as the loss), matching research_leadup_account_sim.
- Positions whose symbol has no cached bars stay OPEN and marked at the
  last quote rather than being silently closed at a made-up price.
- Costs are the research harness's convention (10bp AH entry, 3bp RTH
  exit), so a twin curve is directly comparable to the backtests in
  docs/notes/pead_*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

COST_ENTRY_BP = 10.0   # after-hours entry: spread + adverse selection
COST_EXIT_BP = 3.0     # next-session RTH exit
DEFAULT_EQUITY = 100_000.0
DEFAULT_FLAT_PCT = 10.0   # = the vol policy's base case (0.5% risk / 5% stop)


@dataclass
class SimTrade:
    symbol: str
    side: int
    opened_at: str
    entry: float
    qty: int
    notional: float
    sl_pct: float
    tp_pct: float
    time_stop_utc: str | None
    verdict: dict = field(default_factory=dict)
    move_pct: float | None = None
    run5d_pct: float | None = None
    # resolved
    exit: float | None = None
    exit_reason: str | None = None
    exited_at: str | None = None
    pnl: float | None = None
    mark: float | None = None
    unrealized: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def signals_from_decisions(decisions: list[dict]) -> list[dict]:
    """The tradeable decisions, oldest first.

    Note-mode journals `would_trade`; live mode journals `decision` right
    before submitting (earnings_reaction._decide_text). Both carry the same
    shape, so the twin does not care which mode the instance is in — that is
    the point: the shadow instance and the live instance produce comparable
    curves.
    """
    out = []
    for row in sorted(decisions, key=lambda r: r.get("id") or 0):
        detail = row.get("detail") or {}
        body = detail.get("would_trade") or detail.get("decision")
        if not isinstance(body, dict) or not body.get("symbol"):
            continue
        side = body.get("side")
        if side not in (1, -1):
            continue
        out.append({**body, "ts": row.get("ts"), "decision_id": row.get("id"),
                    "strategy_id": row.get("strategy_id")})
    return out


def _size(policy: str, equity: float, signal: dict, sl_pct: float,
          risk_pct: float, flat_pct: float) -> float:
    if policy == "flat":
        return equity * flat_pct / 100.0
    # vol: mirror of earnings_reaction._size_position
    sizing = signal.get("sizing") or {}
    verdict = signal.get("verdict") or {}
    conf = {"high": 1.5, "medium": 1.0, "low": 0.5}.get(
        str(verdict.get("confidence")), 1.0)
    flag = 0.75 if verdict.get("quality_flags") else 1.0
    move = signal.get("move_pct")
    move_mult = 0.5 if move is not None and abs(float(move)) > 6.0 else 1.0
    # Prefer the multipliers the engine actually applied when they were
    # journaled; recompute only for rows that predate the sizing audit.
    conf = sizing.get("conf_mult", conf)
    flag = sizing.get("flag_mult", flag)
    move_mult = sizing.get("move_mult", move_mult)
    return equity * risk_pct / 100.0 / sl_pct * conf * flag * move_mult


def _resolve(trade: SimTrade, bars: list[dict]) -> None:
    """Walk 1m bars from entry, stop-first on same-bar touches. Leaves the
    trade open (marked at the last close) when nothing is hit and the time
    stop has not passed."""
    if not bars:
        return
    opened = _parse_ts(trade.opened_at)
    stop_at = _parse_ts(trade.time_stop_utc)
    tp = trade.entry * (1 + trade.side * trade.tp_pct)
    sl = trade.entry * (1 - trade.side * trade.sl_pct)
    last_close = None
    for bar in bars:
        ts = datetime.fromtimestamp(float(bar["t"]) / 1000, tz=timezone.utc)
        if opened and ts < opened:
            continue
        high, low, close = float(bar["h"]), float(bar["l"]), float(bar["c"])
        last_close = close
        hit_sl = low <= sl if trade.side > 0 else high >= sl
        hit_tp = high >= tp if trade.side > 0 else low <= tp
        if hit_sl:                      # stop-first: same-bar ties are losses
            trade.exit, trade.exit_reason = sl, "sl"
        elif hit_tp:
            trade.exit, trade.exit_reason = tp, "tp"
        elif stop_at and ts >= stop_at:
            trade.exit, trade.exit_reason = close, "time_stop"
        if trade.exit is not None:
            trade.exited_at = ts.isoformat()
            return
    trade.mark = last_close


def _settle(trade: SimTrade) -> None:
    gross_px = trade.exit if trade.exit is not None else trade.mark
    if gross_px is None:
        return
    gross = trade.side * (gross_px - trade.entry) * trade.qty
    costs = trade.notional * (COST_ENTRY_BP + COST_EXIT_BP) / 1e4
    if trade.exit is not None:
        trade.pnl = round(gross - costs, 2)
    else:
        trade.unrealized = round(gross - costs, 2)


def simulate(signals: list[dict], bars_for, mark_for, *, policy: str,
             start_equity: float = DEFAULT_EQUITY, risk_pct: float = 0.5,
             flat_pct: float = DEFAULT_FLAT_PCT) -> dict:
    """One allocator's curve over the decision stream.

    bars_for(symbol) -> 1m bars (oldest first); mark_for(symbol) -> last
    price or None. Both are injected so this is a pure function the tests
    can drive without an engine.
    """
    equity = start_equity
    curve = [{"ts": (signals[0]["ts"] if signals else None), "equity": round(equity, 2)}]
    trades: list[SimTrade] = []
    for signal in signals:
        bracket = signal.get("bracket") or {}
        sl_pct = float(bracket.get("sl_pct") or 0.05)
        tp_pct = float(bracket.get("tp_pct") or 2 * sl_pct)
        entry = float(signal.get("px") or 0)
        if entry <= 0 or sl_pct <= 0:
            continue
        notional = _size(policy, equity, signal, sl_pct, risk_pct, flat_pct)
        qty = int(notional // entry)
        if qty < 1:
            continue
        trade = SimTrade(
            symbol=signal["symbol"], side=int(signal["side"]),
            opened_at=signal.get("ts"), entry=entry, qty=qty,
            notional=round(qty * entry, 2), sl_pct=sl_pct, tp_pct=tp_pct,
            time_stop_utc=(signal.get("intent") or {}).get("time_stop_utc"),
            verdict=signal.get("verdict") or {},
            move_pct=signal.get("move_pct"), run5d_pct=signal.get("run5d_pct"),
        )
        _resolve(trade, bars_for(trade.symbol))
        if trade.exit is None and trade.mark is None:
            trade.mark = mark_for(trade.symbol)
        _settle(trade)
        trades.append(trade)
        if trade.pnl is not None:
            equity += trade.pnl
            curve.append({"ts": trade.exited_at, "equity": round(equity, 2)})

    open_trades = [t for t in trades if t.exit is None]
    closed = [t for t in trades if t.pnl is not None]
    unrealized = sum(t.unrealized or 0.0 for t in open_trades)
    wins = [t.pnl for t in closed if t.pnl > 0]
    curve.append({"ts": datetime.now(timezone.utc).isoformat(),
                  "equity": round(equity + unrealized, 2)})
    peak, max_dd = start_equity, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_dd = max(max_dd, peak - point["equity"])
    return {
        "policy": policy,
        "start_equity": start_equity,
        "equity": round(equity + unrealized, 2),
        "realized": round(equity - start_equity, 2),
        "unrealized": round(unrealized, 2),
        "return_pct": round((equity + unrealized) / start_equity * 100 - 100, 2),
        "max_drawdown": round(max_dd, 2),
        "trades": len(trades),
        "open": len(open_trades),
        "closed": len(closed),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "exposure": round(sum(t.notional for t in open_trades), 2),
        "curve": curve,
        "positions": [t.to_dict() for t in trades][::-1],
    }
