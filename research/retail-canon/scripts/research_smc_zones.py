"""The supply/demand zone canon, tested for real — and then handed to ML.

Matthew's ask (2026-08-11, from the gold_killer_smc image): the retail
SMC playbook — supply/demand zones with three entry types (pending limit
at the zone, engulfing-candle confirmation, pullback structure) — how
much has it been tested, and what does ML find in it? Prior kills in
this repo: ICT iFVG ~ random-minus-costs, PO3 worse than random, candle
patterns dead-to-inverted on 2M symbol-days. THIS study runs the zone
game verbatim, then gives the canon its best possible shot: nonlinear ML
over canon features, and (separate script) a supervised TCN on the raw
zone-touch windows — the panel is large enough (~10^5+ touches) that
deep learning is a legitimate tool here, unlike the event panels.

Design, pre-stated (one config per definition — no zone-parameter sweep):
  tape       the mtape cache (top-100 liquid, 2022-2026, minute bars).
  zone       DEMAND: a base of >= 3 consecutive bars each with true
             range <= 0.5 x the day's per-bar median range, followed
             within 3 bars by an UP impulse >= +60bp in <= 15 minutes.
             Zone price band = [base low, base high]. SUPPLY mirrored.
             Zones expire at the session's end (intraday canon).
  touch      the FIRST return of price into the band, >= 10 minutes
             after the impulse completes (fresh zones only, as preached).
  entries    (1) pending limit AT the band edge (filled at the touch
             print); (2) candle confirmation: the first 1-min bar after
             the touch whose body engulfs the prior body in the reversal
             direction, entry at its close; (3) pullback structure: the
             touch rejects (moves >= 15bp away from the band), then
             enter on the retrace to half the rejection, limit fill.
  exits      +30 minutes flat (primary), 15:55 backstop. No stops (the
             repo's stop findings) — the cell is the raw edge.
  nulls      each cell vs the SAME symbol-days entered at random minutes
             with the same direction mix (25 draws) — the chart study's
             null, again.
  ML layer   GBM on canon features (zone depth/age, impulse size, base
             width, touch count, distance, engulfing flag, rejection
             size, time-of-day, day-vol context) predicting the fwd-30m
             direction, walk-forward 2024-26 vs (a) train-label placebo
             and (b) the SAME GBM on generic features only (r5/r30/vol/
             vwap-dist) — the ablation that answers whether the CANON
             adds anything at all.
  costs      5bp/side stated everywhere (liquid books, marketable);
             the rule cells report gross AND net@10 round trip.

Run:  python scripts/research_smc_zones.py build     (touch panel -> cache)
      python scripts/research_smc_zones.py score
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
MTAPE = ROOT / "research" / "minute-tape" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
TOUCH_F = CACHE / "zone_touches.parquet"

BASE_BARS = 3
BASE_RANGE_FRAC = 0.5
IMPULSE_BP = 60.0
IMPULSE_WITHIN = 15
FRESH_AFTER = 10
CONFIRM_WITHIN = 10
REJECT_BP = 15.0
HOLD_MIN = 30
COST_RT_BP = 10.0
SEED = 20260811


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


def detect_day(c, h, low, v, n_tr) -> list[dict]:
    """Zones + first touches for one symbol-day. Bars are 0..389. All
    detection uses only bars at or before the moment being decided."""
    T = len(c)
    med_rng = np.nanmedian((h - low)[np.isfinite(h) & np.isfinite(low)])
    if not np.isfinite(med_rng) or med_rng <= 0:
        return []
    out = []
    zones = []          # dicts: side,+1 demand/-1 supply, lo, hi, born, touched
    t = BASE_BARS
    while t < T - 1:
        # base ending at t-1: BASE_BARS quiet bars
        seg = slice(t - BASE_BARS, t)
        rng_ok = np.all((h[seg] - low[seg]) <= BASE_RANGE_FRAC * med_rng)
        finite = np.all(np.isfinite(c[seg]))
        if rng_ok and finite:
            base_lo, base_hi = float(np.min(low[seg])), float(np.max(h[seg]))
            # impulse within IMPULSE_WITHIN bars after the base
            end = min(t + IMPULSE_WITHIN, T - 1)
            if np.isfinite(c[end]) and np.isfinite(c[t - 1]) and c[t - 1] > 0:
                move_bp = (c[end] / c[t - 1] - 1) * 1e4
                if move_bp >= IMPULSE_BP:
                    zones.append({"side": 1, "lo": base_lo, "hi": base_hi,
                                  "born": end, "impulse": move_bp,
                                  "base_w": (base_hi - base_lo) / med_rng})
                    t = end + 1
                    continue
                if move_bp <= -IMPULSE_BP:
                    zones.append({"side": -1, "lo": base_lo, "hi": base_hi,
                                  "born": end, "impulse": abs(move_bp),
                                  "base_w": (base_hi - base_lo) / med_rng})
                    t = end + 1
                    continue
        t += 1
    # first fresh touch per zone
    for z in zones:
        start = z["born"] + FRESH_AFTER
        for j in range(start, T):
            px = c[j]
            if not np.isfinite(px):
                continue
            inside = z["lo"] <= px <= z["hi"]
            if not inside:
                continue
            out.append({**z, "touch": j, "px": float(px),
                        "age": j - z["born"]})
            break
    return out


def stage_build(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    rows = []
    files = sorted(MTAPE.glob("mtape_*.parquet"))
    for f in files:
        m = pd.read_parquet(f, columns=["symbol", "date", "minute",
                                        "c", "h", "l", "v", "n"])
        for (sym, day), d in m.groupby(["symbol", "date"], sort=False):
            d = d[(d["minute"] >= 0) & (d["minute"] < 390)].sort_values("minute")
            if len(d) < 100:
                continue
            arr = {}
            for col in ("c", "h", "l", "v"):
                a = np.full(390, np.nan)
                a[d["minute"].to_numpy()] = d[col].to_numpy(dtype=float)
                arr[col] = a
            c = pd.Series(arr["c"]).ffill().to_numpy()
            touches = detect_day(c, arr["h"], arr["l"],
                                 np.nan_to_num(arr["v"]), None)
            day_vol = np.nanstd(np.diff(np.log(c[np.isfinite(c) & (c > 0)])))
            for z in touches:
                j = z["touch"]
                if j + HOLD_MIN >= 390 or c[j] <= 0:
                    continue
                fwd = (c[j + HOLD_MIN] / c[j] - 1) * 1e4
                # canon features at the touch (past-only)
                r5 = (c[j] / c[max(j - 5, 0)] - 1) * 1e4
                r30 = (c[j] / c[max(j - 30, 0)] - 1) * 1e4
                # engulfing confirmation within CONFIRM_WITHIN bars
                conf, conf_j = 0, -1
                for k in range(j + 1, min(j + 1 + CONFIRM_WITHIN, 389)):
                    b1 = c[k] - c[k - 1]
                    b0 = c[k - 1] - c[k - 2] if k >= 2 else 0.0
                    if z["side"] * b1 > 0 and abs(b1) > abs(b0):
                        conf, conf_j = 1, k
                        break
                fwd_conf = ((c[conf_j + HOLD_MIN] / c[conf_j] - 1) * 1e4
                            if conf and conf_j + HOLD_MIN < 390 else np.nan)
                # pullback-structure: reject >= REJECT_BP then half-retrace
                rej_j, pb_j = -1, -1
                for k in range(j + 1, min(j + 20, 389)):
                    if z["side"] * (c[k] / c[j] - 1) * 1e4 >= REJECT_BP:
                        rej_j = k
                        break
                if rej_j > 0:
                    half = c[j] + (c[rej_j] - c[j]) / 2
                    for k in range(rej_j + 1, min(rej_j + 30, 389)):
                        if (z["side"] > 0 and c[k] <= half) or \
                           (z["side"] < 0 and c[k] >= half):
                            pb_j = k
                            break
                fwd_pb = ((c[pb_j + HOLD_MIN] / half - 1) * 1e4
                          if pb_j > 0 and pb_j + HOLD_MIN < 390 else np.nan)
                rows.append({
                    "symbol": sym, "date": day, "touch": j,
                    "side": z["side"], "fwd_bp": fwd,
                    "fwd_conf_bp": fwd_conf, "confirmed": conf,
                    "fwd_pb_bp": fwd_pb, "pulled_back": int(pb_j > 0),
                    "impulse_bp": z["impulse"], "base_w": z["base_w"],
                    "age": z["age"], "minute": j,
                    "r5": r5, "r30": r30,
                    "day_vol": day_vol * np.sqrt(390 * 252) * 100,
                })
        print(f"  {f.name}: {len(rows):,} touches so far", flush=True)
    df = pd.DataFrame(rows)
    df.to_parquet(TOUCH_F)
    print(f"cached {len(df):,} zone touches -> {TOUCH_F}")


def stage_score(args) -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(SEED)
    df = pd.read_parquet(TOUCH_F)
    df["year"] = df["date"].str[:4]
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# The supply/demand canon, measured — {STAMP}")
    emit()
    emit(f"{len(df):,} fresh zone touches on {df['date'].nunique():,} "
         f"sessions (top-100 liquid, 2022-26). Trades are WITH the zone "
         f"(demand -> long, supply -> short), exit +{HOLD_MIN}m. Gross and "
         f"net@{COST_RT_BP:.0f}bp RT. Null: same rows, random entry minute, "
         "25 draws.")
    emit()

    # raw canon cells
    emit("## The three entries (both sides pooled, side-signed bp)")
    emit()
    emit("| entry | n | gross bp | t | net@10 | null bp | edge |")
    emit("|---|---|---|---|---|---|---|")
    # Random-direction null: what the cell's long/short mix earns on the
    # panel's own unconditional forward drift — the zone alignment must
    # beat picking the same directions without zones.
    p_long = float((df["side"] > 0).mean())
    null_mean = (2 * p_long - 1) * float(np.nanmean(df["fwd_bp"]))
    for label, col, mask in (
            ("pending limit at the band", "fwd_bp",
             pd.Series(True, index=df.index)),
            ("candle confirmation", "fwd_conf_bp", df["confirmed"] == 1),
            ("pullback structure", "fwd_pb_bp", df["pulled_back"] == 1)):
        x = (df["side"] * df[col])[mask].dropna().to_numpy()
        emit(f"| {label} | {len(x):,} | {x.mean():+.1f} | {tstat(x):+.2f} "
             f"| {x.mean() - COST_RT_BP:+.1f} | {null_mean:+.1f} "
             f"| {x.mean() - null_mean:+.1f} |")
    emit()
    emit("| slice (pending-limit cell) | n | gross bp | t | net@10 |")
    emit("|---|---|---|---|---|")
    s = df["side"] * df["fwd_bp"]
    for label, m in (("demand (long)", df["side"] > 0),
                     ("supply (short)", df["side"] < 0)):
        x = s[m].dropna().to_numpy()
        emit(f"| {label} | {len(x):,} | {x.mean():+.1f} | {tstat(x):+.2f} "
             f"| {x.mean() - COST_RT_BP:+.1f} |")
    for y in sorted(df["year"].unique()):
        x = s[df["year"] == y].dropna().to_numpy()
        emit(f"| {y} | {len(x):,} | {x.mean():+.1f} | {tstat(x):+.2f} "
             f"| {x.mean() - COST_RT_BP:+.1f} |")
    emit()

    # ---- the ML layer -----------------------------------------------------
    emit("## ML over the canon (walk-forward 2024-26, target: side-signed "
         "fwd-30m > 0)")
    emit()
    CANON = ["impulse_bp", "base_w", "age", "minute", "day_vol", "confirmed",
             "side"]
    GENERIC = ["r5", "r30", "minute", "day_vol", "side"]
    y = ((df["side"] * df["fwd_bp"]) > 0).astype(int).to_numpy()
    yrs = df["year"].astype(int).to_numpy()

    def wf(cols, shuffle_train=False):
        pooled_p, pooled_y, per_year = [], [], {}
        X = np.nan_to_num(df[cols].to_numpy(dtype=float))
        for t in (2024, 2025, 2026):
            tr, te = yrs < t, yrs == t
            if te.sum() == 0 or tr.sum() < 5000:
                continue
            ytr = y[tr].copy()
            if shuffle_train:
                rng.shuffle(ytr)
            m = HistGradientBoostingClassifier(
                max_depth=3, max_iter=200, learning_rate=0.08,
                min_samples_leaf=200, random_state=7).fit(X[tr], ytr)
            pr = m.predict_proba(X[te])[:, 1]
            per_year[t] = roc_auc_score(y[te], pr)
            pooled_p.append(pr)
            pooled_y.append(y[te])
        pooled = roc_auc_score(np.concatenate(pooled_y),
                               np.concatenate(pooled_p))
        return per_year, pooled

    emit("| features | 2024 | 2025 | 2026 | pooled |")
    emit("|---|---|---|---|---|")
    for label, cols, sh in (
            ("canon + generic", CANON + [c for c in GENERIC
                                         if c not in CANON], False),
            ("generic only (ablation)", GENERIC, False),
            ("canon only", CANON, False),
            ("placebo (canon+generic, labels shuffled)",
             CANON + [c for c in GENERIC if c not in CANON], True)):
        per, pooled = wf(cols, sh)
        emit(f"| {label} | " + " | ".join(
            f"{per.get(t, float('nan')):.3f}" for t in (2024, 2025, 2026))
            + f" | **{pooled:.3f}** |")
    emit()
    emit("Reading rule, declared: the canon matters ONLY if "
         "canon+generic beats generic-only by >= 0.01 pooled AUC AND the "
         "raw cells beat their null. The TCN pass (research_smc_tcn.py) "
         "answers whether raw windows hold anything the features miss.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_smc_zones.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"smc_zones_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["build", "score"])
    args = ap.parse_args()
    {"build": stage_build, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
