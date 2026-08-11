"""Monte Carlo confirmation layer for the gff gate — the two tests
walk-forward cannot provide.

Matthew's question: walk-forward is the deployment claim, but is Monte
Carlo needed to CONFIRM the edge exists? The MCs already run (P-shuffle
null p=0.003, train-label placebo clean) test selection-vs-random and
pipeline-vs-noise. What walk-forward still leaves open:

  one-path variance   the +8.8bp lift and Sharpe 1.24->1.57 are single
                      numbers off a single pass. (a) PAIRED stationary
                      block bootstrap of the daily OOS return series
                      (mean block 10 days, 2,000 draws) puts a CI on the
                      Sharpe difference and on maxDD; (b) combinatorial
                      year-block CV (every pair of the 8 OOS years held
                      out, trained on the other six, C(8,2)=28 folds)
                      turns the lift into a DISTRIBUTION. The CPCV caveat
                      is stated where it belongs: folds that train on
                      later years measure signal STATIONARITY, not
                      deployability — walk-forward remains the
                      deployment claim.

Labels are same-morning one-minute returns, so there is no label-horizon
overlap across days and no purging is required beyond the year-block
boundaries themselves; features are strictly backward-looking.

Run: python scripts/research_gff_gate_mc.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402

from research_gff_decade import spy_daily_ret  # noqa: E402
from research_gff_gate import (  # noqa: E402
    FEATS,
    PRIMARY_TAU,
    TEST_YEARS,
    build_panel,
)

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
NOTES = STUDY / "notes"
COST_BP = 10.0
SEED = 20260811
N_BOOT = 2000
MEAN_BLOCK = 10


def hgb() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.06, max_iter=300,
        min_samples_leaf=80, l2_regularization=1.0, max_bins=127,
        early_stopping=False, random_state=20260810)


def daily_series(frame: pd.DataFrame, days: list[str]) -> np.ndarray:
    net = frame["bp"] - COST_BP
    day_ret = (net * 0.25 / 1e4).groupby(frame["date"]).sum()
    return day_ret.reindex(days).fillna(0.0).to_numpy()


def sharpe(r: np.ndarray) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0


def maxdd(r: np.ndarray) -> float:
    eq = np.cumprod(1 + r)
    return float((1 - eq / np.maximum.accumulate(eq)).max() * 100)


def stationary_indices(n: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap index sequence."""
    idx = np.empty(n, dtype=int)
    i = 0
    while i < n:
        start = rng.integers(0, n)
        length = rng.geometric(1 / MEAN_BLOCK)
        for k in range(length):
            if i >= n:
                break
            idx[i] = (start + k) % n
            i += 1
    return idx


def main() -> None:
    rng = np.random.default_rng(SEED)
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    ev, sel = build_panel()

    # ---- walk-forward P (the deployment path), for the bootstrap ---------
    p = pd.Series(np.nan, index=ev.index)
    for y in TEST_YEARS:
        tr, te = ev["year"] < y, ev["year"] == y
        if te.sum() == 0 or tr.sum() < 300:
            continue
        m = hgb()
        m.fit(ev.loc[tr, FEATS], (ev.loc[tr, "bp"] > 0).astype(int))
        p.loc[te] = m.predict_proba(ev.loc[te, FEATS])[:, 1]
    ev["p"] = p
    j = sel.merge(ev[["symbol", "date", "p"]], on=["symbol", "date"],
                  how="left")
    oos = j.dropna(subset=["p"])
    spy = spy_daily_ret()
    days = [d for d in spy.index if "2019-01-01" <= d <= max(oos["date"])]
    r_un = daily_series(oos, days)
    r_gt = daily_series(oos[oos["p"] >= PRIMARY_TAU], days)

    emit(f"# gff gate Monte Carlo layer — {STAMP}")
    emit()
    emit(f"Point estimates (walk-forward, @10bp): ungated Sharpe "
         f"{sharpe(r_un):+.2f} / maxDD {maxdd(r_un):.1f}%, gated "
         f"{sharpe(r_gt):+.2f} / {maxdd(r_gt):.1f}%.")
    emit()

    # ---- paired stationary block bootstrap --------------------------------
    d_sh, g_sh, g_dd, u_dd = [], [], [], []
    for _ in range(N_BOOT):
        idx = stationary_indices(len(days), rng)
        d_sh.append(sharpe(r_gt[idx]) - sharpe(r_un[idx]))
        g_sh.append(sharpe(r_gt[idx]))
        g_dd.append(maxdd(r_gt[idx]))
        u_dd.append(maxdd(r_un[idx]))
    d_sh = np.array(d_sh)
    g_sh = np.array(g_sh)
    emit("## Paired stationary block bootstrap (2,000 draws, mean block "
         f"{MEAN_BLOCK}d, same days both books)")
    emit()
    emit(f"- Sharpe DIFFERENCE gated-ungated: mean {d_sh.mean():+.2f}, "
         f"95% CI [{np.percentile(d_sh, 2.5):+.2f}, "
         f"{np.percentile(d_sh, 97.5):+.2f}], "
         f"P(diff > 0) = {(d_sh > 0).mean():.3f}")
    emit(f"- gated Sharpe: 95% CI [{np.percentile(g_sh, 2.5):+.2f}, "
         f"{np.percentile(g_sh, 97.5):+.2f}]; "
         f"P(gated Sharpe > 1.0) = {(g_sh > 1.0).mean():.3f}")
    emit(f"- gated maxDD: median {np.median(g_dd):.1f}%, "
         f"95% CI [{np.percentile(g_dd, 2.5):.1f}, "
         f"{np.percentile(g_dd, 97.5):.1f}] "
         f"(ungated median {np.median(u_dd):.1f}%)")
    emit()

    # ---- combinatorial year-block CV --------------------------------------
    emit("## Combinatorial year-block CV (C(8,2) = 28 folds; train = the "
         "other six years)")
    emit()
    emit("Folds training on LATER years measure stationarity, not "
         "deployability — walk-forward stays the deployment claim. Purge "
         "is the year boundary itself (same-morning labels, backward "
         "features; no horizon overlap).")
    emit()
    lifts, kept_pcts = [], []
    yrs = TEST_YEARS
    ev_cv = ev[ev["year"] >= "2019"]
    for test_pair in combinations(yrs, 2):
        te = ev_cv["year"].isin(test_pair)
        tr_full = ev["year"].isin([y for y in yrs if y not in test_pair]
                                  ) | (ev["year"] < "2019")
        m = hgb()
        m.fit(ev.loc[tr_full, FEATS],
              (ev.loc[tr_full, "bp"] > 0).astype(int))
        pcv = pd.Series(np.nan, index=ev.index)
        pcv.loc[ev_cv[te].index] = m.predict_proba(
            ev_cv.loc[te, FEATS])[:, 1]
        jj = sel.merge(ev.assign(pcv=pcv)[["symbol", "date", "pcv"]],
                       on=["symbol", "date"], how="left").dropna(
            subset=["pcv"])
        un = (jj["bp"] - COST_BP).mean()
        gt = (jj.loc[jj["pcv"] >= PRIMARY_TAU, "bp"] - COST_BP).mean()
        lifts.append(gt - un)
        kept_pcts.append((jj["pcv"] >= PRIMARY_TAU).mean() * 100)
    lifts = np.array(lifts)
    emit(f"- lift distribution over 28 folds: mean {lifts.mean():+.1f}bp, "
         f"median {np.median(lifts):+.1f}, min {lifts.min():+.1f}, "
         f"max {lifts.max():+.1f}, sd {lifts.std(ddof=1):.1f}")
    emit(f"- folds with positive lift: {(lifts > 0).sum()}/28; "
         f"mean keep {np.mean(kept_pcts):.0f}%")
    emit(f"- walk-forward point estimate sits at the "
         f"{(lifts < 8.8).mean() * 100:.0f}th percentile of the CV "
         "distribution")
    emit()
    emit("Reading: the walk-forward number is one draw from this "
         "distribution; if the distribution is positive nearly everywhere, "
         "the edge is a property of the panel, not of one lucky path.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_gff_gate_mc.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_gate_mc_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
