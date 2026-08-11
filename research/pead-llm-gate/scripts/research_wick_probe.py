"""The frozen-encoder probe — does the 3090's tape encoder beat hand
features on the wick entry-gate task?

The comparison the brief §4 ordered: the mtape SSL encoder (trained on
2022-2023 tape only, research_mtape_pretrain.py) supplies a frozen 64-d
embedding of each wick event's first 75 bars; hand features are the wick
study's own (features_at, +75). Same task (P(T+1 continuation)), same
walk-forward, and ONLY test years 2024-2026 — the encoder must be
strictly past relative to every scored event, so earlier years are
excluded from the comparison entirely.

Rows, pre-declared:
  A  GBM on hand features               (the incumbent)
  B  logistic on embeddings             (encoder alone)
  C  GBM on hand features + embeddings  (does the encoder ADD anything)
Verdict metric: pooled 2024-26 AUC; C-minus-A is the headline. A placebo
row runs the identical probe with a RANDOM-weight encoder — representation
value must beat untrained-network value, not just zero.

Run: python scripts/research_wick_probe.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[2] / "backend"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from research_wickout import (  # noqa: E402
    build_paths,
    features_at,
    full_sides,
    load_minutes,
    panel,
    r_hold,
    run_rule,
)

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
NOTES = SCRIPTS.parent / "notes"
EMB_F = (SCRIPTS.parents[1] / "minute-tape" / "cache"
         / "wick_embeddings.parquet")
EMB_RND_F = (SCRIPTS.parents[1] / "minute-tape" / "cache"
             / "wick_embeddings_random.parquet")
DECIDE_BARS = 75
TEST_YEARS = (2024, 2025, 2026)


def gbm():
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=150, learning_rate=0.08,
        min_samples_leaf=40, random_state=7)


def wf_auc(X: np.ndarray, y: np.ndarray, yrs: np.ndarray,
           model_fn) -> dict:
    out = {}
    pooled_p, pooled_y = [], []
    for t in TEST_YEARS:
        tr, te = yrs < t, yrs == t
        if te.sum() == 0 or tr.sum() < 200:
            continue
        m = model_fn()
        m.fit(X[tr], y[tr])
        pr = m.predict_proba(X[te])[:, 1]
        out[t] = roc_auc_score(y[te], pr) if len(np.unique(y[te])) > 1 else np.nan
        pooled_p.append(pr)
        pooled_y.append(y[te])
    out["pooled"] = roc_auc_score(np.concatenate(pooled_y),
                                  np.concatenate(pooled_p))
    return out


def main() -> None:
    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    p = panel()
    q = load_minutes(p)
    side = q["side"].to_numpy() if "side" in q else full_sides(q)
    paths = build_paths(q, side)
    em = q["em"].to_numpy()
    move = q["move_pct"].to_numpy()
    run5 = q["run5d"].to_numpy()
    dv = q["dv"].to_numpy()
    yr = q["year"].to_numpy().astype(int)
    base_net, _ = run_rule(paths, r_hold())

    feats, keep = [], []
    for i, pp in enumerate(paths):
        if pp is None or not np.isfinite(base_net[i]):
            continue
        f = features_at(pp, DECIDE_BARS, False, em[i], move[i], run5[i], dv[i])
        if f is None:
            continue
        feats.append(f)
        keep.append(i)
    keep = np.array(keep)
    Xh = np.nan_to_num(pd.DataFrame(feats).to_numpy(),
                       nan=0.0, posinf=0.0, neginf=0.0)
    y = (base_net[keep] > 0).astype(int)
    yrs = yr[keep]

    emb = pd.read_parquet(EMB_F)
    key = pd.DataFrame({"symbol": q["symbol"].to_numpy()[keep],
                        "date": q["date"].astype(str).to_numpy()[keep],
                        "row": np.arange(len(keep))})
    j = key.merge(emb, on=["symbol", "date"], how="left")
    ecols = [c for c in emb.columns if c.startswith("e")]
    Xe = j[ecols].to_numpy()
    has_e = np.isfinite(Xe).all(axis=1)

    m = has_e
    Xh_, Xe_, y_, yrs_ = Xh[m], Xe[m], y[m], yrs[m]
    Xc_ = np.hstack([Xh_, Xe_])

    emit(f"# Frozen-encoder probe vs hand features — {STAMP}")
    emit()
    emit(f"{int(m.sum()):,} events with both feature sets (of {len(keep):,}); "
         f"test years 2024-26 only (encoder trained on 2022-23 tape). "
         f"Target: T+1 continuation.")
    emit()
    emit("| model | 2024 | 2025 | 2026 | pooled |")
    emit("|---|---|---|---|---|")

    def logit():
        return Pipeline([("sc", StandardScaler()),
                         ("lr", LogisticRegression(C=0.5, max_iter=2000))])

    rows = [
        ("A: GBM hand features", Xh_, gbm),
        ("B: logistic on embeddings", Xe_, logit),
        ("C: GBM hand + embeddings", Xc_, gbm),
    ]
    results = {}
    for label, X_, fn in rows:
        r = wf_auc(X_, y_, yrs_, fn)
        results[label[0]] = r
        emit(f"| {label} | " + " | ".join(
            f"{r.get(t, float('nan')):.3f}" for t in TEST_YEARS)
            + f" | **{r['pooled']:.3f}** |")

    if EMB_RND_F.exists():
        er = pd.read_parquet(EMB_RND_F)
        jr = key.merge(er, on=["symbol", "date"], how="left")
        Xr = jr[[c for c in er.columns if c.startswith("e")]].to_numpy()[m]
        rr = wf_auc(np.nan_to_num(Xr), y_, yrs_, logit)
        emit("| placebo: logistic on RANDOM-encoder embeddings | "
             + " | ".join(f"{rr.get(t, float('nan')):.3f}"
                          for t in TEST_YEARS)
             + f" | {rr['pooled']:.3f} |")
    emit()
    d = results["C"]["pooled"] - results["A"]["pooled"]
    emit(f"Headline: C minus A = {d:+.4f} pooled AUC. "
         f"{'The encoder adds nothing the hand features lack.' if d < 0.01 else 'The encoder adds signal beyond the hand features.'}")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(SCRIPTS)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_wick_probe.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"wick_probe_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
