"""Minute-tape cross-sectional baseline — the bar the 3090 must beat.

Question, pre-stated: is there cross-sectional predictability at the
30-minute horizon in the top-100 liquid names that survives our costs and
latency? Honest prior, stated up front (brief §4): gross IC ~0.01-0.03
territory and net-negative after retail frictions is the LIKELY outcome —
the deliverable is the measured yes/no that decides whether GPU sequence
models have anything to chase.

Design:
  panel      the mtape cache (top-100 monthly liquid, 2022-2026, SIP
             minute bars with vwap/trade_count).
  sampling   decision minutes on a 15-min grid, 10:00 .. 15:00 ET
             (auction zones excluded on both ends).
  target     forward 30-min return, cross-sectionally DEMEANED per
             (day, minute) — market moves net out; this is relative
             prediction, beta-free by construction.
  features   trailing 5/15/30/60m returns; 30m realized vol; distance
             from session vwap (cumulative dollar-vwap); range position;
             volume and trade-count z vs the SAME NAME's same-minute mean
             over the trailing 20 sessions (time-of-day seasonality
             removed); minute-of-day. All from bars at or before the
             decision minute — nothing forward.
  model      HistGradientBoostingRegressor (depth 3, lr 0.06, 300 iters,
             min_samples_leaf 200, l2 1.0) — one model, no tuning sweep.
  protocol   expanding walk-forward by year: train 2022-23 -> 2024,
             +2024 -> 2025, +2025 -> 2026.
  readout    per-year rank IC (Spearman per cross-section, then mean/t);
             decile long-short at each grid minute held 30m, gross and
             net (6bp/side primary, 3bp stress-tight, 10bp stress-wide);
             train-label placebo (shuffled within day) x3.
  latency    the live free tier sees SIP 15 minutes late; a real
             implementation trades on IEX sight. This study measures the
             UPPER BOUND (full SIP at zero delay); if the upper bound is
             net-negative, the latency question never arises.

Run:  python scripts/research_mtape_baseline.py score [--smoke]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402
from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

GRID = list(range(30, 331, 15))          # minutes since 09:30: 10:00..15:00
FWD = 30
VOLZ_LOOKBACK = 20
SEED = 20260811
FEATS = ["r5", "r15", "r30", "r60", "rv30", "vwap_dist", "range_pos",
         "volz", "tcz", "minute"]
COSTS = (3.0, 6.0, 10.0)                 # per side, bp


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def month_files(smoke: bool) -> list[Path]:
    files = sorted(CACHE.glob("mtape_*.parquet"))
    return files[:8] if smoke else files


def build_samples(smoke: bool) -> pd.DataFrame:
    """Grid-sampled feature/target rows from the per-month caches.

    Trailing volume/trade-count state (same-minute 20-session means) is
    carried per symbol across months so z-scores never look forward."""
    volhist: dict[str, list[np.ndarray]] = {}
    tchist: dict[str, list[np.ndarray]] = {}
    out: list[pd.DataFrame] = []
    for f in month_files(smoke):
        m = pd.read_parquet(f)
        for day in sorted(m["date"].unique()):
            d = m[m["date"] == day]
            piv_c = d.pivot_table(index="minute", columns="symbol", values="c")
            piv_v = d.pivot_table(index="minute", columns="symbol", values="v")
            piv_n = d.pivot_table(index="minute", columns="symbol", values="n")
            piv_h = d.pivot_table(index="minute", columns="symbol", values="h")
            piv_l = d.pivot_table(index="minute", columns="symbol", values="l")
            piv_vw = d.pivot_table(index="minute", columns="symbol", values="vw")
            idx = np.arange(0, 390)
            C = piv_c.reindex(idx).ffill()
            V = piv_v.reindex(idx).fillna(0.0)
            N = piv_n.reindex(idx).fillna(0.0)
            H = piv_h.reindex(idx)
            L = piv_l.reindex(idx)
            VW = piv_vw.reindex(idx)
            dollars = (VW.fillna(C) * V).cumsum()
            shares = V.cumsum().replace(0, np.nan)
            sess_vwap = dollars / shares
            ret = C / C.shift(1) - 1
            rv30 = ret.rolling(30, min_periods=15).std() * np.sqrt(390 * 252)
            hi = H.cummax()
            lo = L.cummin()
            rng = (hi - lo).replace(0, np.nan)

            vol_mu, vol_sd, tc_mu, tc_sd = {}, {}, {}, {}
            for sym in C.columns:
                hist = volhist.get(sym, [])
                if len(hist) >= 5:
                    a = np.stack(hist[-VOLZ_LOOKBACK:])
                    vol_mu[sym] = a.mean(axis=0)
                    vol_sd[sym] = a.std(axis=0) + 1e-9
                hist_t = tchist.get(sym, [])
                if len(hist_t) >= 5:
                    a = np.stack(hist_t[-VOLZ_LOOKBACK:])
                    tc_mu[sym] = a.mean(axis=0)
                    tc_sd[sym] = a.std(axis=0) + 1e-9

            rows = []
            for t in GRID:
                if t + FWD >= 390:
                    continue
                c_now = C.iloc[t]
                fwd = (C.iloc[t + FWD] / c_now - 1) * 1e4
                feat = pd.DataFrame({
                    "fwd_bp": fwd,
                    "r5": (c_now / C.iloc[t - 5] - 1) * 1e4,
                    "r15": (c_now / C.iloc[t - 15] - 1) * 1e4,
                    "r30": (c_now / C.iloc[t - 30] - 1) * 1e4,
                    "r60": ((c_now / C.iloc[t - 60] - 1) * 1e4
                            if t >= 60 else np.nan),
                    "rv30": rv30.iloc[t],
                    "vwap_dist": (c_now / sess_vwap.iloc[t] - 1) * 1e4,
                    "range_pos": ((c_now - lo.iloc[t]) / rng.iloc[t]),
                })
                v_now = V.iloc[max(0, t - 29):t + 1].sum()
                n_now = N.iloc[max(0, t - 29):t + 1].sum()
                vz, tz = [], []
                for sym in C.columns:
                    if sym in vol_mu:
                        mu = vol_mu[sym][max(0, t - 29):t + 1].sum()
                        sd = vol_sd[sym][max(0, t - 29):t + 1].sum()
                        vz.append((v_now[sym] - mu) / sd)
                        mu_t = tc_mu[sym][max(0, t - 29):t + 1].sum()
                        sd_t = tc_sd[sym][max(0, t - 29):t + 1].sum()
                        tz.append((n_now[sym] - mu_t) / sd_t)
                    else:
                        vz.append(np.nan)
                        tz.append(np.nan)
                feat["volz"] = vz
                feat["tcz"] = tz
                feat["minute"] = float(t)
                feat["symbol"] = feat.index
                feat["date"] = day
                feat["fwd_dm"] = feat["fwd_bp"] - feat["fwd_bp"].mean()
                rows.append(feat.reset_index(drop=True))
            out.append(pd.concat(rows, ignore_index=True))

            for sym in C.columns:
                volhist.setdefault(sym, []).append(
                    V[sym].to_numpy(copy=True))
                tchist.setdefault(sym, []).append(N[sym].to_numpy(copy=True))
                volhist[sym] = volhist[sym][-VOLZ_LOOKBACK:]
                tchist[sym] = tchist[sym][-VOLZ_LOOKBACK:]
    df = pd.concat(out, ignore_index=True)
    return df.dropna(subset=["fwd_dm", "r5", "r15", "r30"])


def model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        max_depth=3, learning_rate=0.06, max_iter=300,
        min_samples_leaf=200, l2_regularization=1.0, max_bins=127,
        early_stopping=False, random_state=SEED)


def evaluate(df: pd.DataFrame, pred_col: str, lines: list[str],
             label: str) -> None:
    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    df = df.dropna(subset=[pred_col])
    emit(f"### {label}")
    emit()
    emit("| year | cross-sections | mean rank IC | IC t | LS gross bp/30m "
         "| net@3/side | net@6/side | net@10/side |")
    emit("|---|---|---|---|---|---|---|---|")
    for y, g in df.groupby(df["date"].str[:4]):
        ics, ls = [], []
        for (_, _), cs in g.groupby(["date", "minute"]):
            if len(cs) < 30:
                continue
            ic = spearmanr(cs[pred_col], cs["fwd_dm"]).statistic
            if np.isfinite(ic):
                ics.append(ic)
            k = max(5, len(cs) // 10)
            top = cs.nlargest(k, pred_col)["fwd_dm"].mean()
            bot = cs.nsmallest(k, pred_col)["fwd_dm"].mean()
            ls.append((top - bot) / 2)          # per-dollar-deployed
        ics_, ls_ = np.array(ics), np.array(ls)
        cells = " | ".join(f"{ls_.mean() - 2 * c:+.1f}" for c in COSTS)
        emit(f"| {y} | {len(ls_):,} | {ics_.mean():+.4f} "
             f"| {tstat(ics_):+.1f} | {ls_.mean():+.1f} | {cells} |")
    emit()


def stage_score(args) -> None:
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    df = build_samples(args.smoke)
    tag = "SMOKE (first 8 months only — plumbing check, NOT results)" \
        if args.smoke else "full panel"
    emit(f"# Minute-tape cross-sectional baseline — {STAMP} ({tag})")
    emit()
    emit(f"{len(df):,} samples, {df['date'].nunique():,} sessions, "
         f"{df['symbol'].nunique()} names, grid 10:00-15:00 every 15m, "
         f"target fwd-{FWD}m demeaned. Model: HGB regressor, one config, "
         "no sweep. LS = decile top-minus-bottom half-spread-deployed; "
         "costs charged per SIDE x2 (entry+exit crossings).")
    emit()

    test_years = ["2024", "2025", "2026"] if not args.smoke else ["2022"]
    df["year"] = df["date"].str[:4]
    df["p"] = np.nan
    rng = np.random.default_rng(SEED)
    placebo_cols = []
    for y in test_years:
        tr = df["year"] < y if not args.smoke else df["date"] < "2022-05-01"
        te = df["year"] == y if not args.smoke else df["date"] >= "2022-05-01"
        if tr.sum() < 10_000 or te.sum() == 0:
            continue
        m = model()
        m.fit(df.loc[tr, FEATS], df.loc[tr, "fwd_dm"])
        df.loc[te, "p"] = m.predict(df.loc[te, FEATS])
        for i in range(3):
            col = f"p_placebo{i}"
            if col not in df.columns:
                df[col] = np.nan
                placebo_cols.append(col)
            ysh = df.loc[tr].groupby("date")["fwd_dm"].transform(
                lambda v: rng.permutation(v.to_numpy()))
            mp = model()
            mp.fit(df.loc[tr, FEATS], ysh)
            df.loc[te, col] = mp.predict(df.loc[te, FEATS])

    emit("## Real model (walk-forward)")
    emit()
    evaluate(df, "p", lines, "prediction = HGB")
    emit("## Placebo (train labels shuffled within day, same pipeline)")
    emit()
    for col in placebo_cols:
        evaluate(df, col, lines, col)

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append(f"_Provenance: `research_mtape_baseline.py score"
                 f"{' --smoke' if args.smoke else ''}` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"mtape_baseline_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["score"])
    ap.add_argument("--smoke", action="store_true",
                    help="first 8 months only; plumbing check")
    args = ap.parse_args()
    stage_score(args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
