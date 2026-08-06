"""PEAD-continuation backtest: does the initial AH reaction continue to T+1?

The AMC reaction strategy's MECHANICAL core, isolated from the LLM layer:
for each historical after-market-close earnings event —
  anchor   = announce-day 15:55/16:00 close (pre-release),
  reaction = last after-hours print within REACT_MIN minutes after the
             8-K acceptance time (EDGAR, ET), vs the anchor,
  trade    = if |reaction| >= MOVE_GATE_PCT: enter AT the reaction print
             in the reaction's direction; exit next session 15:55 close.
Costs: AH entry pays COST_AH_BP, RTH exit pays COST_RTH_BP.

Null: SIGN-FLIP — the same events, entries, and exits with direction
drawn at random. This isolates the question "does the reaction's
DIRECTION predict the continuation?" from "earnings nights have drift".
(A shuffled-direction null inherits the identical vol profile, so no
matched-horizon sampling is needed.)

Data: cached EDGAR calendars (research_leadup_account_sim windows) +
per-event SIP minute bars (historical minute data is free-tier-fine when
the window ends in the past). Each event costs one bars request; runs are
cached per window so regime reruns are compute-only.

Run: .venv/Scripts/python.exe scripts/research_pead_backtest.py
     --start 2025-02-01 --end 2026-08-01 [--limit N] [--refresh]
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from app.config import get_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = Path(__file__).resolve().parent / "_leadup_cache"

REACT_MIN = 15          # measure the reaction this many minutes post-acceptance
MOVE_GATE_PCT = 1.0     # trade only reactions beyond this
COST_AH_BP = 10.0       # AH entry: spread + adverse selection proxy, per side
COST_RTH_BP = 3.0       # RTH exit
N_BOOT = 500


def event_minutes(client, sym: str, d: date) -> pd.DataFrame | None:
    """Minute bars announce-day 15:30 ET -> next day 16:05 ET."""
    start = datetime(d.year, d.month, d.day, 15, 30, tzinfo=ET)
    end = start + timedelta(days=4)   # weekend-safe; trimmed later
    end = min(end, datetime.now(ET) - timedelta(hours=1))
    try:
        out = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
            start=start, end=end, feed="sip"))
    except Exception:
        return None
    bars = out.data.get(sym)
    if not bars:
        return None
    return pd.DataFrame({
        "ts": [b.timestamp.astimezone(ET) for b in bars],
        "c": [float(b.close) for b in bars],
    })


def build_window_inputs(win_start: date, win_end: date,
                        refresh: bool = False) -> pd.DataFrame:
    """EDGAR calendar + daily OHLC panel for a window, built if absent.

    Both come from research_leadup_account_sim's builders (start-ranked
    universe -> EDGAR 2.02 events -> adjusted dailies), so a new window is
    self-serve instead of requiring the account sim to be run first. The
    OHLC panel starts 21 calendar days early: the LLM harness joins on
    prior-day dollar volume and the 5-session run-up, both of which need
    history BEFORE the window's first event."""
    from research_leadup_account_sim import fetch_calendar_window, fetch_ohlc

    cal = fetch_calendar_window(win_start, win_end, refresh)
    pad_start = win_start - timedelta(days=21)
    symbols = sorted(cal["symbol"].unique())
    fetch_ohlc(symbols, pad_start, win_end + timedelta(days=3), refresh)
    return cal


def run(win_start: date, win_end: date, limit: int | None,
        refresh: bool) -> None:
    cal_path = CACHE / f"edgar_cal_{win_start}_{win_end}.parquet"
    if not cal_path.exists():
        build_window_inputs(win_start, win_end, refresh)
    cal = pd.read_parquet(cal_path)
    # Acceptance TIME comes from the calendar build (hour class only); we
    # re-derive minute-level acceptance from the stored date + hour=amc by
    # measuring from 16:00 ET (acceptances cluster 16:00-16:30; REACT_MIN
    # runs from max(16:05, first AH print)).
    cal = cal[cal["hour"] == "amc"].copy()
    cal["date"] = pd.to_datetime(cal["date"]).dt.date
    if limit:
        cal = cal.sample(n=min(limit, len(cal)), random_state=20260805)
    cache = CACHE / f"pead_events_{win_start}_{win_end}_{len(cal)}.parquet"

    if cache.exists() and not refresh:
        ev = pd.read_parquet(cache)
    else:
        settings = get_settings()
        client = StockHistoricalDataClient(settings.alpaca_api_key,
                                           settings.alpaca_secret_key)
        rows = []
        for n, (_, e) in enumerate(cal.iterrows()):
            bars = event_minutes(client, e["symbol"], e["date"])
            if bars is None or len(bars) < 10:
                continue
            d = e["date"]
            day_mask = bars["ts"].dt.date == d
            pre = bars[day_mask & (bars["ts"].dt.hour < 16)]
            if pre.empty:
                continue
            anchor = float(pre["c"].iloc[-1])
            react_cut = datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET) \
                + timedelta(minutes=REACT_MIN)
            ah = bars[day_mask & (bars["ts"].dt.hour >= 16)
                      & (bars["ts"] <= react_cut)]
            if ah.empty:
                continue
            react_px = float(ah["c"].iloc[-1])
            # The NEXT session only. `bars` spans four calendar days, so
            # filtering on "date > d" and taking iloc[-1] silently exited on
            # the LAST session in the window — T+4 for a Monday reporter,
            # T+2 for a Friday one (found 2026-08-06: PLTR 2026-02-02 exited
            # at 134.68 on 02-06 instead of 157.96 on 02-03). Every result
            # built on a panel cached before that date measures a 2-4 session
            # hold, not the T+1 the docs claim. Rebuild affected panels.
            later = sorted({t for t in bars["ts"].dt.date if t > d})
            if not later:
                continue
            nxt = bars[bars["ts"].dt.date == later[0]]
            nxt_rth = nxt[(nxt["ts"].dt.hour * 60 + nxt["ts"].dt.minute)
                          .between(570, 955)]
            if nxt_rth.empty:
                continue
            exit_px = float(nxt_rth["c"].iloc[-1])
            rows.append({"symbol": e["symbol"], "date": str(d),
                         "anchor": anchor, "react": react_px,
                         "exit": exit_px})
            if n % 100 == 0:
                print(f"  events: {n}/{len(cal)}")
            _time.sleep(0.25)
        ev = pd.DataFrame(rows)
        ev.to_parquet(cache)

    ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
    ev["fwd_bp"] = (ev["exit"] / ev["react"] - 1) * 1e4
    gated = ev[np.abs(ev["move_pct"]) >= MOVE_GATE_PCT].copy()
    gated["dir"] = np.sign(gated["move_pct"])
    costs = COST_AH_BP + COST_RTH_BP
    gated["pnl_bp"] = gated["dir"] * gated["fwd_bp"] - costs

    obs = gated["pnl_bp"].to_numpy()
    fwd = gated["fwd_bp"].to_numpy()
    rng = np.random.default_rng(20260805)
    boots = np.array([
        (rng.choice([-1.0, 1.0], size=len(fwd)) * fwd - costs).mean()
        for _ in range(N_BOOT)])
    mean = obs.mean()
    t = mean / (obs.std(ddof=1) / np.sqrt(len(obs)))
    pct = float((boots < mean).mean() * 100)

    up = gated[gated["dir"] > 0]["pnl_bp"]
    dn = gated[gated["dir"] < 0]["pnl_bp"]
    print(f"\n=== PEAD CONTINUATION {win_start}..{win_end} "
          f"(|reaction|>={MOVE_GATE_PCT}%, T+{REACT_MIN}m entry, "
          f"T+1 15:55 exit, costs {costs:.0f}bp) ===")
    print(f"events with AH tape: {len(ev)} | gated trades: {len(gated)} "
          f"({len(up)} long / {len(dn)} short)")
    print(f"mean pnl: {mean:+.1f}bp/trade | t={t:.2f} | win "
          f"{(obs > 0).mean() * 100:.1f}%")
    print(f"long-side: {up.mean():+.1f}bp | short-side: {dn.mean():+.1f}bp")
    print(f"sign-flip null: mean {boots.mean():+.1f}bp, p95 "
          f"{np.percentile(boots, 95):+.1f}bp -> strategy at {pct:.1f}th pctile")

    stamp = datetime.now(ET).strftime("%Y%m%d")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"pead_backtest_{stamp}_{win_start}.md")
    doc.write_text(
        f"# PEAD continuation — {win_start}..{win_end} — run {stamp}\n\n"
        f"Method: script docstring. events={len(ev)} gated={len(gated)} "
        f"mean={mean:+.1f}bp t={t:.2f} null_pctile={pct:.1f} "
        f"long={up.mean():+.1f}bp short={dn.mean():+.1f}bp\n",
        encoding="utf-8")
    print(f"wrote {doc}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2025-02-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    run(date.fromisoformat(args.start), date.fromisoformat(args.end),
        args.limit, args.refresh)
