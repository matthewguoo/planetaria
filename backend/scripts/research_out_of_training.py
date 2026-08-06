"""Is the edge skill, or is it recall? A natural experiment on training cutoffs.

THE PROBLEM. Almost every event in this study predates the model's training
data. If the model remembers what happened next, a remembered outcome is
indistinguishable from a good prediction, and the whole paper measures nothing.
Section 5.7's arms attack this from inside one model — scrub the identity, or
remove the release text entirely — and both are informative but neither is
decisive: the blind arm leaks because the model re-identifies the issuer, and
the memory probe bounds recall from ABOVE without saying how much of the real
edge it accounts for.

THE DESIGN. Anthropic publishes a training-data cutoff for each model, and the
Opus family's cutoffs differ:

    Opus 4.6   2025-08        Opus 4.7   2026-01
    Opus 4.8   2026-01        Opus 5     2026-05

So for an earnings release in, say, March 2026, Opus 5 could have read what
happened next and Opus 4.6/4.7/4.8 could not. Run all four on the SAME events
and the confound becomes an experiment: hold the event fixed, vary whether the
model could have memorised it, and whatever is left is the memorisation effect.

THE ESTIMATOR. Two-way fixed effects on the event-by-model panel:

    y[i,m] = event_effect[i] + model_effect[m] + beta * in_corpus[i,m] + e

Event effects absorb everything about the event — the news, the size of the
move, the market that week. Model effects absorb everything about a model's
general ability. `beta` is therefore identified ONLY from within-event
variation between models that differ in corpus status, which is exactly the
quantity of interest, and it is immune to both "2026 was an easier year" and
"Opus 5 is simply a better model". Standard errors cluster by event, because
four observations of one release are not four independent draws.

WHY EQUIVALENCE TESTING. "beta was not significant" is not evidence that
memorisation is absent — it is compatible with a study too small to see it.
The claim the paper wants is the positive one, so this reports a TOST-style
equivalence result: the smallest margin within which the effect is bounded at
90% confidence. If that margin is small relative to the measured edge, the
edge cannot be mostly recall.

THE PLACEBO. Opus 4.7 and Opus 4.8 share a cutoff to the month. Any gap
between them is model generation with the corpus held constant, so it
calibrates how large a "model difference" looks when memorisation cannot be
the cause.

Run:
    python scripts/research_out_of_training.py report
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_event_panel import load_universe_v2  # noqa: E402
from research_holding_period import paths_file  # noqa: E402
from research_llm_contamination import (  # noqa: E402
    MODELS,
    _load_results,
)

# Equivalence margins tried, in basis points of per-trade return.
MARGINS = (25, 50, 75, 100, 150, 200)


def _cutoff_end(yyyymm: str) -> str:
    """Last day of the cutoff month. A model whose training data ends
    '2026-01' is credited with everything through 2026-01-31 — the
    conservative direction, because it counts MORE events as possibly-seen and
    so makes a memorisation effect easier to detect, not harder."""
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y}-{m:02d}-31" if m in (1, 3, 5, 7, 8, 10, 12) else (
        f"{y}-{m:02d}-30" if m != 2 else f"{y}-02-29")


def panel(effort: str = "medium") -> pd.DataFrame:
    """One row per (event, model), with the outcome and corpus status."""
    res = _load_results(model=None)
    if res.empty:
        raise SystemExit("no verdicts on disk")
    res = res[(res["effort"] == effort) & (res["arm"] == "named")]

    universe = load_universe_v2()
    paths = pd.read_parquet(paths_file("v2"))
    first_close = {(s, d): c[0] for s, d, c in
                   zip(paths["symbol"], paths["date"], paths["closes"])}
    universe["exit"] = [first_close.get((s, d), np.nan) for s, d
                        in zip(universe["symbol"], universe["date"])]
    universe["fwd_bp"] = (universe["exit"] / universe["react"] - 1) * 1e4
    universe = universe[universe["exit"].notna()]

    m = res.merge(universe[["symbol", "date", "move_pct", "fwd_bp", "year"]],
                  on=["symbol", "date"], how="inner")

    # The model's own directional call, as a tradeable side.
    side = np.where(m["direction"] == "bullish", 1,
                    np.where(m["direction"] == "bearish", -1, 0))
    m["side"] = side
    # PRIMARY OUTCOME: what following this model's view would have earned.
    # Defined for every cell including neutrals (which earn nothing), so the
    # panel is balanced and no model is advantaged by abstaining.
    m["signed_bp"] = side * m["fwd_bp"]
    # SECONDARY: directional accuracy, on the calls that took a side.
    m["correct"] = np.where(side == 0, np.nan,
                            (np.sign(m["fwd_bp"]) == side).astype(float))
    # The gate outcome, for continuity with the rest of the paper.
    m["agrees"] = ((side != 0) & (np.sign(m["move_pct"]) == side))
    m["gated_bp"] = np.where(m["agrees"], np.sign(m["move_pct"]) * m["fwd_bp"],
                             np.nan)

    cut = {mid: _cutoff_end(c) for _, (mid, c, _) in MODELS.items()}
    m["cutoff"] = m["model"].map(cut)
    m = m[m["cutoff"].notna()]
    m["in_corpus"] = (m["date"] <= m["cutoff"]).astype(float)
    m["mname"] = m["model"].map({mid: nm for _, (mid, _, nm) in MODELS.items()})
    return m.reset_index(drop=True)


def balanced(m: pd.DataFrame) -> pd.DataFrame:
    """Events every model scored. The comparison is only a comparison if the
    models are answering the same questions."""
    k = m.groupby(["symbol", "date"])["model"].nunique()
    full = set(k[k == k.max()].index)
    return m[[(s, d) in full for s, d in zip(m["symbol"], m["date"])]] \
        .reset_index(drop=True)


def twfe(df: pd.DataFrame, y: str) -> dict:
    """Two-way fixed effects by iterated demeaning, SEs clustered by event.

    Written out rather than pulled from a package: the whole point of the
    section is that a reader can check the estimate, and a one-regressor
    within estimator is short enough to read.
    """
    d = df[[y, "in_corpus", "symbol", "date", "model"]].dropna().copy()
    if d.empty or d["in_corpus"].nunique() < 2:
        return {"n": len(d), "estimate": None, "se": None,
                "note": "no within-event variation in corpus status"}
    d["ev"] = d["symbol"] + "|" + d["date"]
    yv = d[y].to_numpy(float)
    xv = d["in_corpus"].to_numpy(float)

    # Alternating projections: subtract event means, then model means, repeat.
    ev = d["ev"].to_numpy()
    mo = d["model"].to_numpy()
    for _ in range(60):
        for key in (ev, mo):
            s = pd.Series(yv).groupby(key).transform("mean").to_numpy()
            yv = yv - s
            s = pd.Series(xv).groupby(key).transform("mean").to_numpy()
            xv = xv - s

    denom = float(xv @ xv)
    if denom <= 1e-12:
        return {"n": len(d), "estimate": None, "se": None,
                "note": "corpus status is collinear with the fixed effects"}
    beta = float(xv @ yv / denom)
    resid = yv - beta * xv

    # Cluster-robust by event: sum the within-cluster score, square, sum.
    sc = pd.Series(xv * resid).groupby(ev).sum().to_numpy()
    meat = float(sc @ sc)
    n_clust = len(np.unique(ev))
    # Small-sample correction, the usual (G/(G-1)) * ((N-1)/(N-K)).
    corr = (n_clust / max(n_clust - 1, 1)) * ((len(d) - 1) / max(len(d) - 2, 1))
    var = corr * meat / (denom ** 2)
    se = float(np.sqrt(max(var, 0.0)))
    # Rounding is outcome-dependent. Returns are hundreds of basis points and
    # one decimal is plenty; a share-correct outcome lives in [0,1] and one
    # decimal rounds a 90% interval of [-0.069, +0.029] to [-0.1, 0.0],
    # throwing away most of the precision the estimate actually has and making
    # a well-powered test look like a badly-powered one.
    dp = 1 if abs(beta) + se > 1 else 4
    return {"n": int(len(d)), "n_events": int(n_clust),
            "estimate": round(beta, dp + 1), "se": round(se, dp + 1),
            "t": round(beta / se, 2) if se > 0 else None,
            "ci90": [round(beta - 1.645 * se, dp), round(beta + 1.645 * se, dp)],
            "ci95": [round(beta - 1.96 * se, dp), round(beta + 1.96 * se, dp)]}


def equivalence(est: dict, margins=MARGINS) -> dict:
    """TOST. Equivalence at margin D holds when the 90% CI sits inside +/-D.

    Reported as the SMALLEST margin that holds, which is the most information
    a single number can carry here, and is just the larger absolute CI bound.
    """
    if est.get("estimate") is None:
        return {"holds": None}
    lo, hi = est["ci90"]
    tight = max(abs(lo), abs(hi))
    return {
        "tightest_bp": round(float(tight), 1),
        "by_margin": [{"margin_bp": d, "holds": bool(tight <= d)}
                      for d in margins],
        # The two one-sided p-values, stated explicitly so the test is legible.
        "note": ("equivalence at margin D holds when the 90% confidence "
                 "interval lies entirely inside +/-D"),
    }


def memory_probe(effort: str = "medium") -> dict:
    """The memory probe, run on models that cannot have the answer.

    Section 5.7 hands the model a ticker and a date and NO release text, and
    the resulting gate still earns a large spread. That was read as the value
    of recall, and it is the single most damaging number in the paper.

    But the probe was only ever run on one model, for which nearly every event
    was in-corpus, so "recall" and "everything else the model knows" were never
    separated. Running the same probe on a model whose training data ends
    BEFORE these events answers it directly. If the spread survives where
    recall is impossible, then the probe was never measuring recall — it was
    measuring priors about the issuer, combined with the tape sign that the
    gate itself supplies.
    """
    res = _load_results(model=None)
    if res.empty:
        return {}
    res = res[(res["effort"] == effort) & (res["arm"] == "notext")]
    if res.empty or res["model"].nunique() < 2:
        return {}

    universe = load_universe_v2()
    paths = pd.read_parquet(paths_file("v2"))
    first_close = {(s, d): c[0] for s, d, c in
                   zip(paths["symbol"], paths["date"], paths["closes"])}
    universe["exit"] = [first_close.get((s, d), np.nan) for s, d
                        in zip(universe["symbol"], universe["date"])]
    universe["fwd_bp"] = (universe["exit"] / universe["react"] - 1) * 1e4
    m = res.merge(universe[["symbol", "date", "move_pct", "fwd_bp"]],
                  on=["symbol", "date"], how="inner")
    m = m[m["fwd_bp"].notna()]
    if m.empty:
        return {}

    side = np.where(m["direction"] == "bullish", 1,
                    np.where(m["direction"] == "bearish", -1, 0))
    tape = np.sign(m["move_pct"])
    m["agrees"] = (side != 0) & (side == tape)
    m["pnl_bp"] = tape * m["fwd_bp"]
    cut = {mid: _cutoff_end(c) for _, (mid, c, _) in MODELS.items()}
    m["cutoff"] = m["model"].map(cut)
    m = m[m["cutoff"].notna()]
    m["in_corpus"] = (m["date"] <= m["cutoff"])
    nm = {mid: name for _, (mid, _, name) in MODELS.items()}
    m["volunteered"] = side != 0

    # THE STATISTIC THAT MATTERS IS THE VOLUNTEER RATE, not the spread.
    # A model with nothing to recall answers "neutral", and a neutral verdict
    # never passes the gate — so the gate simply does not fire and there is no
    # spread to compare. Measuring only the spread would have silently dropped
    # those models as "too few observations", which is exactly backwards: the
    # absence of directional calls IS the result.
    cells = []
    for (mid, ic), g in m.groupby(["model", "in_corpus"]):
        if len(g) < 15:
            continue
        gg, vv = g[g["agrees"]], g[~g["agrees"]]
        enough = len(gg) >= 15 and len(vv) >= 15
        cells.append({
            "model": mid, "name": nm.get(mid, mid), "in_corpus": bool(ic),
            "n": int(len(g)), "gated_n": int(len(gg)),
            "volunteer_pct": round(float(g["volunteered"].mean() * 100), 1),
            "gated_bp": round(float(gg["pnl_bp"].mean()), 1) if enough else None,
            "vetoed_bp": round(float(vv["pnl_bp"].mean()), 1) if enough else None,
            "spread_bp": round(float(gg["pnl_bp"].mean()
                                     - vv["pnl_bp"].mean()), 1) if enough else None,
        })
    if not cells:
        return {}

    in_c = [c for c in cells if c["in_corpus"]]
    out_c = [c for c in cells if not c["in_corpus"]]

    def rate(cs):
        n = sum(c["n"] for c in cs)
        return (round(sum(c["volunteer_pct"] * c["n"] for c in cs) / n, 1)
                if n else None, n)

    v_in, n_in = rate(in_c)
    v_out, n_out = rate(out_c)
    spreads = [c["spread_bp"] for c in out_c if c["spread_bp"] is not None]
    return {
        "cells": sorted(cells, key=lambda r: (r["name"], r["in_corpus"])),
        "span": [str(m["date"].min()), str(m["date"].max())],
        "volunteer_in_pct": v_in, "volunteer_in_n": n_in,
        "volunteer_out_pct": v_out, "volunteer_out_n": n_out,
        "out_of_corpus_spread_bp": (round(float(np.mean(spreads)), 1)
                                    if spreads else None),
        "out_of_corpus_n": n_out,
        # The probe behaves as designed when a model with nothing to remember
        # declines to answer. That is what validates it as a recall instrument.
        "probe_is_recall": bool(v_in is not None and v_out is not None
                                and v_in > 4 * max(v_out, 0.5)),
    }


def compute(args=None) -> dict:
    m = panel(getattr(args, "effort", "medium"))
    bal = balanced(m)
    if bal.empty or bal["model"].nunique() < 2:
        raise SystemExit("cross-model verdicts not collected yet")

    span = [str(bal["date"].min()), str(bal["date"].max())]
    out: dict = {
        "span": span, "n_events": int(bal.groupby(["symbol", "date"]).ngroups),
        "n_obs": int(len(bal)), "effort": getattr(args, "effort", "medium"),
        "models": [{"model": mid, "name": nm, "cutoff": c,
                    "in_corpus_n": int(((bal["model"] == mid)
                                        & (bal["in_corpus"] == 1)).sum()),
                    "out_corpus_n": int(((bal["model"] == mid)
                                         & (bal["in_corpus"] == 0)).sum())}
                   for _, (mid, c, nm) in MODELS.items()
                   if (bal["model"] == mid).any()],
    }

    # --- the raw cells, before any modelling ------------------------------
    cells = []
    for (mid, ic), g in bal.groupby(["model", "in_corpus"]):
        nm = {m2id: nm2 for _, (m2id, _, nm2) in MODELS.items()}.get(mid, mid)
        cells.append({
            "model": mid, "name": nm, "in_corpus": bool(ic), "n": int(len(g)),
            "signed_bp": round(float(g["signed_bp"].mean()), 1),
            "accuracy_pct": (round(float(g["correct"].mean() * 100), 1)
                             if g["correct"].notna().any() else None),
            "gated_bp": (round(float(g["gated_bp"].mean()), 1)
                         if g["gated_bp"].notna().any() else None),
            "gated_n": int(g["gated_bp"].notna().sum()),
        })
    out["cells"] = sorted(cells, key=lambda r: (r["name"], r["in_corpus"]))

    # --- the naive comparison, kept because it is the WRONG answer ---------
    # Pooling in-corpus against out-of-corpus without event effects gives a
    # spectacular and entirely spurious memorisation effect: "in corpus" is
    # almost perfectly collinear with "earlier in the window", and the early
    # months of this window happened to trade well. Reporting the naive number
    # next to the within-event one is the clearest possible demonstration of
    # why the fixed effects are doing real work rather than decorating.
    ic = bal["in_corpus"] == 1
    out["naive"] = {
        "in_bp": round(float(bal.loc[ic, "signed_bp"].mean()), 1),
        "out_bp": round(float(bal.loc[~ic, "signed_bp"].mean()), 1),
        "diff_bp": round(float(bal.loc[ic, "signed_bp"].mean()
                               - bal.loc[~ic, "signed_bp"].mean()), 1),
        "note": ("no event effects: confounds memorisation with calendar time, "
                 "because corpus status is nearly a function of the date"),
    }

    # --- the estimate -----------------------------------------------------
    # The margin list is in basis points, so it may only be applied to an
    # outcome measured in basis points. Handing it a share-correct estimate
    # would declare equivalence at every margin, trivially and meaninglessly,
    # because 0.06 is less than 25 for reasons of units rather than evidence.
    out["did"] = {}
    for label, y in (("signed_bp", "signed_bp"), ("accuracy", "correct")):
        est = twfe(bal, y)
        if y == "signed_bp":
            est["equivalence"] = equivalence(est)
        else:
            est["equivalence"] = {
                "tightest_share": (max(abs(est["ci90"][0]), abs(est["ci90"][1]))
                                   if est.get("ci90") else None),
                "note": "in share-correct units; see the basis-point translation",
            }
        out["did"][label] = est

    # --- accuracy, translated into basis points ---------------------------
    # The return outcome is badly powered: a single earnings reaction is worth
    # hundreds of basis points, so 257 events cannot pin a 100bp effect. Getting
    # the DIRECTION right is the same question asked with far less noise, and it
    # converts back: flipping one call from wrong to right swings the P&L by
    # twice the size of that move, so a delta of d in the share correct is worth
    # roughly 2 * d * E|move| basis points. This is the bound that actually
    # constrains the hypothesis.
    acc = out["did"].get("accuracy") or {}
    mean_abs = float(bal["fwd_bp"].abs().mean())
    out["accuracy_scale_bp"] = round(mean_abs, 1)
    if acc.get("ci90"):
        lo, hi = acc["ci90"]
        implied = {
            "estimate": round(2 * mean_abs * (acc["estimate"] or 0.0), 1),
            "ci90": [round(2 * mean_abs * lo, 1), round(2 * mean_abs * hi, 1)],
            "bound": round(2 * mean_abs * max(abs(lo), abs(hi)), 1),
            "note": ("the accuracy effect expressed in basis points, using "
                     "2 x mean |next-session move| as the conversion"),
        }
        # Now that it is in basis points the bp margins are meaningful.
        implied["by_margin"] = [{"margin_bp": d,
                                 "holds": bool(implied["bound"] <= d)}
                                for d in MARGINS]
        out["accuracy_implied_bp"] = implied

    # --- placebo: two models, one cutoff ----------------------------------
    same = [mid for _, (mid, c, _) in MODELS.items() if c == "2026-01"]
    if len(same) == 2:
        a, b = same
        ga = bal[bal["model"] == a].set_index(["symbol", "date"])["signed_bp"]
        gb = bal[bal["model"] == b].set_index(["symbol", "date"])["signed_bp"]
        j = ga.align(gb, join="inner")
        diff = (j[0] - j[1]).to_numpy(float)
        if len(diff) > 2:
            se = float(diff.std(ddof=1) / np.sqrt(len(diff)))
            out["placebo"] = {
                "pair": [a, b], "n": int(len(diff)),
                "diff_bp": round(float(diff.mean()), 1),
                "se": round(se, 1),
                "t": round(float(diff.mean() / se), 2) if se > 0 else None,
                "note": ("same training cutoff, different model generation — "
                         "the scale of a model difference that memorisation "
                         "cannot explain"),
            }

    # --- the headline the paper needs -------------------------------------
    ref = out["did"]["signed_bp"]
    gated_all = bal["gated_bp"].dropna()
    out["edge_reference_bp"] = round(float(gated_all.mean()), 1) if len(gated_all) else None

    # The BINDING bound is whichever of the two outcomes constrains harder.
    # Returns are the quantity of interest but the noisier measurement;
    # accuracy is the same hypothesis measured more precisely. Taking the
    # tighter of the two is legitimate only because both are pre-specified
    # tests of one directional claim — "in-corpus does better" — and the
    # tighter one is reported as the bound with its provenance attached.
    cands = []
    if (ref.get("equivalence") or {}).get("tightest_bp") is not None:
        cands.append(("per-trade return", ref["equivalence"]["tightest_bp"]))
    if out.get("accuracy_implied_bp"):
        cands.append(("directional accuracy", out["accuracy_implied_bp"]["bound"]))
    if not cands or ref.get("estimate") is None:
        out["verdict"] = "not identified"
        out["verdict_short"] = "not identified"
        return out

    source, tight = min(cands, key=lambda c: c[1])
    out["bound_bp"] = round(float(tight), 1)
    out["bound_source"] = source
    ratio = (tight / abs(out["edge_reference_bp"])
             if out["edge_reference_bp"] else None)
    sig_ret = abs(ref.get("t") or 0) > 1.96
    acc = out["did"].get("accuracy") or {}
    sig_acc = abs(acc.get("t") or 0) > 1.96 and (acc.get("estimate") or 0) > 0
    if (sig_ret and ref["estimate"] > 0) or sig_acc:
        out["verdict"] = "detected"
        out["verdict_short"] = "memorisation detected"
    elif ratio is not None and ratio < 0.5:
        out["verdict"] = "bounded small"
        out["verdict_short"] = f"bounded under {tight:.0f}bp"
    else:
        out["verdict"] = "inconclusive"
        out["verdict_short"] = "underpowered"
    out["ratio_of_edge"] = round(ratio, 2) if ratio is not None else None

    # --- head to head on one calendar window -------------------------------
    # The fixed-effects estimate is the right answer but not a legible one.
    # This is the same comparison a reader can do by eye: take only the events
    # where Opus 5 had corpus access and at least one other model did not, and
    # put the two groups side by side. Same dates, same releases, so the
    # calendar confound that ruins the naive comparison cannot operate.
    # It has to be PAIRED WITHIN EACH EVENT. Pooling every in-corpus
    # observation against every out-of-corpus one reintroduces exactly the
    # calendar confound this design exists to remove — the same model lands on
    # both sides depending on the date, and the "difference" is again mostly
    # which months are in which group. Differencing inside an event and then
    # averaging keeps the comparison honest and gives a paired t-test for free.
    per_event, acc_pairs, gated_pairs = [], [], []
    for _, g in bal.groupby(["symbol", "date"]):
        seen, unseen = g[g["in_corpus"] == 1], g[g["in_corpus"] == 0]
        if seen.empty or unseen.empty:
            continue                      # no contrast available in this event
        per_event.append(float(seen["signed_bp"].mean()
                               - unseen["signed_bp"].mean()))
        if seen["correct"].notna().any() and unseen["correct"].notna().any():
            acc_pairs.append(float(seen["correct"].mean()
                                   - unseen["correct"].mean()))
        if seen["gated_bp"].notna().any() and unseen["gated_bp"].notna().any():
            gated_pairs.append(float(seen["gated_bp"].mean()
                                     - unseen["gated_bp"].mean()))
    if per_event:
        arr = np.asarray(per_event)
        se = float(arr.std(ddof=1) / np.sqrt(len(arr)))
        out["head_to_head"] = {
            "n_events": int(len(arr)),
            "diff_bp": round(float(arr.mean()), 1),
            "se": round(se, 1),
            "t": round(float(arr.mean() / se), 2) if se > 0 else None,
            "ci90": [round(float(arr.mean() - 1.645 * se), 1),
                     round(float(arr.mean() + 1.645 * se), 1)],
            "acc_diff_pp": (round(float(np.mean(acc_pairs) * 100), 1)
                            if acc_pairs else None),
            "gated_diff_bp": (round(float(np.mean(gated_pairs)), 1)
                              if gated_pairs else None),
            "note": ("mean within-event difference between models that had "
                     "the event in corpus and models that did not"),
        }

    # --- does the memorisation effect itself track liquidity? --------------
    # The liquidity result in Section 5.2 has an alternative reading: liquid
    # names are the most written-about, so the model may remember them better
    # and the "edge in liquid names" may be recall wearing a microstructure
    # costume. The two stories make opposite predictions HERE. If the edge in
    # liquid names is memory, the in-corpus advantage must be larger there.
    try:
        u = load_universe_v2()[["symbol", "date", "dv"]]
        b2 = bal.merge(u, on=["symbol", "date"], how="inner")
        if len(b2) > 100 and b2["dv"].notna().any():
            cut = float(b2["dv"].median())
            halves = {}
            for tag, sel in (("high", b2["dv"] >= cut), ("low", b2["dv"] < cut)):
                sub = b2[sel].reset_index(drop=True)
                est = twfe(sub, "signed_bp")
                acc = twfe(sub, "correct")
                halves[tag] = {**{k: est[k] for k in
                                  ("estimate", "se", "t", "n_events", "ci90")
                                  if k in est},
                               "accuracy": acc.get("estimate"),
                               "accuracy_se": acc.get("se")}
            halves["cut_musd"] = round(cut / 1e6)
            out["by_liquidity"] = halves
    except Exception as exc:                              # noqa: BLE001
        out["by_liquidity_unavailable"] = f"{type(exc).__name__}: {exc}"

    # --- the memory probe, re-run where recall is impossible ---------------
    mp = memory_probe(getattr(args, "effort", "medium"))
    if mp:
        out["memory_probe"] = mp

    # --- what it would take to settle it -----------------------------------
    # A bound is only as good as the sample behind it, and "we would need more
    # events" is not useful without a number. Standard errors fall as
    # 1/sqrt(n), so the sample needed to reach a target bound scales as the
    # square of the ratio. Stated for a bound of a quarter of the edge, which
    # is the threshold at which memorisation stops being able to account for a
    # meaningful share of the result.
    if out["edge_reference_bp"]:
        target = 0.25 * abs(out["edge_reference_bp"])
        if tight > target > 0:
            need = out["n_events"] * (tight / target) ** 2
            out["power"] = {
                "target_bound_bp": round(target, 1),
                "events_needed": int(round(need, -1)),
                "events_have": out["n_events"],
                "note": ("standard errors scale as 1/sqrt(n), so the sample "
                         "needed grows with the square of the tightening"),
            }
    return out


def stage_report(args) -> None:
    d = compute(args)
    print(f"\n=== out-of-training test, {d['n_events']} events "
          f"{d['span'][0]}..{d['span'][1]}, {d['n_obs']} verdicts ===\n")
    print(f"{'model':12s}{'cutoff':>10}{'in-corpus':>11}{'out':>7}")
    for r in d["models"]:
        print(f"{r['name']:12s}{r['cutoff']:>10}{r['in_corpus_n']:>11}"
              f"{r['out_corpus_n']:>7}")

    print(f"\n{'model':12s}{'corpus':>10}{'n':>6}{'signed bp':>11}"
          f"{'accuracy':>10}{'gated bp':>10}")
    for c in d["cells"]:
        print(f"{c['name']:12s}{'in' if c['in_corpus'] else 'out':>10}"
              f"{c['n']:>6}{c['signed_bp']:>+11.1f}"
              f"{(c['accuracy_pct'] if c['accuracy_pct'] is not None else float('nan')):>9.1f}%"
              f"{(c['gated_bp'] if c['gated_bp'] is not None else float('nan')):>+10.1f}")

    for label, est in d["did"].items():
        if est.get("estimate") is None:
            print(f"\n{label}: {est.get('note')}")
            continue
        eq = est["equivalence"]
        unit = "bp" if label == "signed_bp" else " (share correct)"
        print(f"\ntwo-way FE, outcome = {label}: "
              f"beta = {est['estimate']:+}{unit}  "
              f"(se {est['se']}, t = {est['t']}, {est['n_events']} events)")
        bound = eq.get("tightest_bp", eq.get("tightest_share"))
        print(f"  90% CI {est['ci90']}   -> bounded within +/-{bound}{unit}")
        for r in eq.get("by_margin", []):
            print(f"    +/-{r['margin_bp']:>4}bp  "
                  f"{'EQUIVALENT' if r['holds'] else 'not established'}")

    nv = d.get("naive") or {}
    if nv:
        print(f"\nnaive pooled comparison (NO event effects): "
              f"in-corpus {nv['in_bp']:+}bp vs out {nv['out_bp']:+}bp "
              f"= {nv['diff_bp']:+}bp")
        print("  -> that is calendar time, not memory; the within-event "
              f"estimate above is {d['did']['signed_bp']['estimate']:+}bp")
    ai = d.get("accuracy_implied_bp")
    if ai:
        print(f"\naccuracy translated to bp (x2 x mean |move| "
              f"{d['accuracy_scale_bp']:.0f}bp): {ai['estimate']:+}bp, "
              f"90% CI {ai['ci90']} -> bound +/-{ai['bound']}bp")
    mp = d.get("memory_probe")
    if mp:
        print("\nmemory probe (ticker + date, NO release text), by model:")
        print(f"{'model':12s}{'corpus':>9}{'n':>6}{'volunteered':>13}{'spread':>9}")
        for c in mp["cells"]:
            sp = "—" if c["spread_bp"] is None else f"{c['spread_bp']:+.1f}"
            print(f"{c['name']:12s}{'in' if c['in_corpus'] else 'after':>9}"
                  f"{c['n']:>6}{c['volunteer_pct']:>12.1f}%{sp:>9}")
        print(f"  volunteered a direction: {mp['volunteer_in_pct']}% in-corpus "
              f"(n={mp['volunteer_in_n']}) vs {mp['volunteer_out_pct']}% "
              f"after cutoff (n={mp['volunteer_out_n']})")
        print("  -> " + ("the probe IS a recall instrument: a model with "
                         "nothing to remember declines to answer."
                         if mp.get("probe_is_recall") else
                         "the probe answers even where recall is impossible."))
    h = d.get("head_to_head")
    if h:
        print(f"\npaired within-event difference (had it in corpus minus did "
              f"not), {h['n_events']} events with both:")
        print(f"  per-trade return  {h['diff_bp']:+.1f}bp  (se {h['se']}, "
              f"t = {h['t']}, 90% CI {h['ci90']})")
        if h.get("acc_diff_pp") is not None:
            print(f"  direction correct {h['acc_diff_pp']:+.1f} percentage points")
        if h.get("gated_diff_bp") is not None:
            print(f"  gated leg         {h['gated_diff_bp']:+.1f}bp")
    if d.get("placebo"):
        p = d["placebo"]
        print(f"\nplacebo (same cutoff, different generation): "
              f"{p['diff_bp']:+}bp (se {p['se']}, t = {p['t']}, n = {p['n']})")
    print(f"\nreference: the gate earns {d['edge_reference_bp']:+}bp on these "
          f"events.")
    if d.get("bound_bp") is not None:
        print(f"binding bound: +/-{d['bound_bp']}bp (from {d['bound_source']}), "
              f"{d.get('ratio_of_edge')} of the edge")
    print(f"Verdict: {d['verdict']} ({d['verdict_short']}).")

    doc = Path(__file__).resolve().parents[2] / "docs" / "notes"
    doc.mkdir(parents=True, exist_ok=True)
    out = doc / f"out_of_training_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    lines = [f"# Out-of-training test ({d['span'][0]}..{d['span'][1]})", "",
             f"{d['n_events']} events x {len(d['models'])} models "
             f"= {d['n_obs']} verdicts.", "",
             "| model | corpus | n | signed bp | accuracy | gated bp |",
             "|---|---|---|---|---|---|"]
    for c in d["cells"]:
        lines.append(f"| {c['name']} | {'in' if c['in_corpus'] else 'out'} | "
                     f"{c['n']} | {c['signed_bp']:+.1f} | "
                     f"{c['accuracy_pct']} | {c['gated_bp']} |")
    ref = d["did"]["signed_bp"]
    if ref.get("estimate") is not None:
        lines += ["", f"Two-way FE beta = {ref['estimate']:+}bp "
                      f"(se {ref['se']}, 90% CI {ref['ci90']}).",
                  f"Verdict: {d['verdict']}."]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["report"])
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    {"report": stage_report}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
