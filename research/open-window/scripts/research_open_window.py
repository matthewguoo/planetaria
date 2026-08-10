"""The opening window, at minute resolution: where does the fade complete?

Matthew's ask: mean reversion in the first 15-30 minutes on the names
retail piles into at the open — the degen window, with automation as the
edge. The daily-data bleed map already showed the day-level fade (L/S
+10.5bp/d, t 3.7, short-the-gap-up side carrying it); what daily bars
cannot see is the CLOCK — if the reversion completes by 10:00, the
open->close version holds six extra hours of noise for nothing, and the
tradeable form is a 9:35->10:30 scalp.

Universe per day: the top-8 by |gap| x dollar-volume among names gapping
|>=1.5%| vs the prior close, prior-day dollar volume >= $150M, price >=
$10 — the liquid end of the morning movers a scanner would actually
watch. 2022-01 -> 2026-08 (the 2022 bear is in sample on purpose).

Two questions, one dataset:
  FADE CURVE   enter against the gap at 09:31 or 09:35, exit at each
               checkpoint (09:45, 10:00, 10:30, 11:00, 12:00, 15:55).
               Where does the P&L stop growing?
  FIRST-30 REV rank the 09:30->10:00 move within the same universe; fade
               the extremes from 10:00 to 11:00 / 12:00 / close
               (the Heston-Korajczyk-Sadka window, event-conditioned).

Minute bars are SIP; marks are bar closes (mid-ish on liquid names).
Net lines charge a 6bp round trip. Fetch batches one request per DAY.

Run:  python scripts/research_open_window.py fetch
      python scripts/research_open_window.py score
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
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

MARKS = ("09:30", "09:35", "09:45", "10:00", "10:30", "11:00", "12:00", "15:55")
MARKS_F = CACHE / "open_marks.parquet"
COST_BP = 6.0
START = "2022-01-01"


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def gap_events() -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]))
    g = df.groupby("symbol", sort=False)
    df["c_prev"] = g["c"].shift(1)
    df["dv_prev"] = df["c_prev"] * g["v"].shift(1)
    df["dt"] = pd.to_datetime(df["date"])
    df["gapd"] = (df["dt"] - g["dt"].shift(1)).dt.days
    df["gap"] = df["o"] / df["c_prev"] - 1
    ok = ((df["date"] >= START) & (df["gapd"] <= 5)
          & (df["dv_prev"] >= 1.5e8) & (df["c_prev"] >= 10)
          & (df["gap"].abs() >= 0.015))
    ev = df[ok].copy()
    ev["score"] = ev["gap"].abs() * np.log(ev["dv_prev"])
    ev["rank"] = ev.groupby("date")["score"].rank(ascending=False)
    return ev[ev["rank"] <= 8][["symbol", "date", "gap", "dv_prev"]]


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    CACHE.mkdir(parents=True, exist_ok=True)
    ev = gap_events()
    print(f"{len(ev)} gap events across {ev['date'].nunique()} days")
    prior, have = pd.DataFrame(), set()
    if MARKS_F.exists():
        prior = pd.read_parquet(MARKS_F)
        have = set(prior["date"].astype(str))
    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    rows = []
    days = [d for d in sorted(ev["date"].unique()) if d not in have]
    for n, day in enumerate(days):
        g = ev[ev["date"] == day]
        d = datetime.fromisoformat(day)
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sorted(g["symbol"]),
                timeframe=TimeFrame.Minute,
                start=d.replace(hour=9, minute=29, tzinfo=ET),
                end=d.replace(hour=16, minute=1, tzinfo=ET),
                feed="sip"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  {day}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        for _, e in g.iterrows():
            bars = res.data.get(e["symbol"]) or []
            marks = {}
            for b in bars:
                hm = b.timestamp.astimezone(ET).strftime("%H:%M")
                if hm in MARKS:
                    marks.setdefault(hm, float(b.close))
            if "09:30" not in marks or "15:55" not in marks:
                continue
            rows.append({"symbol": e["symbol"], "date": day,
                         "gap": float(e["gap"]),
                         **{f"m{hm.replace(':', '')}": marks.get(hm)
                            for hm in MARKS}})
        if n % 50 == 0:
            print(f"  {n}/{len(days)} ({day}, {len(rows)} rows)")
        _time.sleep(0.3)
    df = pd.DataFrame(rows)
    allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
    allf.to_parquet(MARKS_F)
    print(f"cached {len(allf)} marked gap events -> {MARKS_F}")


def stage_score(args) -> None:
    j = pd.read_parquet(MARKS_F)
    j["year"] = j["date"].str[:4]
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The opening window at minute resolution — {STAMP}")
    emit()
    emit(f"{len(j):,} gap events, {j['date'].nunique()} days, "
         f"{START}->. Fade = against the gap sign; net charges "
         f"{COST_BP:.0f}bp per round trip. Marks are SIP minute closes.")
    emit()

    fade = -np.sign(j["gap"].to_numpy())
    for entry in ("0931", "0935"):
        e = j["m0930" if entry == "0931" else "m0935"].to_numpy()
        emit(f"## Fade curve, entry at {entry[:2]}:{entry[2:]} "
             f"({'first bar close' if entry == '0931' else 'auction settled'})")
        emit()
        emit("| exit | n | all bp | t | net | short-gap-up only bp | t | long-gap-down only bp | t |")
        emit("|---|---|---|---|---|---|---|---|---|")
        for exit_hm in ("0945", "1000", "1030", "1100", "1200", "1555"):
            x = j[f"m{exit_hm}"].to_numpy()
            ok = np.isfinite(e) & np.isfinite(x) & (e > 0)
            r = fade[ok] * (x[ok] / e[ok] - 1) * 1e4
            up = ok & (j["gap"].to_numpy() > 0)
            dn = ok & (j["gap"].to_numpy() < 0)
            r_up = fade[up] * (j[f"m{exit_hm}"].to_numpy()[up] / e[up] - 1) * 1e4
            r_dn = fade[dn] * (j[f"m{exit_hm}"].to_numpy()[dn] / e[dn] - 1) * 1e4
            emit(f"| {exit_hm[:2]}:{exit_hm[2:]} | {int(ok.sum()):,} "
                 f"| {r.mean():+.1f} | {tstat(r):+.2f} "
                 f"| {r.mean() - COST_BP:+.1f} "
                 f"| {r_up.mean():+.1f} | {tstat(r_up):+.2f} "
                 f"| {r_dn.mean():+.1f} | {tstat(r_dn):+.2f} |")
        emit()

    # First-30-minute overreaction: rank the 09:30->10:00 move, fade it.
    emit("## First-30-min overreaction (rank the 09:30->10:00 move, fade)")
    emit()
    j2 = j.dropna(subset=["m0930", "m1000", "m1100", "m1200", "m1555"]).copy()
    j2["r30"] = j2["m1000"] / j2["m0930"] - 1
    j2["rk"] = j2.groupby("date")["r30"].rank(pct=True)
    emit("| leg | exit | n | bp | t | net |")
    emit("|---|---|---|---|---|---|")
    for label, sel, sign in (
            ("fade top-3rd (short the AM ripper)", j2["rk"] >= 2 / 3, -1.0),
            ("fade bottom-3rd (long the AM dumper)", j2["rk"] <= 1 / 3, +1.0)):
        sub = j2[sel]
        for exit_hm in ("1100", "1200", "1555"):
            r = sign * (sub[f"m{exit_hm}"] / sub["m1000"] - 1) * 1e4
            r = r.to_numpy()
            emit(f"| {label} | {exit_hm[:2]}:{exit_hm[2:]} | {len(r):,} "
                 f"| {r.mean():+.1f} | {tstat(r):+.2f} "
                 f"| {r.mean() - COST_BP:+.1f} |")
    emit()
    emit("## Short-gap-up fade, 09:35 -> 10:30, by year (the candidate cell)")
    emit()
    emit("| year | n | bp | t | net |")
    emit("|---|---|---|---|---|")
    up = j[(j["gap"] > 0)].dropna(subset=["m0935", "m1030"])
    for y, g in up.groupby("year"):
        r = -(g["m1030"] / g["m0935"] - 1).to_numpy() * 1e4
        emit(f"| {y} | {len(r):,} | {r.mean():+.1f} | {tstat(r):+.2f} "
             f"| {r.mean() - COST_BP:+.1f} |")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"open_window_{STAMP}.md"
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
