"""Ex-dividend predictability, measured from the panels' own seams.

No dividend feed needed: the study tree now carries the SAME daily closes
twice — Adjustment.ALL (bars_ohlc_*) and RAW (bars_rawoc_*) — and their
ratio k = adj/raw is piecewise-constant, stepping exactly at corporate
actions. A step of a few percent is an ex-dividend date and its size is
the dividend yield; a step near a round factor is a split and is excluded.
Every ex-div event for the liquid universe, 2016-2026, reconstructed
offline.

Two documented claims to test:

  EX-DAY PREMIUM (Elton-Gruber 1970 line): the open drops by LESS than
  the dividend, so close(t-1) -> open(ex) + div is a positive overnight
  return to the holder. The classic tax-clientele effect; the modern
  question is whether anything survives in liquid names.

  DIVIDEND RUN (Hartzmark-Solomon 2013, "the dividend month premium"):
  abnormal buying pressure BETWEEN announcement and ex-day (+~50bp/month
  in their 1927-2011 sample), with reversal after. Without announcement
  dates we test the tradeable shadow: the run-up [t-10, t-1] into the
  ex-day vs the same-universe baseline, and the reversal [t+1, t+10].

All returns computed on ADJUSTED prices (so the dividend itself is in the
return where it belongs); the raw panel's only job is finding the events.
Gross of costs; a round trip on these names is 6-13bp.

Run:  python scripts/research_exdiv.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MIN_DV = 5e7
MIN_PX = 5.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def load_joined() -> pd.DataFrame:
    adj = pd.concat([pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
                     for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))],
                    ignore_index=True)
    raw = pd.concat([pd.read_parquet(p)
                     for p in sorted(PEAD_CACHE.glob("bars_rawoc_*.parquet"))],
                    ignore_index=True)
    for df in (adj, raw):
        df["date"] = df["date"].astype(str)
    adj = adj.drop_duplicates(subset=["symbol", "date"])
    raw = (raw.drop_duplicates(subset=["symbol", "date"])
           .rename(columns={"o": "o_raw", "c": "c_raw"}))
    j = adj.merge(raw, on=["symbol", "date"], how="inner")
    j = j.sort_values(["symbol", "date"]).reset_index(drop=True)
    return j


def main() -> None:
    j = load_joined()
    g = j.groupby("symbol", sort=False)
    j["k"] = j["c"] / j["c_raw"]
    j["k_prev"] = g["k"].shift(1)
    j["c_prev"] = g["c"].shift(1)
    j["craw_prev"] = g["c_raw"].shift(1)
    j["dv_prev"] = j["c_prev"] * g["v"].shift(1)
    j["dt"] = pd.to_datetime(j["date"])
    j["gap_days"] = (j["dt"] - g["dt"].shift(1)).dt.days
    # The seam: k steps at corporate actions. Dividends are small steps;
    # splits are large round ones. 5bp of float noise tolerance.
    step = j["k"] / j["k_prev"] - 1
    j["div_yield"] = np.where(step > 0.0005, 1 - j["k_prev"] / j["k"], np.nan)
    is_div = (np.isfinite(j["div_yield"]) & (j["div_yield"] > 0.0005)
              & (j["div_yield"] < 0.06)             # splits/specials out
              & (j["gap_days"] <= 5)
              & (j["dv_prev"] >= MIN_DV) & (j["c_prev"] >= MIN_PX))
    ev = j[is_div].copy()

    # Returns on the ADJUSTED series (dividend included by construction).
    j["on_ret"] = j["o"] / j["c_prev"] - 1           # close(t-1) -> open(t)
    j["cc1"] = j["c"] / j["c_prev"] - 1
    # run-up and reversal windows, per symbol, adjusted closes
    j["run10"] = g["c"].shift(1) / g["c"].shift(11) - 1
    j["rev10"] = g["c"].shift(-10) / j["c"] - 1
    ev = ev.join(j.loc[ev.index, ["on_ret", "cc1", "run10", "rev10"]])
    # Raw drop ratio (Elton-Gruber): (P_{t-1} - open_raw_t) / div
    div_ps = ev["div_yield"] * ev["craw_prev"]
    ev["drop_ratio"] = (ev["craw_prev"] - ev["o_raw"]) / div_ps

    # Universe baselines: same panel-days, same floors, div and non-div.
    base_ok = ((j["dv_prev"] >= MIN_DV) & (j["c_prev"] >= MIN_PX)
               & (j["gap_days"] <= 5))
    base = j[base_ok & ~is_div]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Ex-dividend predictability from the panel seams — {STAMP}")
    emit()
    emit(f"{int(is_div.sum()):,} ex-dividend events detected "
         f"({ev['symbol'].nunique()} symbols, median yield "
         f"{ev['div_yield'].median() * 1e4:.0f}bp), "
         f"{len(base):,} baseline symbol-days, 2016-2026, floors "
         f"${MIN_DV / 1e6:.0f}M / ${MIN_PX:.0f}. Returns on adjusted prices "
         "(the dividend is in the return); gross of 6-13bp round trips.")
    emit()
    emit("| measure | n | div events | baseline | edge bp | t(edge) |")
    emit("|---|---|---|---|---|---|")

    def row(label, col, sub=None, base_col=None):
        e = (sub if sub is not None else ev)[col].to_numpy() * 1e4
        b = base[base_col or col].to_numpy() * 1e4
        e, b = e[np.isfinite(e)], b[np.isfinite(b)]
        diff = e.mean() - b.mean()
        se = np.sqrt(e.var(ddof=1) / len(e) + b.var(ddof=1) / len(b))
        emit(f"| {label} | {len(e):,} | {e.mean():+.1f} | {b.mean():+.1f} "
             f"| {diff:+.1f} | {diff / se:+.2f} |")

    row("overnight into ex-day (close->open, div incl.)", "on_ret")
    row("full ex-day (close->close, div incl.)", "cc1")
    row("run-up [t-10, t-1]", "run10")
    row("reversal [t+1, t+10]", "rev10")
    hi = ev[ev["div_yield"] >= ev["div_yield"].quantile(0.8)]
    row("overnight, top-quintile yields", "on_ret", sub=hi)
    row("run-up, top-quintile yields", "run10", sub=hi)
    row("reversal, top-quintile yields", "rev10", sub=hi)
    emit()

    dr = ev["drop_ratio"].to_numpy()
    dr = dr[np.isfinite(dr) & (np.abs(dr) < 5)]
    emit(f"Elton-Gruber drop ratio (open drop / dividend): mean "
         f"{dr.mean():.3f}, median {np.median(dr):.3f} "
         f"(1.0 = fully efficient; <1 pays the holder), n={len(dr):,}, "
         f"t vs 1.0 {tstat(dr - 1):+.2f}.")
    emit()
    by_year = ev.assign(year=ev["date"].str[:4]).groupby("year")
    emit("| year | events | overnight bp | t | drop ratio |")
    emit("|---|---|---|---|---|")
    for y, s in by_year:
        x = s["on_ret"].to_numpy() * 1e4
        d = s["drop_ratio"].to_numpy()
        d = d[np.isfinite(d) & (np.abs(d) < 5)]
        emit(f"| {y} | {len(s):,} | {x.mean():+.1f} | {tstat(x):+.2f} "
             f"| {np.median(d):.3f} |")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"exdiv_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
