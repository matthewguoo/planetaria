"""The minute-tape cache — the 3090 project's dataset (brief §4 shelf).

Full-session SIP minute bars, with vwap and trade_count (the finest bar
feed Alpaca serves; sub-minute means raw tick pulls, which remain the
per-event surgical tool), for a stable liquid universe:

  universe   monthly top-100 by PRIOR-month mean dollar volume from the
             adjusted daily panels (ranking is ratio-safe; point-in-time —
             month M's list uses only month M-1's data).
  window     2022-01 .. 2026-08, 09:30-16:00 ET, feed=sip, raw prints.
             ~1,130 sessions x ~100 names x 390 minutes ~= 44M bars.
  cache      one parquet per month (cache/mtape_YYYY-MM.parquet),
             resumable at day granularity within each month.
  basis      raw intraday prints; any cross-day feature joins the daily
             panels by ratio only (scan-note §4 trap).

The modeling protocol lives with the models, not here — but the order of
battle is fixed by the brief: CPU GBM on engineered bar features is the
baseline any GPU sequence model must beat, walk-forward by year,
train-label placebo, thresholds as families.

Run:  python scripts/research_minute_tape.py fetch
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

import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")

START, END = "2022-01-01", "2026-08-04"
TOP_N = 100
CHUNK = 75
DV_MIN, PX_MIN = 1.5e8, 10.0


def monthly_universe() -> dict[str, list[str]]:
    """month 'YYYY-MM' -> that month's top-100 (built from month M-1)."""
    frames = [pd.read_parquet(p, columns=["symbol", "date", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = df[df["date"] >= "2021-11-01"].drop_duplicates(["symbol", "date"])
    df["dv"] = df["c"] * df["v"]
    df["month"] = df["date"].str[:7]
    agg = (df.groupby(["month", "symbol"])
           .agg(dv=("dv", "mean"), px=("c", "median"), n=("dv", "size"))
           .reset_index())
    agg = agg[(agg["dv"] >= DV_MIN) & (agg["px"] >= PX_MIN) & (agg["n"] >= 15)]
    months = sorted(m for m in df["month"].unique() if m >= "2021-12")
    out: dict[str, list[str]] = {}
    for prev, cur in zip(months[:-1], months[1:]):
        g = agg[agg["month"] == prev].nlargest(TOP_N, "dv")
        out[cur] = sorted(g["symbol"])
    return out


def sessions() -> list[str]:
    spy = pd.concat([pd.read_parquet(f) for f in
                     sorted(PEAD_CACHE.glob("bench_SPY_*.parquet"))])
    days = sorted(set(spy["date"].astype(str)))
    return [d for d in days if START <= d <= END]


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    CACHE.mkdir(parents=True, exist_ok=True)
    uni = monthly_universe()
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    all_days = sessions()
    by_month: dict[str, list[str]] = {}
    for d in all_days:
        by_month.setdefault(d[:7], []).append(d)
    total_done = 0
    for month in sorted(by_month):
        if month not in uni:
            continue
        out_f = CACHE / f"mtape_{month}.parquet"
        prior, have = pd.DataFrame(), set()
        if out_f.exists():
            prior = pd.read_parquet(out_f)
            have = set(prior["date"].astype(str).unique())
        days = [d for d in by_month[month] if d not in have]
        if not days:
            continue
        syms = uni[month]
        print(f"{month}: {len(days)} days x {len(syms)} names", flush=True)
        month_rows: list[dict] = []
        for day in days:
            d = datetime.fromisoformat(day)
            dayrows: list[dict] = []
            failed = False
            for i in range(0, len(syms), CHUNK):
                chunk = syms[i:i + CHUNK]
                try:
                    res = api.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
                        start=d.replace(hour=9, minute=30, tzinfo=ET),
                        end=d.replace(hour=16, minute=0, tzinfo=ET),
                        feed="sip"))
                except Exception as exc:               # noqa: BLE001
                    print(f"  {day}: {str(exc)[:70]}", flush=True)
                    _time.sleep(1.0)
                    failed = True
                    break
                for sym in chunk:
                    for b in res.data.get(sym) or []:
                        ts = b.timestamp.astimezone(ET)
                        dayrows.append({
                            "symbol": sym, "date": day,
                            "minute": ts.hour * 60 + ts.minute - 570,
                            "o": float(b.open), "h": float(b.high),
                            "l": float(b.low), "c": float(b.close),
                            "v": float(b.volume),
                            "vw": float(b.vwap or 0.0),
                            "n": int(b.trade_count or 0)})
                _time.sleep(0.2)
            if not failed:
                month_rows.extend(dayrows)
                total_done += 1
        if month_rows:
            df = pd.DataFrame(month_rows)
            allf = (pd.concat([prior, df], ignore_index=True)
                    if len(prior) else df)
            allf = allf.drop_duplicates(subset=["symbol", "date", "minute"])
            allf.to_parquet(out_f)
            print(f"  -> {out_f.name}: {len(allf):,} bars", flush=True)
    print(f"fetch pass complete ({total_done} new days this run)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch"])
    args = ap.parse_args()
    stage_fetch(args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
