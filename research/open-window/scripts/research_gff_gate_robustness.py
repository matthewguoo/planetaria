"""Overfit detectors for the gff gate — placebo, seeds, ablation.

Matthew's order after the gate landed: a good strat must prove it is not
overfit. Three detectors, each answering one way the +8.8bp lift could be
fake:

  placebo    refit the ENTIRE walk-forward with training labels shuffled
             within year (marginals kept, signal destroyed; test labels
             stay real). If the pipeline still produces lift, the pipeline
             itself manufactures edge — leakage or selection, and nothing
             downstream is trustworthy. 5 draws.
  seeds      the primary cell across HGB random seeds. A result that
             moves with the seed is variance, not signal.
  ablation   drop the top features (n_fades_today; then also ret1,
             turn_bp). An edge that dies with one column is that column's
             artifact, not a strategy.

Everything reuses build_panel/FEATS from research_gff_gate — same panel,
same protocol, HGB only (the primary model). Primary readout everywhere:
both legs, tau=0.50, net@10 lift vs ungated on the registered selection.

Run: python scripts/research_gff_gate_robustness.py
"""

from __future__ import annotations

import subprocess
import sys
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
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402

from research_gff_gate import FEATS, PRIMARY_TAU, TEST_YEARS, build_panel  # noqa: E402
from research_gff_decade import tstat  # noqa: E402

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
NOTES = STUDY / "notes"
COST_BP = 10.0


def hgb(seed: int) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.06, max_iter=300,
        min_samples_leaf=80, l2_regularization=1.0, max_bins=127,
        early_stopping=False, random_state=seed)


def run_wf(ev: pd.DataFrame, sel: pd.DataFrame, feats: list[str],
           seed: int = 20260810,
           shuffle_train: np.random.Generator | None = None) -> dict:
    """Lean HGB-only walk-forward; returns primary-cell readout."""
    p = pd.Series(np.nan, index=ev.index)
    for y in TEST_YEARS:
        tr = ev["year"] < y
        te = ev["year"] == y
        if te.sum() == 0 or tr.sum() < 300:
            continue
        ytr = (ev.loc[tr, "bp"] > 0).astype(int)
        if shuffle_train is not None:
            ytr = ytr.copy()
            yrs = ev.loc[tr, "year"]
            for yy in yrs.unique():
                m = (yrs == yy).to_numpy()
                vals = ytr.to_numpy().copy()
                vals[m] = shuffle_train.permutation(vals[m])
                ytr = pd.Series(vals, index=ytr.index)
        model = hgb(seed)
        model.fit(ev.loc[tr, feats], ytr)
        p.loc[te] = model.predict_proba(ev.loc[te, feats])[:, 1]
    ev = ev.assign(p=p)
    j = sel.merge(ev[["symbol", "date", "p"]], on=["symbol", "date"],
                  how="left").dropna(subset=["p"])
    ungated = j["bp"] - COST_BP
    kept = j.loc[j["p"] >= PRIMARY_TAU, "bp"] - COST_BP
    return {"n": len(j), "kept": len(kept),
            "keep_pct": len(kept) / len(j) * 100,
            "ungated": ungated.mean(), "gated": kept.mean(),
            "lift": kept.mean() - ungated.mean(),
            "t_gated": tstat(kept.to_numpy())}


def main() -> None:
    ev, sel = build_panel()
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# gff gate robustness: placebo, seeds, ablation — {STAMP}")
    emit()
    emit("Primary readout everywhere: HGB, both legs, tau=0.50, net@10 "
         "lift vs ungated on the registered selection, walk-forward "
         "2019-26. Reference from the gate note: +8.8bp lift at 61% keep.")
    emit()

    base = run_wf(ev, sel, FEATS)
    emit(f"reference rerun: lift {base['lift']:+.1f}bp at "
         f"{base['keep_pct']:.0f}% keep (t gated {base['t_gated']:+.2f})")
    emit()

    emit("## Placebo: training labels shuffled within year (5 draws)")
    emit()
    emit("If any draw shows lift like the real one, the pipeline "
         "manufactures edge.")
    emit()
    emit("| draw | keep% | lift bp |")
    emit("|---|---|---|")
    rng = np.random.default_rng(7)
    placebo = []
    for i in range(5):
        r = run_wf(ev, sel, FEATS, shuffle_train=rng)
        placebo.append(r["lift"])
        emit(f"| {i + 1} | {r['keep_pct']:.0f} | {r['lift']:+.1f} |")
    emit()
    emit(f"placebo mean {np.mean(placebo):+.1f}bp (real: {base['lift']:+.1f})")
    emit()

    emit("## Seed sensitivity")
    emit()
    emit("| seed | keep% | lift bp | t gated |")
    emit("|---|---|---|---|")
    for seed in (20260810, 1, 7, 42, 777):
        r = run_wf(ev, sel, FEATS, seed=seed)
        emit(f"| {seed} | {r['keep_pct']:.0f} | {r['lift']:+.1f} "
             f"| {r['t_gated']:+.2f} |")
    emit()

    emit("## Ablation (drop the model's favorite features)")
    emit()
    emit("| features | keep% | lift bp | t gated |")
    emit("|---|---|---|---|")
    for label, drop in (
            ("all 23 (reference)", []),
            ("- n_fades_today", ["n_fades_today"]),
            ("- n_fades_today, ret1", ["n_fades_today", "ret1"]),
            ("- n_fades_today, ret1, turn_bp",
             ["n_fades_today", "ret1", "turn_bp"]),
            ("- top5 (also pm_dollar_log, spy_rv20)",
             ["n_fades_today", "ret1", "turn_bp", "pm_dollar_log",
              "spy_rv20"])):
        feats = [f for f in FEATS if f not in drop]
        r = run_wf(ev, sel, feats)
        emit(f"| {label} | {r['keep_pct']:.0f} | {r['lift']:+.1f} "
             f"| {r['t_gated']:+.2f} |")
    emit()

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append(f"_Provenance: `research_gff_gate_robustness.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_gate_robustness_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
