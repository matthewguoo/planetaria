"""pead_confirmed (§3d) — the year table and slot sim its pre-registration
needs. The wick synthesis found the paying channel is the ENTRY: delay to
accept+75m (the first hour nets ~zero drift) and take only events whose
tape confirms the reaction; +81-92bp/trade on 48-61% taken vs +39
ungated. This finishes the two requirements the synthesis listed before a
pre-reg: year-by-year stability and the slot-level account sim.

Design, pre-stated (conventions are the wick study's own — nothing
re-chosen):
  panel      research_wickout panel + minute paths (1,800 events with
             tape); tape sides; bar-indexed clock (accept+75 BARS, the
             study's own convention on the evening tape).
  entry      the +75-bar tape print; exit T+1 close. Delayed return =
             (1 + gross_hold) / (1 + r75) - 1, net of the panel's 13bp;
             AH-measured 23.2bp stress row (the delayed-arm entry is an
             after-hours fill).
  gate       stage_learn's exact model (HGB depth 3, 150 iters, lr .08,
             min_leaf 40, seed 7) on features_at(+75), walk-forward
             2019-26 (train from 2016), target = T+1 continuation.
             tau family {0.45, 0.50, 0.55} reported in FULL; PRIMARY,
             declared here: tau = 0.50.
  secondary  the poor-man's mechanical rule, one config, no sweep:
             still holding >= 50% of the reaction AND above the running
             VWAP at the decision bar.
  slot sim   4 slots/night by dollar volume, 25% of allocation each,
             flat days in the series, 2019-26 window, beta vs SPY.

Run: python scripts/research_pead_confirmed.py
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
from sklearn.metrics import roc_auc_score  # noqa: E402

from research_llm_contamination import COSTS_BP, _spy_daily  # noqa: E402
from research_wickout import (  # noqa: E402
    build_paths,
    features_at,
    full_sides,
    load_minutes,
    panel,
    r_hold,
    run_rule,
    tstat,
)

ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
NOTES = SCRIPTS.parent / "notes"
DECIDE_BARS = 75
TAUS = (0.45, 0.50, 0.55)
PRIMARY_TAU = 0.50
AH_STRESS_BP = 23.2
SLOTS = 4
POSITION_FRAC = 0.25


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
    dates = q["date"].to_numpy()

    base_net, _ = run_rule(paths, r_hold())          # net from entry, decimal
    gross = base_net + COSTS_BP / 1e4
    r75 = np.full(len(paths), np.nan)
    feats, keep = [], []
    for i, pp in enumerate(paths):
        if pp is None or not np.isfinite(base_net[i]):
            continue
        j = min(pp.i_entry + DECIDE_BARS, pp.n - 1)
        r75[i] = pp.rc[j] / 100
        f = features_at(pp, DECIDE_BARS, False, em[i], move[i], run5[i], dv[i])
        if f is None:
            continue
        feats.append(f)
        keep.append(i)
    keep = np.array(keep)
    X = pd.DataFrame(feats)
    Xv = np.nan_to_num(X.to_numpy(), nan=0.0, posinf=0.0, neginf=0.0)
    yy = (base_net[keep] > 0).astype(int)
    yrs = yr[keep]

    delayed_g = (1 + gross[keep]) / (1 + r75[keep]) - 1
    dnet = delayed_g - COSTS_BP / 1e4
    dnet_ah = delayed_g - AH_STRESS_BP / 1e4

    # walk-forward gate (stage_learn's exact model)
    prob = np.full(len(keep), np.nan)
    for t in range(2019, 2027):
        tr, te = yrs < t, yrs == t
        if te.sum() == 0 or tr.sum() < 200:
            continue
        gb = HistGradientBoostingClassifier(
            max_depth=3, max_iter=150, learning_rate=0.08,
            min_samples_leaf=40, random_state=7).fit(Xv[tr], yy[tr])
        prob[te] = gb.predict_proba(Xv[te])[:, 1]
    oos = np.isfinite(prob)

    # poor-man's rule at the same bar: holding >= 50% of the reaction and
    # above running vwap (features carry both).
    retrace = X["retrace"].to_numpy() if "retrace" in X else np.full(len(X), np.nan)
    vwap_edge = X["vwap_edge_bp"].to_numpy() if "vwap_edge_bp" in X else \
        np.full(len(X), np.nan)
    rule_keep = (retrace <= 0.5) & (vwap_edge > 0)

    emit(f"# pead_confirmed: year table + slot sim — {STAMP}")
    emit()
    emit(f"{len(keep):,} events with a +{DECIDE_BARS}-bar decision "
         f"(of {len(q):,} pathed). Delayed entry at the decision print, "
         f"exit T+1 close, net@{COSTS_BP:.0f}bp (panel convention) and "
         f"@{AH_STRESS_BP:.1f}bp (AH-measured stress). Gate: walk-forward "
         f"GBM P(T+1 continuation), OOS AUC "
         f"{roc_auc_score(yy[oos], prob[oos]):.3f} on {int(oos.sum()):,}. "
         f"PRIMARY (declared in docstring): tau={PRIMARY_TAU}.")
    emit()

    emit("## The family (OOS 2019-26, per trade)")
    emit()
    emit("| gate | kept | keep% | net@13 bp | t | net@23.2 bp | t |")
    emit("|---|---|---|---|---|---|---|")

    def row(label, mask):
        a, b = dnet[mask] * 1e4, dnet_ah[mask] * 1e4
        mark = " **<-- primary**" if label == f"P >= {PRIMARY_TAU:.2f}" else ""
        emit(f"| {label} | {mask.sum():,} | {mask.sum() / oos.sum() * 100:.0f} "
             f"| {a.mean():+.1f} | {tstat(a):+.2f} | {b.mean():+.1f} "
             f"| {tstat(b):+.2f}{mark} |")

    row("ungated (delayed hold)", oos)
    for tau in TAUS:
        row(f"P >= {tau:.2f}", oos & (prob >= tau))
    row("rule: <=50% retraced & above vwap", oos & rule_keep)
    emit()

    emit("## Primary cell by year (net@13)")
    emit()
    emit("| year | ungated n | ungated bp | gated n | gated bp | t |")
    emit("|---|---|---|---|---|---|")
    gmask = oos & (prob >= PRIMARY_TAU)
    for t in range(2019, 2027):
        m_u = oos & (yrs == t)
        m_g = gmask & (yrs == t)
        if m_u.sum() == 0:
            continue
        emit(f"| {t} | {m_u.sum():,} | {(dnet[m_u] * 1e4).mean():+.1f} "
             f"| {m_g.sum():,} | {(dnet[m_g] * 1e4).mean():+.1f} "
             f"| {tstat(dnet[m_g] * 1e4):+.2f} |")
    emit()

    emit("## Slot sim (4 slots/night by dollar volume, 25% each, "
         "2019-26, net@13)")
    emit()
    from datetime import date
    spy = (_spy_daily(date(2018, 12, 1), date(2026, 8, 7))
           .sort_values("date"))
    spy["date"] = spy["date"].astype(str)
    spy_c = spy.set_index("date")["close"]
    sessions = [d for d in spy_c.index if "2019-01-01" <= d <= "2026-08-06"]
    spy_ret = spy_c.pct_change()
    emit("| book | trades | ann % | Sharpe | maxDD % | beta |")
    emit("|---|---|---|---|---|---|")
    for label, mask in (("ungated", oos), ("gated (primary)", gmask)):
        sub = pd.DataFrame({
            "date": dates[keep][mask], "ret": dnet[mask],
            "dv": dv[keep][mask]})
        picks = (sub.sort_values(["date", "dv"], ascending=[True, False])
                 .groupby("date").head(SLOTS))
        day_ret = (picks["ret"] * POSITION_FRAC).groupby(picks["date"]).sum()
        ret = day_ret.reindex(sessions).fillna(0.0)
        eq = np.cumprod(1 + ret.to_numpy())
        years = len(sessions) / 252
        ann = float(eq[-1] ** (1 / years) - 1) * 100
        sh = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
        dd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
        j = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
        beta = float(np.cov(j.iloc[:, 0], j.iloc[:, 1], ddof=1)[0, 1]
                     / np.var(j.iloc[:, 1], ddof=1))
        emit(f"| {label} | {len(picks):,} | {ann:+.2f} | {sh:+.2f} "
             f"| {dd:.1f} | {beta:+.3f} |")
    emit()
    emit("Reading gates before anyone builds: the entry is an AFTER-HOURS "
         "fill (same SIP/AH dependency class as the delayed arm — the IRA "
         "preflight's question applies); the gate is a fitted model and "
         "inherits the full 2b testing bar (placebo/CV) before pre-reg; "
         "and the 23.2bp column is the cost reality until AH fills are "
         "measured on these books.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(SCRIPTS)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_pead_confirmed.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"pead_confirmed_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
