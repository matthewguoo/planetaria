"""Start dumb, find the bleed: decomposing the gap fade's missing bp.

The reversal study left one live gross edge: fade the overnight gap,
open->close (L/S +10.5bp/d gross, t 3.7) — and a net line that dies at
realistic costs. Matthew's program: run the dumb version conceptually,
then locate every place it bleeds and remove them one at a time.

This is pass one, from daily data. The bleed candidates it can measure:

  1. SIGNAL DILUTION — is the edge in the extreme gaps only? (If D1's
     edge is entirely in the < -4% tail, trading the whole decile is
     paying costs on passengers.)
  2. UNIVERSE COST — price and dollar-volume buckets. A $6 stock's fade
     can gross +15bp and still lose: its spread is the bleed. The range%
     column ((H-L)/C of the traded day) is the cost proxy per bucket.
  3. EVENT CONTAMINATION — gaps caused by an AMC earnings report last
     night are a different animal (the overnight-decomp study showed
     post-earnings day-1 REVERSES, i.e. helps the fade — but those names
     also carry event spreads). Flagged from events_v2.
  4. REGIME — SPY trailing-vol split and day-of-week: does the fade only
     pay when the market is stressed?

What pass one CANNOT see (queued for a minute-bar pass on a broad panel):
the intraday hold-time curve — whether the fade's profit is complete by
10:30 and the rest of the day is round-trip risk for nothing.

Run:  python scripts/research_gapfade_bleed.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from research_reversal import MIN_DV, MIN_PX, load_panel, tstat  # noqa: E402

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")


def daily_mean(sub: pd.DataFrame, col: str, sign: float) -> np.ndarray:
    return (sub.groupby("date")[col].mean() * sign * 1e4).to_numpy()


def main() -> None:
    df = load_panel()
    ok = ((df["dv1"] >= MIN_DV) & (df["c1"] >= MIN_PX) & (df["fwd_gap"] <= 5)
          & df["gap_next"].notna() & df["oc_next"].notna())
    df = df[ok].reset_index(drop=True)

    # traded-day range% as the cost proxy (the fade trades day T+1; the
    # panel row for T+1 carries its own h/l/c — approximate with day T's
    # to stay within one frame: shift is one session and ranges are
    # persistent, which is all a proxy needs).
    hl = pd.concat([pd.read_parquet(p, columns=["symbol", "date", "h", "l", "c"])
                    for p in sorted(PEAD_CACHE.glob("bars_ohlc_*.parquet"))])
    hl["date"] = hl["date"].astype(str)
    hl = hl.drop_duplicates(subset=["symbol", "date"])
    hl["range_pct"] = (hl["h"] - hl["l"]) / hl["c"] * 100
    df = df.merge(hl[["symbol", "date", "range_pct"]], on=["symbol", "date"],
                  how="left")

    ev = pd.concat([pd.read_parquet(p, columns=["symbol", "date"])
                    for p in sorted(PEAD_CACHE.glob("events_v2_*.parquet"))])
    ev_set = set(zip(ev["symbol"], ev["date"].astype(str)))
    df["earn"] = [(s, d) in ev_set for s, d in zip(df["symbol"], df["date"])]

    spy = df[df["symbol"] == "SPY"][["date", "r1"]].copy()
    spy["spyvol"] = spy["r1"].rolling(21, min_periods=10).std() * np.sqrt(252) * 100
    df = df.merge(spy[["date", "spyvol"]], on="date", how="left")
    volmed = df.drop_duplicates("date")["spyvol"].median()
    df["dow"] = pd.to_datetime(df["date"]).dt.dayofweek  # of signal day T

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Gap-fade bleed map, pass one (daily data) — {STAMP}")
    emit()
    emit(f"{len(df):,} eligible symbol-days. LONG leg fades gaps <= -0.5% "
         "(buy the T+1 open, sell its close); SHORT leg fades gaps >= "
         "+0.5%. All GROSS; range% is the traded name's (H-L)/C cost "
         "proxy — a bucket is tradeable when its gross clears a realistic "
         "fraction of that range, not when it clears zero.")
    emit()

    for side_name, sel, sign in (
            ("LONG the gap-down", df["gap_next"] <= -0.005, +1.0),
            ("SHORT the gap-up (gated leg)", df["gap_next"] >= 0.005, -1.0)):
        side = df[sel]
        emit(f"## {side_name} (n={len(side):,}, "
             f"{len(side) / side['date'].nunique():.1f} names/day)")
        emit()
        emit("| slice | n | bp/d gross | t | names/d | range% |")
        emit("|---|---|---|---|---|---|")

        def row(label, m):
            sub = side[m]
            if sub["date"].nunique() < 60:
                return
            x = daily_mean(sub, "oc_next", sign)
            emit(f"| {label} | {len(sub):,} | {x.mean():+.1f} | {tstat(x):+.1f} "
                 f"| {len(sub) / sub['date'].nunique():.1f} "
                 f"| {sub['range_pct'].mean():.1f} |")

        row("ALL", side["gap_next"].notna())
        g = side["gap_next"].abs()
        emit("| — by gap size | | | | | |")
        row("0.5-1%", (g >= 0.005) & (g < 0.01))
        row("1-2%", (g >= 0.01) & (g < 0.02))
        row("2-4%", (g >= 0.02) & (g < 0.04))
        row(">=4%", g >= 0.04)
        emit("| — by price | | | | | |")
        row("$5-10", (side["c1"] >= 5) & (side["c1"] < 10))
        row("$10-25", (side["c1"] >= 10) & (side["c1"] < 25))
        row("$25-100", (side["c1"] >= 25) & (side["c1"] < 100))
        row("$100+", side["c1"] >= 100)
        emit("| — by dollar volume | | | | | |")
        row("$50-150M", (side["dv1"] >= 5e7) & (side["dv1"] < 1.5e8))
        row("$150-500M", (side["dv1"] >= 1.5e8) & (side["dv1"] < 5e8))
        row("$500M+", side["dv1"] >= 5e8)
        emit("| — by cause and regime | | | | | |")
        row("AMC earnings last night", side["earn"])
        row("no earnings", ~side["earn"])
        row("SPY vol above median", side["spyvol"] > volmed)
        row("SPY vol below median", side["spyvol"] <= volmed)
        row("Monday fade (gap over weekend)", side["dow"] == 4)
        emit()

    # The one cost-motivated (not mined) combination, both legs: liquid,
    # non-penny, big-enough gap that the signal isn't noise.
    emit("## Cost-motivated composite: px >= $25, dv >= $500M, |gap| >= 1%")
    emit()
    emit("Chosen for tradability (tight spreads, real gaps), not searched "
         "over — the buckets above are the search space and this is the one "
         "combination cost logic picks a priori.")
    emit()
    for label, sel, sign in (
            ("LONG leg", (df["gap_next"] <= -0.01) & (df["c1"] >= 25)
             & (df["dv1"] >= 5e8), +1.0),
            ("SHORT leg", (df["gap_next"] >= 0.01) & (df["c1"] >= 25)
             & (df["dv1"] >= 5e8), -1.0)):
        sub = df[sel]
        x = daily_mean(sub, "oc_next", sign)
        idx = pd.to_datetime(sorted(sub["date"].unique()))
        late = np.asarray(idx >= "2021-01-01")
        emit(f"- {label}: {x.mean():+.1f}bp/d gross (t {tstat(x):+.1f}), "
             f"{len(sub) / sub['date'].nunique():.1f} names/day, range% "
             f"{sub['range_pct'].mean():.1f}; 2021-26 {x[late].mean():+.1f} "
             f"(t {tstat(x[late]):+.1f})")
    emit()
    emit("Pass two (queued): the minute-bar hold-time curve — when in the "
         "day the fade's profit completes, which decides the exit clock "
         "and halves the round trip if the answer is 'by late morning'.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gapfade_bleed_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
