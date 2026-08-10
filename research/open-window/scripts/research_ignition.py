"""Momentum ignition at the open, with the stop measured instead of hoped.

Matthew's ask: the engine reads the tape fast at the open — can it jump on
a pop or a dump and protect itself with a "denoising" stop? Two priors
say no (the chart study's ORB lost with every bracket it swept; the PEAD
study measured stops donating the edge inside the whipsaw band), but both
were different universes. This measures it on the morning-gap universe at
1-minute resolution, with the stop-whipsaw cost accounted bar-by-bar.

Design, pre-stated:
  universe   the open-window study's gap events (top-8 liquid |gap|>=1.5%
             names per day, 2022-2026)
  trigger    |move from the 09:30 open| >= T by minute m (m <= 15),
             T in {50, 100}bp; enter WITH the move at that minute's close
  stops      none / fixed 40bp / fixed 80bp / trailing 80bp; stop fills
             AT the stop level when the bar's low (high, for shorts)
             crosses it — the conservative fill
  exits      11:00 or 15:55 close, whichever the config says, unless the
             stop fired first
  null       the same bracket entered at a RANDOM minute (1..30) on the
             same names — 25 draws per config; the edge is actual minus
             null, exactly the chart study's discipline
  costs      6bp round trip

Sides are reported separately: long-the-pop is placeable today;
short-the-dump sits behind equity_long_only like everything else short.

Run:  python scripts/research_ignition.py fetch
      python scripts/research_ignition.py score
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_open_window import gap_events  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
PATHS_F = CACHE / "open_paths.parquet"
COST_BP = 6.0
N_MIN = 90                     # 09:30 -> 11:00
RNG = np.random.default_rng(20260810)


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    CACHE.mkdir(parents=True, exist_ok=True)
    ev = gap_events()
    prior, have = pd.DataFrame(), set()
    if PATHS_F.exists():
        prior = pd.read_parquet(PATHS_F)
        have = set(prior["date"].astype(str))
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    days = [d for d in sorted(ev["date"].unique()) if d not in have]
    print(f"{len(days)} days to fetch")
    for n, day in enumerate(days):
        g = ev[ev["date"] == day]
        d = datetime.fromisoformat(day)
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sorted(g["symbol"]),
                timeframe=TimeFrame.Minute,
                start=d.replace(hour=9, minute=30, tzinfo=ET),
                end=d.replace(hour=15, minute=56, tzinfo=ET),
                feed="sip"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  {day}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        for _, e in g.iterrows():
            bars = res.data.get(e["symbol"]) or []
            o = c = h = lo = None
            path_c = np.full(N_MIN, np.nan)
            path_h = np.full(N_MIN, np.nan)
            path_l = np.full(N_MIN, np.nan)
            last = None
            for b in bars:
                ts = b.timestamp.astimezone(ET)
                minute = (ts.hour - 9) * 60 + ts.minute - 30
                if minute == 0 and o is None:
                    o = float(b.open)
                if 0 <= minute < N_MIN:
                    path_c[minute] = float(b.close)
                    path_h[minute] = float(b.high)
                    path_l[minute] = float(b.low)
                if ts.strftime("%H:%M") == "15:55":
                    last = float(b.close)
            if o is None or last is None or not np.isfinite(path_c[:16]).any():
                continue
            rows.append({"symbol": e["symbol"], "date": day, "gap": float(e["gap"]),
                         "open": o, "close1555": last,
                         "c": path_c.tolist(), "h": path_h.tolist(),
                         "l": path_l.tolist()})
        if n % 50 == 0:
            print(f"  {n}/{len(days)} ({day}, {len(rows)} rows)")
        _time.sleep(0.3)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf.to_parquet(PATHS_F)
    print(f"cached {len(allf)} first-hour paths -> {PATHS_F}")


# -------------------------------------------------------------------- score

def simulate(c, h, lo, open_px, close1555, entry_min, side, stop_kind,
             stop_bp, exit_1100: bool) -> float | None:
    """One trade: enter at entry_min close, walk minute bars, conservative
    stop fills, exit at 11:00 close or 15:55."""
    e = c[entry_min]
    if not np.isfinite(e) or e <= 0:
        return None
    stop_px = None
    best = e
    if stop_kind in ("fixed", "trail"):
        stop_px = e * (1 - side * stop_bp / 1e4)
    end = min(len(c) - 1, 89) if exit_1100 else len(c) - 1
    for m in range(entry_min + 1, end + 1):
        if stop_px is not None and np.isfinite(lo[m]) and np.isfinite(h[m]):
            crossed = lo[m] <= stop_px if side > 0 else h[m] >= stop_px
            if crossed:
                return side * (stop_px / e - 1) * 1e4 - COST_BP
        if stop_kind == "trail" and np.isfinite(c[m]):
            if side > 0 and c[m] > best:
                best = c[m]
                stop_px = best * (1 - stop_bp / 1e4)
            elif side < 0 and c[m] < best:
                best = c[m]
                stop_px = best * (1 + stop_bp / 1e4)
    x = c[end] if exit_1100 and np.isfinite(c[end]) else close1555
    return side * (x / e - 1) * 1e4 - COST_BP


def stage_score(args) -> None:
    df = pd.read_parquet(PATHS_F)
    C = np.stack([np.asarray(v, float) for v in df["c"]])
    H = np.stack([np.asarray(v, float) for v in df["h"]])
    L = np.stack([np.asarray(v, float) for v in df["l"]])
    # extend paths to 15:55 by appending the close mark as a final "minute"
    opens = df["open"].to_numpy()
    closes = df["close1555"].to_numpy()

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Momentum ignition at the open, stops measured — {STAMP}")
    emit()
    emit(f"{len(df):,} first-hour paths (1-min), gap universe 2022-2026. "
         f"Entry with the move on trigger; conservative stop fills; "
         f"{COST_BP:.0f}bp charged. `edge` = actual minus the same config "
         "entered at a random minute 1-30 (25 draws) — the chart study's "
         "null. A config only matters if it beats ITS OWN null.")
    emit()
    emit("| trigger | side | stop | exit | n | bp | t | null bp | edge bp | stop-out% |")
    emit("|---|---|---|---|---|---|---|---|---|---|")

    def first_trigger(paths_c, open_px, thr_bp, side):
        rel = (paths_c[:16] / open_px - 1) * side * 1e4
        okm = np.where(np.isfinite(rel) & (rel >= thr_bp))[0]
        return int(okm[0]) if len(okm) else None

    for thr in (50.0, 100.0):
        for side, sname in ((+1, "long pop"), (-1, "short dump")):
            for stop_kind, stop_bp, stlabel in (
                    (None, 0.0, "none"), ("fixed", 40.0, "fix40"),
                    ("fixed", 80.0, "fix80"), ("trail", 80.0, "trail80")):
                for exit_1100, exlabel in ((True, "11:00"), (False, "15:55")):
                    rets, stopped = [], 0
                    null_rets = []
                    for i in range(len(df)):
                        m = first_trigger(C[i], opens[i], thr, side)
                        if m is None:
                            continue
                        r = simulate(C[i], H[i], L[i], opens[i], closes[i],
                                     m, side, stop_kind, stop_bp, exit_1100)
                        if r is None:
                            continue
                        rets.append(r)
                        if stop_kind and r < -stop_bp * 0.5:
                            stopped += 1
                        for em in RNG.integers(1, 31, size=25):
                            rn = simulate(C[i], H[i], L[i], opens[i],
                                          closes[i], int(em), side,
                                          stop_kind, stop_bp, exit_1100)
                            if rn is not None:
                                null_rets.append(rn)
                    if len(rets) < 100:
                        continue
                    r = np.array(rets)
                    nl = np.array(null_rets)
                    emit(f"| {thr:.0f}bp/15m | {sname} | {stlabel} "
                         f"| {exlabel} | {len(r):,} | {r.mean():+.1f} "
                         f"| {tstat(r):+.2f} | {nl.mean():+.1f} "
                         f"| {r.mean() - nl.mean():+.1f} "
                         f"| {stopped / len(r) * 100:.0f} |")
        emit("| | | | | | | | | | |")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"ignition_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
