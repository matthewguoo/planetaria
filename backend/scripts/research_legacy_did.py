"""Memorisation, measured against a long pre-period.

THE PROBLEM WITH THE FOUR-OPUS DESIGN. It compares models whose training
cutoffs differ by a few months, so the only events that distinguish them are
the 236 inside the straddle window. Those same 236 events have to identify
two things at once: how much better the newer model is IN GENERAL (skill),
and how much better it is ON EVENTS IT MAY HAVE READ ABOUT (memorisation).
One window, two parameters, and the result was a +-86bp bound -- 61% of the
edge -- which is another way of saying the design could not separate them.

WHAT A LOW CUTOFF BUYS. Haiku 4.5's training data ends 2025-07, ten months
before Opus 5's. That splits the ten-year panel into three regimes:

    R1  2016-01 .. 2025-07   both models in corpus     n=1542
    R2  2025-08 .. 2026-05   Haiku OUT, Opus 5 in      n= 236
    R3  2026-06 .. 2026-07   both models out           n=  37

R1 is the piece the four-Opus design never had. With 1,542 events where
neither model has an informational advantage over the other, the skill gap is
pinned down on its own, and memorisation is what remains: the amount by which
the gap WIDENS in R2, where only Opus 5 could have read the outcome.

    memorisation = (Opus5 - Haiku | R2) - (Opus5 - Haiku | R1)

R3 is a free falsification. With both models out of corpus the gap must fall
back to its R1 level. If it does not, whatever moved in R2 was a change in
the market or the sample, not a change in what the model remembered.

WHY THE ESTIMATOR IS A DIFFERENCE OF MEANS AND NOT A REGRESSION. Both models
score the SAME events, so the within-event difference d_i = y_o5,i - y_h45,i
removes the event effect exactly -- no fixed effects to estimate, no
clustering assumption to defend. Comparing mean(d) across regimes is then an
ordinary two-sample contrast, and its standard error is honest by
construction.

THE ONE ASSUMPTION. Haiku is the weaker model, and the R1 gap absorbs that.
The design survives Haiku being weak, being handicapped to budget_tokens
thinking, and thinking less -- all of it is a CONSTANT that differences out.
It breaks only if the handicap itself varies across regimes, i.e. if Haiku
degrades on recent events for a reason unrelated to corpus membership. R3
is the test of that, which is why it is reported even at n=37.

THE FAILURE MODE THIS SCRIPT MUST DETECT. If Haiku has no directional skill
at all, the DiD is measuring memorisation on a signal that does not exist and
will return a precise, meaningless zero. `skill_check` is not decoration: a
Haiku edge indistinguishable from zero in R1 invalidates the headline number,
and the report says so in that case rather than printing a bound.

Exploratory. Nothing here is wired into the paper's data payload.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from research_llm_contamination import MODELS
from research_out_of_training import panel

RNG = np.random.default_rng(20260806)
N_BOOT = 10_000

STUDY = "claude-opus-5"
LEGACY = "claude-haiku-4-5"

# Regime edges. R1/R2 splits at Haiku's cutoff, R2/R3 at Opus 5's, so each
# boundary is a real corpus boundary for exactly one model.
R1_HI = "2025-07-31"
R2_HI = "2026-05-31"
REGIMES = (("R1", "both in corpus", "2000-01-01", R1_HI),
           ("R2", "Haiku out, Opus 5 in", "2025-08-01", R2_HI),
           ("R3", "both out (placebo)", "2026-06-01", "2030-01-01"))

# "In corpus" is not a uniform state. A 2016 event has been seen in vastly
# more training text -- news, filings, retrospectives, tickers in tables --
# than one from June 2025, which had weeks to be written about before the
# corpus closed. So the full-decade R1 averages a memorisation benefit that
# is itself decaying toward the cutoff, and differencing R2 against that
# average would attribute the decay to the corpus boundary.
#
# R1-LOCAL is the better-matched control: the twelve months IMMEDIATELY
# before Haiku's cutoff. Those events sit at nearly the same corpus depth as
# R2's, in nearly the same market, and differ from R2 in essentially one
# respect -- whether Haiku could have read them. Where the two controls
# disagree, R1-local is the one to believe.
R1_LOCAL_LO = "2024-08-01"

# NOT `gated_bp`. Conditional on a model agreeing with the tape, the gate's
# return is sign(move) * fwd -- a function of the TAPE alone, with the model's
# verdict appearing nowhere in it. So on every event where both models agree
# with the tape, both are handed the identical number and the within-event
# difference is mechanically zero. The first run printed 0.0 +- 0.0 in all
# four regimes, which is what an outcome that cannot vary looks like.
#
# The models differ in WHICH events they gate, not in what a gated event
# pays. `gate_pnl` therefore scores the strategy per EVENT, crediting zero
# when a model declines to trade -- so declining to trade on a loser is a
# win, and coverage and selectivity both enter the outcome.
OUTCOMES = (("signed_bp", "following the model's own direction, bp"),
            ("correct", "directional accuracy, share"),
            ("gate_pnl", "the gate, per event (0 when it stands down), bp"))


def _regime(dates: pd.Series) -> pd.Series:
    d = pd.to_datetime(dates)
    return pd.Series(np.where(d <= R1_HI, "R1",
                              np.where(d <= R2_HI, "R2", "R3")), index=d.index)


def paired(effort: str = "medium") -> pd.DataFrame:
    """One row per event: both models' outcomes, and their difference.

    The inner join is the point. An event only informs the contrast if BOTH
    models scored it; a left join would silently let Opus-only events into
    R1 and reintroduce the composition problem the pairing exists to remove.
    """
    p = panel(effort=effort)
    p = p[p["model"].isin([STUDY, LEGACY])]
    have = set(p["model"].unique())
    missing = {STUDY, LEGACY} - have
    if missing:
        raise SystemExit(f"no verdicts on disk for {', '.join(sorted(missing))}")

    keys = ["symbol", "date"]
    a = p[p["model"] == STUDY].set_index(keys)
    b = p[p["model"] == LEGACY].set_index(keys)
    m = a.join(b, how="inner", lsuffix="_o5", rsuffix="_h45")
    # Standing down earns zero, it does not earn "missing" — see OUTCOMES.
    for suf in ("_o5", "_h45"):
        m[f"gate_pnl{suf}"] = np.where(m[f"agrees{suf}"],
                                       m[f"gated_bp{suf}"], 0.0)
    for col, _ in OUTCOMES:
        m[f"d_{col}"] = m[f"{col}_o5"] - m[f"{col}_h45"]
    m = m.reset_index()
    m["regime"] = _regime(m["date"])
    return m


def _mean_se(x: np.ndarray) -> tuple[float, float, int]:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return (float("nan"), float("nan"), len(x))
    return float(x.mean()), float(x.std(ddof=1) / np.sqrt(len(x))), len(x)


def _boot_diff(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """Percentile CI and two-sided p for mean(a) - mean(b), by resampling
    each regime independently. The regimes are disjoint sets of events, so
    independent resampling is the right null."""
    a = np.asarray(a, float)[np.isfinite(a)]
    b = np.asarray(b, float)[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return (float("nan"), float("nan"), float("nan"))
    draws = np.array([RNG.choice(a, len(a), replace=True).mean()
                      - RNG.choice(b, len(b), replace=True).mean()
                      for _ in range(N_BOOT)])
    lo, hi = np.percentile(draws, [5, 95])
    p = 2 * min((draws <= 0).mean(), (draws >= 0).mean())
    return float(lo), float(hi), float(min(1.0, p))


def skill_check(m: pd.DataFrame) -> dict:
    """Does the legacy model have ANY directional edge in R1?

    If it does not, every number below it is a contrast between a signal and
    noise, and the DiD's tightness is an artefact of the noise being stable.
    """
    r1 = m[m["regime"] == "R1"]
    out = {}
    for who, suf in (("opus5", "_o5"), ("haiku45", "_h45")):
        mu, se, n = _mean_se(r1[f"signed_bp{suf}"])
        acc, ase, an = _mean_se(r1[f"correct{suf}"])
        out[who] = {"signed_bp": mu, "se": se, "n": n,
                    "t": mu / se if se else float("nan"),
                    "accuracy": acc, "accuracy_se": ase, "accuracy_n": an}
    h = out["haiku45"]
    out["legacy_has_signal"] = bool(abs(h["t"]) >= 2.0)
    return out


def _contrast(a: np.ndarray, b: np.ndarray) -> dict:
    """mean(a) - mean(b), bootstrapped. Empty when either side is too thin.

    DEGENERATE flags a sample with no variance at all. A bootstrap resamples
    such a sample into itself every time, so the draw distribution collapses
    onto a point, the interval shrinks to nothing, and a tiny p falls out of
    a comparison that in fact rests on almost no information. The first run
    produced exactly that: the both-out placebo on accuracy had n=14, zero
    variance, and a "significant" p=0.012 that meant nothing whatever.
    """
    ma, _, na = _mean_se(a)
    mb, _, nb = _mean_se(b)
    fa = np.asarray(a, float)[np.isfinite(a)]
    fb = np.asarray(b, float)[np.isfinite(b)]
    degenerate = bool((len(fa) and fa.std() == 0) or (len(fb) and fb.std() == 0))
    if na < 2 or nb < 2:
        nan = float("nan")
        return {"estimate": nan, "lo90": nan, "hi90": nan, "p": nan,
                "n_treat": na, "n_ctrl": nb, "degenerate": degenerate}
    lo, hi, p = _boot_diff(a, b)
    return {"estimate": ma - mb, "lo90": lo, "hi90": hi, "p": p,
            "n_treat": na, "n_ctrl": nb, "degenerate": degenerate}


def did(m: pd.DataFrame, col: str) -> dict:
    """The estimand, its local-control variant, and its placebo."""
    g = {r: m.loc[m["regime"] == r, f"d_{col}"].to_numpy() for r, *_ in REGIMES}
    by_regime = {r: dict(zip(("gap", "se", "n"), _mean_se(g[r]))) for r in g}
    local = m.loc[(m["regime"] == "R1")
                  & (m["date"].astype(str) >= R1_LOCAL_LO),
                  f"d_{col}"].to_numpy()
    by_regime["R1-local"] = dict(zip(("gap", "se", "n"), _mean_se(local)))
    return {"by_regime": by_regime,
            "did": _contrast(g["R2"], g["R1"]),
            "did_local": _contrast(g["R2"], local),
            "placebo": _contrast(g["R3"], g["R1"])}


def event_study(m: pd.DataFrame, col: str = "signed_bp") -> list[dict]:
    """The Opus-minus-Haiku gap, quarter by quarter.

    The DiD asks whether the gap is bigger inside R2. This asks the sharper
    question: does it STEP at 2025-07, or was it already drifting? A true
    corpus effect must appear as a discontinuity at the cutoff and nowhere
    else. A slow drift across the whole decade would produce the same DiD
    number and mean something entirely different.
    """
    q = pd.to_datetime(m["date"]).dt.to_period("Q")
    rows = []
    for period, idx in m.groupby(q).groups.items():
        mu, se, n = _mean_se(m.loc[idx, f"d_{col}"])
        if n < 5:                       # too thin to plot honestly
            continue
        rows.append({"quarter": str(period), "gap": mu, "se": se, "n": n,
                     "regime": str(m.loc[idx, "regime"].iloc[0])})
    return rows


def compute(effort: str = "medium") -> dict:
    m = paired(effort=effort)
    res = {"n_events": int(len(m)),
           "regime_n": {r: int((m["regime"] == r).sum()) for r, *_ in REGIMES},
           "skill": skill_check(m),
           "outcomes": {c: did(m, c) for c, _ in OUTCOMES}}
    # The edge the bound is a fraction OF: the study model's own gated result
    # over the whole panel, which is what the paper reports as the strategy.
    edge, se, n = _mean_se(m["gated_bp_o5"])
    res["study_edge_bp"] = {"mean": edge, "se": se, "n": n}
    res["event_study"] = event_study(m)
    # The bound is the half-width of the 90% CI: the largest memorisation
    # effect the data cannot rule out. Reported for BOTH controls, because
    # the decade control and the local control answer slightly different
    # questions and the honest bound is the wider of the two.
    for src, bp_key, share_key in (
            ("did", "bound_bp", "bound_share_of_edge"),
            ("did_local", "bound_bp_local", "bound_share_of_edge_local")):
        b = res["outcomes"]["signed_bp"][src]
        half = ((b["hi90"] - b["lo90"]) / 2 if np.isfinite(b["hi90"])
                else float("nan"))
        res[bp_key] = half
        res[share_key] = half / edge if edge else float("nan")
    return res


def _fmt(v: float, dp: int = 1) -> str:
    return "n/a" if v is None or not np.isfinite(v) else f"{v:,.{dp}f}"


def report(effort: str = "medium") -> None:
    r = compute(effort=effort)
    line = "=" * 74
    print(line)
    print(f"MEMORISATION DiD  --  {MODELS['o5'][2]} vs {MODELS['h45'][2]}"
          f"  (cutoffs {MODELS['o5'][1]} vs {MODELS['h45'][1]})")
    print(line)
    print(f"paired events scored by both models: {r['n_events']}")
    for tag, desc, _, _ in REGIMES:
        print(f"   {tag}  {desc:<24} n={r['regime_n'][tag]:>5}")

    s = r["skill"]
    print("\n-- does the legacy model have any signal? (R1, where neither "
          "model\n   has a corpus advantage) " + "-" * 18)
    for who in ("opus5", "haiku45"):
        d = s[who]
        print(f"   {who:<9} signed {_fmt(d['signed_bp'])}bp "
              f"(se {_fmt(d['se'])}, t={_fmt(d['t'], 2)}), "
              f"accuracy {_fmt(d['accuracy'], 4)}  n={d['n']}")
    if not s["legacy_has_signal"]:
        print("   *** Haiku's R1 edge is NOT distinguishable from zero (|t|<2).")
        print("   *** The DiD below contrasts a signal against noise. A tight")
        print("   *** bound would reflect stable noise, not absent memorisation.")
    else:
        print("   Haiku carries real directional signal, so the contrast is")
        print("   between two informative models and the DiD is interpretable.")

    for col, desc in OUTCOMES:
        o = r["outcomes"][col]
        dp = 4 if col == "correct" else 1
        print(f"\n-- {desc} " + "-" * max(2, 58 - len(desc)))
        print(f"   {'regime':<10} {'Opus5 - Haiku':>16} {'se':>10} {'n':>6}")
        for tag in ("R1", "R1-local", "R2", "R3"):
            g = o["by_regime"][tag]
            print(f"   {tag:<10} {_fmt(g['gap'], dp):>16} "
                  f"{_fmt(g['se'], dp):>10} {g['n']:>6}")
        for lab, key in (("MEMORISATION  R2 - R1      ", "did"),
                         ("  same, vs local control   ", "did_local"),
                         ("PLACEBO       R3 - R1      ", "placebo")):
            d = o[key]
            warn = "  [DEGENERATE - zero variance, p is meaningless]" \
                if d.get("degenerate") else ""
            print(f"   {lab} {_fmt(d['estimate'], dp):>9}  "
                  f"90% CI [{_fmt(d['lo90'], dp)}, {_fmt(d['hi90'], dp)}]  "
                  f"p={_fmt(d['p'], 3)}{warn}")

    es = r["event_study"]
    print("\n-- the gap quarter by quarter (does it STEP at the cutoff?) ------")
    print("   a corpus effect must be a discontinuity at 2025Q3, not a drift")
    prev = None
    for row in es:
        if prev and prev != row["regime"]:
            print(f"   {'':>8} {'-' * 34}  <-- {prev} ends, {row['regime']} begins")
        bar = "#" * min(28, int(abs(row["gap"]) / 8))
        sign = "+" if row["gap"] >= 0 else "-"
        print(f"   {row['quarter']:>8} {row['gap']:>9.1f}bp n={row['n']:>4} "
              f"{sign}{bar}")
        prev = row["regime"]

    e = r["study_edge_bp"]
    print("\n-- what this does to the bound " + "-" * 43)
    print(f"   study gated edge          {_fmt(e['mean'])}bp (n={e['n']})")
    print(f"   bound, decade control    +-{_fmt(r['bound_bp'])}bp"
          f"  = {_fmt(100 * r['bound_share_of_edge'])}% of the edge")
    print(f"   bound, local control     +-{_fmt(r['bound_bp_local'])}bp"
          f"  = {_fmt(100 * r['bound_share_of_edge_local'])}% of the edge")
    print("   (four-Opus design, for comparison: +-85.9bp = 61% of the edge)")
    print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["report", "json"])
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args()
    if a.stage == "json":
        print(json.dumps(compute(effort=a.effort), indent=2, default=float))
    else:
        report(effort=a.effort)


if __name__ == "__main__":
    main()
