"""Account-level backtest of the REGISTERED afternoon-fly config. No sweeps.

The pre-registration (docs/pre-registration-afternoon-fly.md) fixes the
config: QQQ, 14:00 entry, 1.0% wings (min $1), credit sanity band 10-90% of
width, $0.02 credit given up for marketability, exit 15:50, max 5 sets on a
$10,000 allocation. This script prices exactly that from the cached OPRA
marks and reports the account curve, Sharpe, CAGR, max drawdown, and daily
alpha/beta against QQQ and SPY.

Two honest gaps, bracketed rather than papered over:
  exit    the cache marks 15:30 and expiry intrinsic, not 15:50 — both are
          reported; the registered exit sits between them.
  costs   leg friction is "~2-5c per structure per side" in the pre-reg;
          net rows are shown at 4c and 8c per structure ROUND TRIP on top
          of the $0.02 entry giveup.

Run: python scripts/research_fly_account.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

# The registered config — from the pre-registration, not re-chosen here.
ENTRY = "1400"
WIDTH = "1.0"
MIN_CREDIT_FRAC, MAX_CREDIT_FRAC = 0.10, 0.90
ENTRY_GIVEUP = 0.02          # $ credit surrendered for marketability
FRICTIONS = (0.04, 0.08)     # $ per structure round trip (2-5c/side band)
SETS = (1, 5)
ALLOC = 10_000.0


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def alpha_beta(ret: pd.Series, bench: pd.Series) -> tuple[float, float, float]:
    """OLS daily alpha (annualized %, on the $-return series) and beta."""
    j = pd.concat([ret, bench], axis=1, join="inner").dropna()
    if len(j) < 60:
        return float("nan"), float("nan"), float("nan")
    y, x = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
    beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1))
    alpha = float((y.mean() - beta * x.mean()) * 252 * 100)
    corr = float(np.corrcoef(y, x)[0, 1])
    return alpha, beta, corr


def bench_daily(sym: str) -> pd.Series:
    df = pd.read_parquet(CACHE / f"under30_raw_{sym}.parquet")
    df["d"] = df["ts"].dt.date.astype(str)
    close = df[df["ts"].dt.strftime("%H:%M") == "15:30"].set_index("d")["c"]
    return close.sort_index().pct_change().rename(sym)


def main() -> None:
    atm = pd.read_parquet(CACHE / "straddle_marks.parquet")
    atm = atm[atm["sym"] == "QQQ"].set_index("d")
    wings = pd.read_parquet(CACHE / "wing_marks.parquet").set_index("d")
    j = atm.join(wings, how="inner", rsuffix="_w").sort_index()

    cols = [f"C_{ENTRY}", f"P_{ENTRY}", f"C{WIDTH}_{ENTRY}", f"P{WIDTH}_{ENTRY}"]
    quoted = j.dropna(subset=cols).copy()

    w = quoted[f"K_C_{WIDTH}"] - quoted["k"]
    credit = (quoted[f"C_{ENTRY}"] + quoted[f"P_{ENTRY}"]
              - quoted[f"C{WIDTH}_{ENTRY}"] - quoted[f"P{WIDTH}_{ENTRY}"]
              - ENTRY_GIVEUP)
    frac = credit / w
    guard = (frac >= MIN_CREDIT_FRAC) & (frac <= MAX_CREDIT_FRAC) & ((w - credit) > 0.05)

    s_t = quoted["close"]
    intrinsic = ((s_t - quoted["k"]).abs()
                 - np.maximum(0.0, s_t - quoted[f"K_C_{WIDTH}"])
                 - np.maximum(0.0, quoted[f"K_P_{WIDTH}"] - s_t))
    pnl_intr = credit - intrinsic

    m1530 = ["C_1530", "P_1530", f"C{WIDTH}_1530", f"P{WIDTH}_1530"]
    have_1530 = quoted.dropna(subset=m1530).index
    val_1530 = (quoted["C_1530"] + quoted["P_1530"]
                - quoted[f"C{WIDTH}_1530"] - quoted[f"P{WIDTH}_1530"])
    pnl_1530 = credit - val_1530

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Afternoon fly, REGISTERED config, account level — {STAMP}")
    emit()
    emit(f"{len(j)} sessions 2024-01..2026-08; {len(quoted)} with a full "
         f"14:00 quote set; guard (credit/width in "
         f"[{MIN_CREDIT_FRAC:.2f},{MAX_CREDIT_FRAC:.2f}]) passes "
         f"{int(guard.sum())} and skips {int((~guard).sum())} "
         f"({(~guard).mean() * 100:.1f}%). Sessions without quotes or "
         "skipped by the guard are FLAT DAYS and stay in the daily series — "
         "the Sharpe is the account's, not the trade's. Entry credit is "
         f"already net of the ${ENTRY_GIVEUP:.02f} marketability giveup.")
    emit()

    all_days = j.index.astype(str)
    daily_frames: dict[str, pd.Series] = {}
    emit("| exit | friction | sets | trades | net bp/day (traded) | t | win% "
         "| worst day $ | ann ret % | Sharpe | maxDD % | alpha vs QQQ %/yr "
         "| beta QQQ | alpha vs SPY %/yr | beta SPY |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for exit_name, pnl_series, valid_idx in (
            ("intrinsic", pnl_intr, quoted.index),
            ("15:30 close-out", pnl_1530, have_1530)):
        for fric in FRICTIONS:
            for nsets in SETS:
                pnl = (pnl_series - fric).where(guard, 0.0)
                pnl = pnl.reindex(j.index).fillna(0.0)
                pnl.loc[~pnl.index.isin(valid_idx)] = 0.0
                dollars = pnl * 100 * nsets
                ret = (dollars / ALLOC).rename("ret")
                ret.index = all_days
                traded = dollars[dollars != 0.0]
                bp_day = (pnl[pnl != 0.0] / quoted["px1000"].reindex(pnl[pnl != 0.0].index) * 1e4)
                curve = ALLOC + dollars.cumsum()
                peak = curve.cummax()
                mdd = float(((peak - curve) / peak).max() * 100)
                years = len(all_days) / 252
                ann_ret = float((curve.iloc[-1] / ALLOC) ** (1 / years) - 1) * 100 if curve.iloc[-1] > 0 else float("nan")
                sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252)) if ret.std(ddof=1) > 0 else 0.0
                a_q, b_q, _ = alpha_beta(ret, bench_daily("QQQ"))
                a_s, b_s, _ = alpha_beta(ret, bench_daily("SPY"))
                emit(f"| {exit_name} | {fric * 100:.0f}c | {nsets} | {len(traded)} "
                     f"| {bp_day.mean():+.2f} | {tstat(bp_day.to_numpy()):+.2f} "
                     f"| {(traded > 0).mean() * 100:.0f} | {traded.min():+,.0f} "
                     f"| {ann_ret:+.1f} | {sharpe:+.2f} | {mdd:.1f} "
                     f"| {a_q:+.1f} | {b_q:+.3f} | {a_s:+.1f} | {b_s:+.3f} |")
                if exit_name == "intrinsic" and fric == 0.04 and nsets == 5:
                    daily_frames["fly"] = ret
    emit()

    emit("## By year — intrinsic exit, 4c friction, 5 sets")
    emit()
    ret = daily_frames["fly"]
    yr = ret.index.str[:4]
    emit("| year | sessions | traded | net $ | Sharpe | worst day $ |")
    emit("|---|---|---|---|---|---|")
    for y in sorted(set(yr)):
        r = ret[yr == y]
        d = r * ALLOC
        t = d[d != 0]
        sh = float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 0 else 0.0
        emit(f"| {y} | {len(r)} | {len(t)} | {d.sum():+,.0f} | {sh:+.2f} "
             f"| {d.min():+,.0f} |")
    emit()
    emit("The registered 15:50 exit sits between the 15:30 close-out and "
         "intrinsic rows. Selection honesty: this cell was chosen by the "
         "2026-08-10 sweep (best of 9) before registration; the pre-reg's "
         "own discounted prior is +2.5-2.8bp/day gross.")

    ret_out = daily_frames["fly"]
    pd.DataFrame({"d": ret_out.index, "ret": ret_out.to_numpy()}).to_parquet(
        CACHE / "account_daily_fly.parquet")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_fly_account.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"fly_account_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
