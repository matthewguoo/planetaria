"""Short-term mean reversion, measured directly instead of through candles.

The candle study (candle-null/) found every "working" pattern was a noisy
proxy for one factor: liquid names mean-revert over 1-5 days. This measures
the factor itself, in the three forms our stack could actually trade:

  1. 1-day reversal    rank yesterday's close-to-close move, cross-section
  2. 5-day reversal    rank the trailing week (the classic Lehmann/Lo-
                       MacKinlay contrarian horizon)
  3. gap fade          rank TODAY'S open vs yesterday's close at 09:30,
                       trade open -> close (the intraday MFT form; the
                       overnight-decomp study already showed post-earnings
                       day-1 reverses — this is the unconditional version)

Universe: the pead-llm-gate daily panels (top ~1,000/year, 2016-2026),
prior-day dollar volume >= $50M, price >= $5, Adjustment.ALL throughout
(ratios only). Entries at the NEXT OPEN for the close-signal forms (the
signal exists at the close; entering at that close is lookahead-adjacent),
at the open itself for the gap fade. Deciles re-ranked daily; D1 = losers/
gap-downs, D10 = winners/gap-ups.

Execution honesty: this is a daily-turnover strategy. Every line reports
gross and net at 6bp and 13bp round trips. The long-loser leg is the one
the account can take today (shorts exist but are gated); the short-winner
leg and long-short are reported for shape. Cooper (RFS 1999): reversal is
stronger after LOW-volume moves — one pre-stated conditioning, D1 split by
whether the mover's volume was below its own trailing-21d median.

Run:  python scripts/research_reversal.py
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
COSTS = (6.0, 13.0)


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def load_panel() -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]).reset_index(drop=True))
    g = df.groupby("symbol", sort=False)
    df["c1"] = g["c"].shift(1)
    df["c6"] = g["c"].shift(6)
    df["v_med21"] = g["v"].transform(lambda s: s.rolling(21, min_periods=10).median())
    df["dv1"] = df["c1"] * g["v"].shift(1)
    df["o_next"] = g["o"].shift(-1)
    df["c_next"] = g["c"].shift(-1)
    df["c_fwd5"] = g["c"].shift(-5)
    df["dt"] = pd.to_datetime(df["date"])
    df["fwd_gap"] = (g["dt"].shift(-1) - df["dt"]).dt.days
    df["r1"] = df["c"] / df["c1"] - 1
    df["r5"] = df["c"] / df["c6"] - 1
    df["gap_next"] = df["o_next"] / df["c"] - 1     # known at T+1 09:30
    df["oc_next"] = df["c_next"] / df["o_next"] - 1
    df["o5_next"] = df["c_fwd5"] / df["o_next"] - 1
    df["lowvol"] = df["v"] < df["v_med21"]
    return df


def decile_series(df: pd.DataFrame, signal: str, fwd: str) -> pd.DataFrame:
    d = df.dropna(subset=[signal, fwd]).copy()
    d["dec"] = d.groupby("date")[signal].transform(
        lambda s: np.clip(np.ceil(s.rank(pct=True) * 10), 1, 10)).astype(int)
    return (d.groupby(["date", "dec"])[fwd].mean().reset_index()
            .pivot(index="date", columns="dec", values=fwd))


def emit_block(emit, title, piv, halves, hold_days=1):
    unit = "bp/trade" if hold_days > 1 else "bp/d"
    emit(f"## {title}")
    emit()
    if hold_days > 1:
        emit(f"{hold_days}-session holds measured on overlapping daily "
             f"starts; t is computed on every {hold_days}th start "
             "(non-overlapping) and the annualisation charges one round "
             f"trip per {hold_days}-day trade.")
        emit()
    emit(f"| leg | window | {unit} gross | t | net@6bp | net@13bp | ann net@6 % |")
    emit("|---|---|---|---|---|---|---|")
    legs = {
        "long D1 (losers/gap-down)": piv[1] * 1e4,
        "short D10 (winners/gap-up)": -piv[10] * 1e4,
        "long-short D1-D10": (piv[1] - piv[10]) * 1e4,
    }
    for lname, s in legs.items():
        for wname, m in halves.items():
            x = s[m].dropna().to_numpy()
            if len(x) < 30:
                continue
            n6, n13 = x.mean() - COSTS[0], x.mean() - COSTS[1]
            t = tstat(x[::hold_days])
            ann = n6 * 252 / hold_days / 100
            emit(f"| {lname} | {wname} | {x.mean():+.1f} | {t:+.1f} "
                 f"| {n6:+.1f} | {n13:+.1f} | {ann:+.1f} |")
    emit()
    row = " | ".join(f"{piv[d].mean() * 1e4:+.1f}" for d in range(1, 11))
    emit(f"decile means D1..D10 ({unit} gross): {row}")
    emit()


def main() -> None:
    df = load_panel()
    ok = ((df["dv1"] >= MIN_DV) & (df["c1"] >= MIN_PX) & (df["fwd_gap"] <= 5))
    df = df[ok].reset_index(drop=True)

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Short-term reversal and gap fade — {STAMP}")
    emit()
    emit(f"{len(df):,} eligible symbol-days. Deciles daily; forward legs "
         "from the NEXT OPEN for close signals, from the open for the gap "
         "fade. Net lines charge the full round trip to the single traded "
         "leg per day.")
    emit()

    for signal, fwd, title, hold in (
            ("r1", "oc_next", "1-day reversal -> next open->close", 1),
            ("r5", "oc_next", "5-day reversal -> next open->close", 1),
            ("r5", "o5_next", "5-day reversal -> next open->T+5 close (weekly form)", 5),
            ("gap_next", "oc_next", "gap fade: open gap -> same day open->close", 1)):
        piv = decile_series(df, signal, fwd)
        idx = pd.to_datetime(piv.index)
        halves = {"all": np.ones(len(piv), bool),
                  "2016-20": np.asarray(idx < "2021-01-01"),
                  "2021-26": np.asarray(idx >= "2021-01-01")}
        emit_block(emit, title, piv, halves, hold_days=hold)

    # Cooper conditioning: the loser decile split by the mover's own volume.
    d = df.dropna(subset=["r1", "oc_next"]).copy()
    d["dec"] = d.groupby("date")["r1"].transform(
        lambda s: np.clip(np.ceil(s.rank(pct=True) * 10), 1, 10)).astype(int)
    d1 = d[d["dec"] == 1]
    emit("## Cooper conditioning: D1 losers by the move's volume")
    emit()
    for label, mm in (("low-volume losers (reversal should be stronger)",
                       d1["lowvol"]),
                      ("high-volume losers", ~d1["lowvol"])):
        s = d1[mm].groupby("date")["oc_next"].mean() * 1e4
        x = s.to_numpy()
        emit(f"- {label}: {x.mean():+.1f}bp/d gross (t {tstat(x):+.1f}, "
             f"net@6 {x.mean() - 6:+.1f}, n_days={len(x)})")
    emit()

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"reversal_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
