"""Astrology check: do mechanical iFVG and PO3 (ICT) entries beat random?

The honest problem with ICT concepts is that as taught they are
discretionary enough to be unfalsifiable — every loser can be explained as
"wrong context". This script tests ONE precise, mechanical reading of each
on real SIP minute bars, against the only null that matters: RANDOM entries
run through the IDENTICAL exit engine (same bracket, same time stop, same
costs). If the pattern's mean $/trade doesn't clear the random distribution,
the pattern is decoration on top of the exit engine and market drift.

Definitions tested (deliberately simple, stated so they can be attacked):

iFVG (inverse fair value gap), per timeframe, RTH bars only:
  - Bullish FVG at bar i: low[i] > high[i-2]; zone = [high[i-2], low[i]].
    Bearish mirrored. Zones live max ZONE_AGE bars.
  - INVERSION: a close fully through the zone's far edge flips its role
    (violated bullish FVG -> resistance; violated bearish FVG -> support).
  - ENTRY: first RETEST of the inverted zone within RETEST_AGE bars —
    price tags the near edge from the far side -> fade back through the
    zone is the bet. Stop beyond the zone's far edge + 10% zone padding;
    TP at 2R; time stop TIME_STOP bars; force-flat at session close
    (no overnight holds).

PO3 (power-of-three session model: accumulate -> manipulate -> distribute):
  - Accumulation range: prior 18:00 -> 04:00 ET high/low (extended-hours
    1m bars, aggregated independently of trading timeframe).
  - MANIPULATION: first excursion beyond the range boundary during
    04:00-10:30 ET that closes back INSIDE within RECLAIM_BARS bars of the
    trading timeframe ("sweep and reclaim").
  - ENTRY on the reclaim close, direction away from the sweep (sweep of
    the low -> long). Stop at the sweep extreme; TP 2R; flat 15:55 ET.
  - Max one trade per symbol per day (the first valid signal).

Costs: per side, half-spread + slippage (SPY/QQQ 2c, AMD/TSLA 5c).

Null: N_BOOT bootstrap runs of the same trade COUNT with uniformly random
RTH entries (random direction), identical exit engine. Report the actual
mean $/share/trade's percentile vs that distribution + a plain t-stat.

Run:  .venv/Scripts/python.exe scripts/research_ict_backtest.py
      [--refresh] [--symbols SPY,QQQ,AMD,TSLA] [--start 2025-02-01]
Bars cache to scratch parquet; --refresh refetches.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
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
CACHE_DIR = Path(__file__).resolve().parent / "_ict_cache"

ZONE_AGE = 200        # bars an FVG stays tracked
RETEST_AGE = 100      # bars after inversion in which a retest can trigger
TIME_STOP = 100       # bars; iFVG max hold
RECLAIM_BARS = 3      # PO3: sweep must close back inside within this many bars
TP_R = 2.0            # both strategies: 2R bracket target
N_BOOT = 200
COSTS = {"SPY": 0.02, "QQQ": 0.02, "AMD": 0.05, "TSLA": 0.05}  # $/share/side


# ------------------------------------------------------------------ data

def fetch_bars(symbol: str, start: str, end: str, refresh: bool) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_{start}_{end}_1m.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    settings = get_settings()
    client = StockHistoricalDataClient(settings.alpaca_api_key,
                                       settings.alpaca_secret_key)
    print(f"  fetching {symbol} 1m {start}..{end} (SIP, extended hours)...")
    t0 = _time.monotonic()
    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
        start=datetime.fromisoformat(start).replace(tzinfo=ET),
        end=datetime.fromisoformat(end).replace(tzinfo=ET),
        feed="sip", limit=None,
    )
    out = client.get_stock_bars(req)
    rows = [{"ts": b.timestamp, "o": float(b.open), "h": float(b.high),
             "l": float(b.low), "c": float(b.close), "v": float(b.volume)}
            for b in out.data.get(symbol, [])]
    df = pd.DataFrame(rows).set_index("ts")
    df.index = pd.DatetimeIndex(df.index).tz_convert(ET)
    df.to_parquet(cache)
    print(f"    {len(df)} bars in {_time.monotonic() - t0:.0f}s")
    return df


def resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 1:
        return df1m
    return (df1m.resample(f"{minutes}min", label="left", closed="left")
            .agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
            .dropna())


def rth_mask(index: pd.DatetimeIndex) -> np.ndarray:
    mins = index.hour * 60 + index.minute
    return np.asarray((index.weekday < 5) & (mins >= 570) & (mins < 960))


# ----------------------------------------------------------- exit engine

def run_exit(h: np.ndarray, l: np.ndarray, c: np.ndarray, day: np.ndarray,
             i: int, side: int, entry: float, stop: float, tp: float,
             max_hold: int) -> tuple[float, int]:
    """Walk forward from entry bar i+1: first touch of stop/tp wins (stop
    wins ties, conservative), else time stop, else last bar of the entry's
    session. Returns ($/share result BEFORE costs, exit bar index)."""
    n = len(c)
    end = min(i + 1 + max_hold, n)
    for j in range(i + 1, end):
        if day[j] != day[i]:               # never hold overnight
            return side * (c[j - 1] - entry), j - 1
        if side > 0:
            if l[j] <= stop:
                return stop - entry, j
            if h[j] >= tp:
                return tp - entry, j
        else:
            if h[j] >= stop:
                return entry - stop, j
            if l[j] <= tp:
                return entry - tp, j
    j = min(end, n) - 1
    return side * (c[j] - entry), j


# ------------------------------------------------------------- strategies

def ifvg_trades(df: pd.DataFrame) -> list[dict]:
    """Mechanical iFVG per module docstring. RTH bars only."""
    rth = df[rth_mask(df.index)]
    h, l, c = rth["h"].to_numpy(), rth["l"].to_numpy(), rth["c"].to_numpy()
    day = rth.index.date
    n = len(rth)
    zones: list[dict] = []   # {lo, hi, dir, born, inverted_at}
    trades = []
    for i in range(2, n):
        # expire old zones
        zones = [z for z in zones
                 if i - z["born"] < ZONE_AGE
                 and (z["inverted_at"] is None or i - z["inverted_at"] < RETEST_AGE)]
        # new FVGs (same session only)
        if day[i] == day[i - 2]:
            if l[i] > h[i - 2]:
                zones.append({"lo": h[i - 2], "hi": l[i], "dir": 1,
                              "born": i, "inverted_at": None})
            elif h[i] < l[i - 2]:
                zones.append({"lo": h[i], "hi": l[i - 2], "dir": -1,
                              "born": i, "inverted_at": None})
        still: list[dict] = []
        for z in zones:
            if z["inverted_at"] is None:
                # inversion = close fully through the far edge
                if z["dir"] == 1 and c[i] < z["lo"]:
                    z["inverted_at"] = i
                elif z["dir"] == -1 and c[i] > z["hi"]:
                    z["inverted_at"] = i
                still.append(z)
                continue
            # inverted: wait for the retest of the near edge
            if z["dir"] == 1:      # violated bullish FVG -> resistance; short the tag
                if h[i] >= z["lo"]:
                    entry = z["lo"]
                    pad = 0.1 * (z["hi"] - z["lo"])
                    stop = z["hi"] + pad
                    tp = entry - TP_R * (stop - entry)
                    res, j = run_exit(h, l, c, day, i, -1, entry, stop, tp, TIME_STOP)
                    trades.append({"i": i, "j": j, "side": -1, "res": res,
                                   "r": stop - entry})
                    continue       # zone consumed
            else:                  # violated bearish FVG -> support; buy the tag
                if l[i] <= z["hi"]:
                    entry = z["hi"]
                    pad = 0.1 * (z["hi"] - z["lo"])
                    stop = z["lo"] - pad
                    tp = entry + TP_R * (entry - stop)
                    res, j = run_exit(h, l, c, day, i, 1, entry, stop, tp, TIME_STOP)
                    trades.append({"i": i, "j": j, "side": 1, "res": res,
                                   "r": entry - stop})
                    continue
            still.append(z)
        zones = still
    return trades


def po3_trades(df_tf: pd.DataFrame, df1m: pd.DataFrame) -> list[dict]:
    """Sweep-and-reclaim of the 18:00->04:00 ET range, entries on df_tf."""
    on = df1m.between_time("18:00", "03:59")
    # label each overnight range by the SESSION DATE it precedes (18:00+ ->
    # next trading day)
    sess = pd.Series(on.index.date, index=on.index)
    sess[on.index.hour >= 18] = (
        pd.Series(on.index[on.index.hour >= 18]) + pd.Timedelta(days=1)
    ).dt.date.values
    ranges = on.groupby(sess.values).agg(rhi=("h", "max"), rlo=("l", "min"))

    h, l, c = df_tf["h"].to_numpy(), df_tf["l"].to_numpy(), df_tf["c"].to_numpy()
    day = df_tf.index.date
    mins = df_tf.index.hour * 60 + df_tf.index.minute
    trades = []
    n = len(df_tf)
    traded_days: set = set()
    sweep: dict | None = None
    for i in range(n):
        d = day[i]
        if d in traded_days:
            continue
        if not (240 <= mins[i] < 630):     # manipulation window 04:00-10:30
            sweep = None
            continue
        if d not in ranges.index:
            continue
        rhi, rlo = ranges.loc[d, "rhi"], ranges.loc[d, "rlo"]
        if sweep is not None and sweep["day"] != d:
            sweep = None
        if sweep is None:
            if h[i] > rhi and c[i] > rhi:
                sweep = {"day": d, "dir": -1, "extreme": h[i], "since": i}
            elif l[i] < rlo and c[i] < rlo:
                sweep = {"day": d, "dir": 1, "extreme": l[i], "since": i}
            continue
        # a sweep is armed: track extreme, wait for reclaim close inside
        sweep["extreme"] = max(sweep["extreme"], h[i]) if sweep["dir"] == -1 \
            else min(sweep["extreme"], l[i])
        if i - sweep["since"] > RECLAIM_BARS:
            sweep = None
            continue
        if sweep["dir"] == -1 and c[i] < rhi:          # reclaimed under the high
            entry, stop = c[i], sweep["extreme"]
            tp = entry - TP_R * (stop - entry)
            res, j = run_exit(h, l, c, day, i, -1, entry, stop, tp, 10_000)
            trades.append({"i": i, "j": j, "side": -1, "res": res,
                           "r": stop - entry})
            traded_days.add(d)
            sweep = None
        elif sweep["dir"] == 1 and c[i] > rlo:         # reclaimed over the low
            entry, stop = c[i], sweep["extreme"]
            tp = entry + TP_R * (entry - stop)
            res, j = run_exit(h, l, c, day, i, 1, entry, stop, tp, 10_000)
            trades.append({"i": i, "j": j, "side": 1, "res": res,
                           "r": entry - stop})
            traded_days.add(d)
            sweep = None
    return trades


# ------------------------------------------------------------------ null

def random_null(df: pd.DataFrame, n_trades: int, avg_r: float, cost: float,
                rng: np.random.Generator, runs: int = N_BOOT) -> np.ndarray:
    """Mean net $/share/trade for `runs` random-entry portfolios pushed
    through the same exit engine: random RTH bar, random direction, stop at
    avg_r distance, 2R target, same time stop."""
    rth = df[rth_mask(df.index)]
    h, l, c = rth["h"].to_numpy(), rth["l"].to_numpy(), rth["c"].to_numpy()
    day = rth.index.date
    n = len(rth)
    # The null's MEAN per trade converges long before tens of thousands of
    # samples; cap the per-run portfolio so 1m configs stay tractable.
    n_trades = min(n_trades, 2000)
    means = np.empty(runs)
    for k in range(runs):
        idx = rng.integers(2, n - 2, size=n_trades)
        sides = rng.choice([1, -1], size=n_trades)
        tot = 0.0
        for i, s in zip(idx, sides):
            entry = c[i]
            stop = entry - s * avg_r
            tp = entry + s * TP_R * avg_r
            res, _ = run_exit(h, l, c, day, i, int(s), entry, stop, tp, TIME_STOP)
            tot += res - 2 * cost
        means[k] = tot / n_trades
    return means


# ---------------------------------------------------------------- report

def evaluate(name: str, symbol: str, tf: int, trades: list[dict],
             df: pd.DataFrame, cost: float, rng) -> dict | None:
    if len(trades) < 10:
        return {"strategy": name, "symbol": symbol, "tf": tf,
                "trades": len(trades), "note": "too few trades"}
    net = np.array([t["res"] for t in trades]) - 2 * cost
    rs = np.array([max(t["r"], 1e-9) for t in trades])
    r_mult = net / rs
    wins = net > 0
    gross_w = net[wins].sum()
    gross_l = -net[~wins].sum()
    mean = net.mean()
    t_stat = mean / (net.std(ddof=1) / np.sqrt(len(net))) if len(net) > 1 else 0.0
    null = random_null(df, len(trades), float(rs.mean()), cost, rng)
    pct = float((null < mean).mean() * 100)
    return {
        "strategy": name, "symbol": symbol, "tf": tf, "trades": len(trades),
        "win_pct": round(float(wins.mean() * 100), 1),
        "avg_R": round(float(r_mult.mean()), 3),
        "pf": round(float(gross_w / gross_l), 2) if gross_l > 0 else float("inf"),
        "mean_$": round(float(mean), 4),
        "t": round(float(t_stat), 2),
        "null_pct": round(pct, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="SPY,QQQ,AMD,TSLA")
    ap.add_argument("--start", default="2025-02-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    rng = np.random.default_rng(20260805)
    rows = []
    for symbol in args.symbols.split(","):
        symbol = symbol.strip().upper()
        df1m = fetch_bars(symbol, args.start, args.end, args.refresh)
        cost = COSTS.get(symbol, 0.05)
        for tf in (1, 5, 15):
            df = resample(df1m, tf)
            t0 = _time.monotonic()
            for name, trades in (
                ("iFVG", ifvg_trades(df)),
                ("PO3", po3_trades(df, df1m)),
            ):
                row = evaluate(name, symbol, tf, trades, df, cost, rng)
                rows.append(row)
                print(f"  {symbol} {tf}m {name}: {row} "
                      f"({_time.monotonic() - t0:.0f}s)")

    out = pd.DataFrame(rows)
    print("\n=== SUMMARY (net of costs; null_pct = percentile vs random "
          "entries with the SAME exit engine) ===")
    print(out.to_string(index=False))
    stamp = datetime.now(ET).strftime("%Y%m%d")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"ict_backtest_{stamp}.md")
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        f"# iFVG / PO3 mechanical backtest — {datetime.now(ET):%Y-%m-%d}\n\n"
        f"Symbols {args.symbols}, {args.start}..{args.end}, SIP 1m base.\n"
        f"Definitions + methodology: `backend/scripts/research_ict_backtest.py`"
        f" docstring.\n\n```\n{out.to_string(index=False)}\n```\n",
        encoding="utf-8",
    )
    print(f"\nwrote {doc}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
