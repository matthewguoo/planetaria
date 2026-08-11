"""ML fade-quality gate for gap_fail_fade — walk-forward, thresholds-as-family.

Queue item #1 of the 2026-08-10 handoff: "a fade-quality gate for gff
(premarket features -> take/skip)". Precedent and discipline: the wick
study's delayed-entry gate (+36.9 -> +81.2bp/tr at walk-forward AUC 0.737)
— walk-forward only, thresholds counted as a family, forward test before
trust.

Design, pre-stated (written before any result was seen):
  panel      the registered-config trade set from research_gff_decade
             (union 2016-2026, fading trigger, top-2/leg by pmvol, entry
             at the auction print, exit 09:31 close, net@10) — reproduced
             and asserted against the 20260810_1756 note BEFORE gating.
  training   all qualifying fades (pre-slot, ~3.7k); evaluation on the
             registered selection (the deployable book).
  features   decision-time only (<= 09:29 premarket + prior days):
             gap, against-gap turn, turn/gap fraction, premarket volume
             and dollars, price, leg, day-of-week, morning crowdedness
             (fades + gap events that morning), prior-day symbol context
             from the ADJUSTED daily panel used as RATIOS only (scan-note
             §4 trap): 1/5/20d returns, 20d realized vol, gap-in-sigma,
             prev range and close-position, prev dollar volume; SPY 1/5d
             return and 20d realized vol. The auction print (`open`) is
             the FILL, not a feature — nothing at or after 09:30 enters.
  models     LogisticRegression (median-impute + robust-scale) and
             HistGradientBoostingClassifier (depth 3, lr 0.06, 300 iters,
             min_samples_leaf 80, l2 1.0) — two models, no tuning sweep.
  target     gross trade bp > 0.
  protocol   expanding walk-forward by calendar year: train 2016..Y-1,
             score Y, for Y in 2019..2026. Nothing from Y touches the
             model that scores Y.
  gate       skip a selected trade when P(win) < tau, tau in
             {0.40, 0.45, 0.50, 0.55, 0.60}, table reported in FULL.
             PRIMARY CELL, declared here: HGB, both legs, tau=0.50,
             net@10. Null for the primary cell: 2,000 within-year
             shuffles of P (same keep-count, random composition) — the
             gate must beat random skipping, not just the ungated mean.
  secondary  (exploratory, labeled as such) rank-by-P slot selection in
             place of the pmvol ranking; pm0900-segment ablation on
             2022+ only (decade rows never stored the 09:00 mark).

Run: python scripts/research_gff_gate.py
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
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import RobustScaler  # noqa: E402

from research_gff_decade import (  # noqa: E402
    MIN_PRICE,
    MIN_TURN_BP,
    SLOTS_PER_LEG,
    load_union,
    spy_daily_ret,
    tstat,
)

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
PEAD_CACHE = ROOT / "research" / "pead-llm-gate" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

COST_BP = 10.0
POSITION_FRAC = 0.25
TAUS = (0.40, 0.45, 0.50, 0.55, 0.60)
PRIMARY_TAU = 0.50
TEST_YEARS = [str(y) for y in range(2019, 2027)]
SEED = 20260810
N_SHUFFLES = 2000

FEATS = [
    "gap_bp", "abs_gap_bp", "turn_bp", "turn_frac", "pm_dollar_log",
    "pmvol_log", "px_log", "is_long", "dow", "is_monday",
    "n_fades_today", "n_events_today",
    "ret1", "ret5", "ret20", "rv20", "gap_sigma",
    "range_prev", "closepos_prev", "dv_prev_log",
    "spy_ret1", "spy_ret5", "spy_rv20",
]
PM0900_FEATS = ["early_bp", "accel_bp"]


def daily_context() -> pd.DataFrame:
    """Prior-day symbol features from the adjusted panel — ratios only."""
    frames = [pd.read_parquet(p, columns=["symbol", "date", "o", "h", "l", "c", "v"])
              for p in sorted(PEAD_CACHE.glob("bars_ohlc_*_1000.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].astype(str)
    df = (df.drop_duplicates(subset=["symbol", "date"])
          .sort_values(["symbol", "date"]))
    g = df.groupby("symbol", sort=False)
    c = df["c"]
    ret = c / g["c"].shift(1) - 1
    df["ret1"] = ret.groupby(df["symbol"]).shift(1)
    df["ret5"] = (c / g["c"].shift(5) - 1).groupby(df["symbol"]).shift(1)
    df["ret20"] = (c / g["c"].shift(20) - 1).groupby(df["symbol"]).shift(1)
    df["rv20"] = (ret.groupby(df["symbol"])
                  .rolling(20, min_periods=15).std().reset_index(level=0, drop=True)
                  .groupby(df["symbol"]).shift(1)) * np.sqrt(252)
    rng = (df["h"] - df["l"]) / df["c"]
    pos = ((df["c"] - df["l"]) / (df["h"] - df["l"]).replace(0, np.nan))
    df["range_prev"] = rng.groupby(df["symbol"]).shift(1)
    df["closepos_prev"] = pos.groupby(df["symbol"]).shift(1)
    df["dv_prev_log"] = np.log1p((df["c"] * df["v"]).groupby(df["symbol"]).shift(1))
    return df[["symbol", "date", "ret1", "ret5", "ret20", "rv20",
               "range_prev", "closepos_prev", "dv_prev_log"]]


def spy_context() -> pd.DataFrame:
    spy = spy_daily_ret().to_frame("r").reset_index()
    spy["spy_ret1"] = spy["r"].shift(1)
    spy["spy_ret5"] = spy["r"].rolling(5).sum().shift(1)
    spy["spy_rv20"] = spy["r"].rolling(20, min_periods=15).std().shift(1) * np.sqrt(252)
    return spy[["date", "spy_ret1", "spy_ret5", "spy_rv20"]]


def build_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """All qualifying fades with features (train pool) + registered selection."""
    df = load_union().dropna(subset=["pm0915", "pm0929", "open", "c1"])
    df = df[df["open"] >= MIN_PRICE].copy()
    df["n_events_today"] = df.groupby("date")["symbol"].transform("size")
    pm_trend = (df["pm0929"] / df["pm0915"] - 1) * 1e4
    gap = df["gap"]
    fading = (pm_trend.abs() >= MIN_TURN_BP) & \
             (np.sign(pm_trend) != np.sign(gap * 1e4))
    df["bp"] = -np.sign(gap) * (df["c1"] / df["open"] - 1) * 1e4
    ev = df[fading].dropna(subset=["bp"]).copy()

    ev["gap_bp"] = ev["gap"] * 1e4
    ev["abs_gap_bp"] = ev["gap_bp"].abs()
    ev["turn_bp"] = -np.sign(ev["gap_bp"]) * (ev["pm0929"] / ev["pm0915"] - 1) * 1e4
    ev["turn_frac"] = ev["turn_bp"] / ev["abs_gap_bp"]
    ev["pm_dollar_log"] = np.log1p(ev["pmvol"] * ev["pm0929"])
    ev["pmvol_log"] = np.log1p(ev["pmvol"])
    ev["px_log"] = np.log(ev["pm0929"])
    ev["is_long"] = (ev["gap"] < 0).astype(float)
    dt = pd.to_datetime(ev["date"])
    ev["dow"] = dt.dt.dayofweek.astype(float)
    ev["is_monday"] = (dt.dt.dayofweek == 0).astype(float)
    ev["n_fades_today"] = ev.groupby("date")["symbol"].transform("size")
    ev["year"] = ev["date"].str[:4]

    ev = ev.merge(daily_context(), on=["symbol", "date"], how="left")
    ev = ev.merge(spy_context(), on="date", how="left")
    rv_day_bp = ev["rv20"] / np.sqrt(252) * 1e4
    ev["gap_sigma"] = ev["abs_gap_bp"] / rv_day_bp.replace(0, np.nan)

    pm0900 = pd.read_parquet(CACHE / "pm_marks.parquet").drop_duplicates(
        subset=["symbol", "date"])[["symbol", "date", "pm0900"]]
    ev = ev.merge(pm0900, on=["symbol", "date"], how="left")
    ev["early_bp"] = -np.sign(ev["gap_bp"]) * (ev["pm0915"] / ev["pm0900"] - 1) * 1e4
    ev["accel_bp"] = ev["turn_bp"] - ev["early_bp"]

    ev["long"] = ev["gap"] < 0
    sel = (ev.sort_values(["date", "pmvol"], ascending=[True, False])
           .groupby(["date", "long"]).head(SLOTS_PER_LEG).copy())
    return ev, sel


def make_models() -> dict[str, object]:
    logit = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", RobustScaler()),
        ("lr", LogisticRegression(C=1.0, max_iter=2000)),
    ])
    hgb = HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.06, max_iter=300,
        min_samples_leaf=80, l2_regularization=1.0, max_bins=127,
        early_stopping=False, random_state=SEED)
    return {"logit": logit, "hgb": hgb}


def walk_forward(ev: pd.DataFrame, sel: pd.DataFrame,
                 feats: list[str]) -> tuple[pd.DataFrame, dict, list]:
    """P(win) per model for every fade in the OOS years; fold models kept."""
    preds = {name: pd.Series(np.nan, index=ev.index) for name in make_models()}
    fold_rows = []
    fold_models: dict[str, list] = {name: [] for name in make_models()}
    for y in TEST_YEARS:
        tr = ev["year"] < y
        te = ev["year"] == y
        if te.sum() == 0 or tr.sum() < 300:
            continue
        Xtr, ytr = ev.loc[tr, feats], (ev.loc[tr, "bp"] > 0).astype(int)
        Xte, yte = ev.loc[te, feats], (ev.loc[te, "bp"] > 0).astype(int)
        row = {"year": y, "n_train": int(tr.sum()), "n_test": int(te.sum())}
        for name, model in make_models().items():
            model.fit(Xtr, ytr)
            p = model.predict_proba(Xte)[:, 1]
            preds[name].loc[te] = p
            row[f"auc_{name}"] = roc_auc_score(yte, p) if yte.nunique() > 1 else np.nan
            fold_models[name].append((y, model))
        fold_rows.append(row)
    for name, p in preds.items():
        ev[f"p_{name}"] = p
    sel = sel.drop(columns=[c for c in sel.columns if c.startswith("p_")],
                   errors="ignore")
    out_sel = sel.merge(
        ev[["symbol", "date", "p_logit", "p_hgb"]],
        on=["symbol", "date"], how="left")
    return out_sel, {"folds": pd.DataFrame(fold_rows)}, fold_models["hgb"]


def account_stats(day_ret: pd.Series, all_days: list[str],
                  spy: pd.Series) -> dict:
    ret = day_ret.reindex(all_days).fillna(0.0)
    eq = np.cumprod(1 + ret.to_numpy())
    years = len(all_days) / 252
    ann = float(eq[-1] ** (1 / years) - 1) * 100
    sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
    mdd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
    j = pd.concat([ret, spy], axis=1, join="inner").dropna()
    y, x = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
    beta = float(np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1))
    alpha = float((y.mean() - beta * x.mean()) * 252 * 100)
    return {"ann": ann, "sharpe": sharpe, "mdd": mdd,
            "alpha": alpha, "beta": beta}


def main() -> None:
    rng = np.random.default_rng(SEED)
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    ev, sel = build_panel()

    # ---- baseline reproduction gate (must match gff_decade_20260810_1756) --
    net_l = sel.loc[sel["long"], "bp"] - COST_BP
    net_b = sel["bp"] - COST_BP
    repro = {
        "n_long": len(net_l), "n_both": len(net_b),
        "net_long": net_l.mean(), "t_long": tstat(net_l.to_numpy()),
        "net_both": net_b.mean(), "t_both": tstat(net_b.to_numpy()),
    }
    ok = (repro["n_long"] == 1518 and repro["n_both"] == 3377
          and abs(repro["net_long"] - 14.5) < 0.15
          and abs(repro["net_both"] - 9.2) < 0.15)
    print(f"baseline repro: {repro}  -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("PANEL DOES NOT REPRODUCE THE REGISTERED NOTE — stopping "
              "before any gating.")
        sys.exit(1)

    sel, wf, hgb_folds = walk_forward(ev, sel, FEATS)
    oos = sel.dropna(subset=["p_hgb"]).copy()          # selected trades, 2019+
    ev_oos = ev.dropna(subset=["p_hgb"]).copy()        # all fades, 2019+

    emit(f"# gff fade-quality gate, walk-forward — {STAMP}")
    emit()
    emit(f"Panel: {len(ev):,} qualifying fades / {len(sel):,} registered "
         f"trades (baseline reproduced: LONG {repro['net_long']:+.1f}bp "
         f"t {repro['t_long']:+.2f}, BOTH {repro['net_both']:+.1f}bp "
         f"t {repro['t_both']:+.2f} @10bp — matches the 20260810_1756 note). "
         f"Training on all fades, gating the registered selection. "
         f"Walk-forward 2019-2026 (train from 2016). Target: gross bp > 0. "
         f"{len(FEATS)} decision-time features; nothing at/after 09:30. "
         f"PRIMARY CELL (declared in the script docstring before results): "
         f"HGB, both legs, tau={PRIMARY_TAU}, net@10.")
    emit()

    # ---- AUC by year ------------------------------------------------------
    emit("## Walk-forward AUC by year (all qualifying fades)")
    emit()
    emit("| year | n train | n test | logit | HGB |")
    emit("|---|---|---|---|---|")
    for _, r in wf["folds"].iterrows():
        emit(f"| {r['year']} | {r['n_train']:,} | {r['n_test']:,} "
             f"| {r['auc_logit']:.3f} | {r['auc_hgb']:.3f} |")
    pooled_auc = {}
    for name in ("logit", "hgb"):
        yy = (ev_oos["bp"] > 0).astype(int)
        pooled_auc[name] = roc_auc_score(yy, ev_oos[f"p_{name}"])
    emit(f"| pooled | | {len(ev_oos):,} | {pooled_auc['logit']:.3f} "
         f"| {pooled_auc['hgb']:.3f} |")
    emit()

    # ---- gate family table ------------------------------------------------
    emit("## Gate on the registered selection, OOS 2019-2026, net@10 bp/trade")
    emit()
    emit("The full tau x model x legs family (20 cells + baselines). "
         "Anyone shopping this table owes a x20 multiplicity haircut; only "
         "the pre-declared primary cell gets the permutation null.")
    emit()
    emit("| model | legs | tau | kept | keep% | net bp/tr | t | delta vs ungated |")
    emit("|---|---|---|---|---|---|---|---|")
    base = {}
    for legs, m in (("LONG", oos["long"]), ("BOTH", pd.Series(True, index=oos.index))):
        nb = oos.loc[m, "bp"] - COST_BP
        base[legs] = nb.mean()
        emit(f"| — | {legs} | none | {len(nb):,} | 100 | {nb.mean():+.1f} "
             f"| {tstat(nb.to_numpy()):+.2f} | |")
    for name in ("logit", "hgb"):
        for legs, m in (("LONG", oos["long"]),
                        ("BOTH", pd.Series(True, index=oos.index))):
            for tau in TAUS:
                keep = m & (oos[f"p_{name}"] >= tau)
                nb = oos.loc[keep, "bp"] - COST_BP
                if len(nb) < 50:
                    continue
                mark = " **<-- primary**" if (
                    name == "hgb" and legs == "BOTH" and tau == PRIMARY_TAU) else ""
                emit(f"| {name} | {legs} | {tau:.2f} | {len(nb):,} "
                     f"| {len(nb) / m.sum() * 100:.0f} | {nb.mean():+.1f} "
                     f"| {tstat(nb.to_numpy()):+.2f} "
                     f"| {nb.mean() - base[legs]:+.1f}{mark} |")
    emit()

    # ---- primary cell: permutation null -----------------------------------
    keep_mask = oos["p_hgb"] >= PRIMARY_TAU
    actual = (oos.loc[keep_mask, "bp"] - COST_BP).mean()
    bp_arr = oos["bp"].to_numpy() - COST_BP
    years_arr = oos["year"].to_numpy()
    p_arr = oos["p_hgb"].to_numpy()
    shuf_means = np.empty(N_SHUFFLES)
    for i in range(N_SHUFFLES):
        keep_idx = np.zeros(len(oos), dtype=bool)
        for y in np.unique(years_arr):
            ym = years_arr == y
            perm = rng.permutation(p_arr[ym])
            keep_idx[ym] = perm >= PRIMARY_TAU
        shuf_means[i] = bp_arr[keep_idx].mean()
    pval = float((shuf_means >= actual).mean())
    emit("## Primary cell null (2,000 within-year shuffles of P, same "
         "keep-count)")
    emit()
    emit(f"Actual net@10 {actual:+.1f}bp/tr on {int(keep_mask.sum()):,} kept "
         f"({keep_mask.mean() * 100:.0f}%); shuffled selections mean "
         f"{shuf_means.mean():+.1f}bp, sd {shuf_means.std(ddof=1):.1f} -> "
         f"empirical p = {pval:.4f} (P(shuffle >= actual)).")
    emit()

    # ---- primary cell by year --------------------------------------------
    emit("## Primary cell by year (HGB, both legs, tau=0.50, net@10)")
    emit()
    emit("| year | ungated n | ungated bp | gated n | gated bp | t | delta |")
    emit("|---|---|---|---|---|---|---|")
    for y in TEST_YEARS:
        ym = oos["year"] == y
        if ym.sum() == 0:
            continue
        u = oos.loc[ym, "bp"] - COST_BP
        g = oos.loc[ym & keep_mask, "bp"] - COST_BP
        emit(f"| {y} | {len(u):,} | {u.mean():+.1f} | {len(g):,} "
             f"| {g.mean():+.1f} | {tstat(g.to_numpy()):+.2f} "
             f"| {g.mean() - u.mean():+.1f} |")
    emit()

    # ---- account level ----------------------------------------------------
    spy = spy_daily_ret()
    all_days = [d for d in spy.index
                if "2019-01-01" <= d <= max(oos["date"])]
    emit("## Account level, 2019-2026 OOS window, @10bp (25%/trade sizing)")
    emit()
    emit("| book | trades | ann % | Sharpe | maxDD % | alpha %/yr | beta |")
    emit("|---|---|---|---|---|---|---|")
    books = (
        ("LONG ungated", oos[oos["long"]]),
        ("LONG gated", oos[oos["long"] & keep_mask]),
        ("BOTH ungated", oos),
        ("BOTH gated", oos[keep_mask]),
    )
    for label, frame in books:
        net = frame["bp"] - COST_BP
        day_ret = (net * POSITION_FRAC / 1e4).groupby(frame["date"]).sum()
        st = account_stats(day_ret, all_days, spy)
        emit(f"| {label} | {len(frame):,} | {st['ann']:+.2f} "
             f"| {st['sharpe']:+.2f} | {st['mdd']:.1f} | {st['alpha']:+.2f} "
             f"| {st['beta']:+.3f} |")
    emit()

    # ---- feature importance (OOS permutation, averaged over folds) --------
    emit("## Feature importance (HGB, permutation AUC drop on each OOS year, "
         "n-weighted mean)")
    emit()
    imps = np.zeros(len(FEATS))
    tot = 0
    for y, model in hgb_folds:
        te = ev_oos["year"] == y
        if te.sum() < 100 or ev_oos.loc[te, "bp"].gt(0).nunique() < 2:
            continue
        r = permutation_importance(
            model, ev_oos.loc[te, FEATS], (ev_oos.loc[te, "bp"] > 0).astype(int),
            scoring="roc_auc", n_repeats=10, random_state=SEED)
        imps += r.importances_mean * te.sum()
        tot += te.sum()
    imps /= max(tot, 1)
    order = np.argsort(imps)[::-1]
    emit("| feature | mean AUC drop |")
    emit("|---|---|")
    for i in order[:10]:
        emit(f"| {FEATS[i]} | {imps[i]:+.4f} |")
    emit()

    # ---- secondary: rank-by-P slot selection (exploratory) ----------------
    emit("## Secondary (exploratory): rank-by-P slot selection, net@10")
    emit()
    emit("Replaces the pmvol ranking with p_hgb (top-2/leg among that "
         "morning's fades). Same walk-forward P; NOT the primary — no null "
         "run; a forward test would be needed before believing it.")
    emit()
    emit("| selection | legs | n | net bp/tr | t |")
    emit("|---|---|---|---|---|")
    sel_p = (ev_oos.sort_values(["date", "p_hgb"], ascending=[True, False])
             .groupby(["date", "long"]).head(SLOTS_PER_LEG))
    for legs, frame in (("LONG", oos[oos["long"]]), ("BOTH", oos)):
        nb = frame["bp"] - COST_BP
        emit(f"| pmvol (registered) | {legs} | {len(nb):,} | {nb.mean():+.1f} "
             f"| {tstat(nb.to_numpy()):+.2f} |")
    for legs, m in (("LONG", sel_p["long"]), ("BOTH", pd.Series(True, index=sel_p.index))):
        nb = sel_p.loc[m, "bp"] - COST_BP
        emit(f"| rank-by-P | {legs} | {len(nb):,} | {nb.mean():+.1f} "
             f"| {tstat(nb.to_numpy()):+.2f} |")
    emit()

    # ---- secondary: pm0900 ablation (2022+ only, exploratory) -------------
    emit("## Secondary (exploratory): pm0900-segment ablation, 2022+ panel")
    emit()
    emit("Decade rows never stored the 09:00 mark, so this runs train-2022+ "
         "only: expanding walk-forward 2024-2026, HGB, decade features vs "
         "+early_bp/accel_bp. Short training window — direction, not a "
         "verdict.")
    emit()
    emit("| test year | n | AUC base | AUC +pm0900 | delta |")
    emit("|---|---|---|---|---|")
    sub = ev[ev["year"] >= "2022"].copy()
    for y in ("2024", "2025", "2026"):
        tr = sub["year"] < y
        te = sub["year"] == y
        if te.sum() == 0:
            continue
        ytr = (sub.loc[tr, "bp"] > 0).astype(int)
        yte = (sub.loc[te, "bp"] > 0).astype(int)
        aucs = {}
        for label, cols in (("base", FEATS), ("pm0900", FEATS + PM0900_FEATS)):
            hgb = HistGradientBoostingClassifier(
                max_depth=3, learning_rate=0.06, max_iter=300,
                min_samples_leaf=80, l2_regularization=1.0, max_bins=127,
                early_stopping=False, random_state=SEED)
            hgb.fit(sub.loc[tr, cols], ytr)
            aucs[label] = roc_auc_score(yte, hgb.predict_proba(sub.loc[te, cols])[:, 1])
        emit(f"| {y} | {int(te.sum()):,} | {aucs['base']:.3f} "
             f"| {aucs['pm0900']:.3f} | {aucs['pm0900'] - aucs['base']:+.3f} |")
    emit()

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append(f"_Provenance: `research_gff_gate.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_gate_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
