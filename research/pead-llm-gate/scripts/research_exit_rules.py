"""Exit-rule search: what raises Sharpe when the hit rate is only 50-60%.

THE PREMISE. At a 55% win rate on ±5% earnings reactions, the entry signal is
close to a coin flip and almost all of the risk-adjusted return has to come
from the exit — from cutting the losers faster than the winners. This module
sweeps exit rules over the SAME panel and the SAME account discipline the
flagship uses (6 slots, research_holding_period.account_slots), so a Sharpe
here is comparable to the 2.148 in the paper.

THE FAMILIES, and what each one is a bet on:

  plain        fixed T+1..T+5. The null.
  guidance     T+k when the release moved guidance, T+1 otherwise. This is
               what the flagship already does; it is here as the baseline
               to beat, not as a candidate.
  reversal     ADVERSE-REVERSAL EXIT. Hold to T+k only if the first close is
               already in your favour; otherwise take the T+1 close and go.
               The bet is that a reaction that fails on day one is a
               reaction that was wrong, and the drift never arrives.
  runner       The mirror: hold to T+k only if day one gained at least X%,
               otherwise exit T+1. A bet on momentum confirmation rather
               than on cutting failure.
  bracket      Static stop/target, first-touch, intraday resolution.
  conf         Horizon conditioned on the model's own stated confidence.
  combo        reversal + bracket together.

NO LOOKAHEAD. Every rule keys only on information available at the moment it
acts: the first session's close is observed before deciding whether to hold a
second session. Stops and targets use first-passage times from the cached
paths and only count if touched BEFORE the horizon's close, which is
resolve()'s existing semantics, not a new one.

THE HEALTH WARNING, which matters more here than anywhere else in the study.
This is a search over ~100 exit rules on 1,787 events. The winner of a search
that size is partly a winner because it is lucky. Everything is reported
train (2021-23) / test (2024-26) and sorted by TEST Sharpe, and any rule
whose train and test disagree in sign should be read as noise regardless of
how good the headline number looks. The study has already learned once that
a 2.9 Sharpe became 1.7 under a timing repair; an exit rule chosen on this
grid deserves at least that much scepticism.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from research_holding_period import (
    FULL_MUT,
    SLOTS,
    account_slots,
    mutation_sides,
    paths_file,
    resolve,
    scored_events,
)
from research_llm_contamination import _spy_daily


def build(effort: str = "medium", panel: str = "v2") -> tuple:
    paths = pd.read_parquet(paths_file(panel))
    # mutation_sides() reads direction and both consensus fields as well as
    # the ones the exit rules key on, so the projection has to carry them.
    meta = scored_events(effort, False, panel)[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated", "direction",
         "eps_vs_consensus", "revenue_vs_consensus"]]
    p = (paths.drop(columns=["gated"], errors="ignore")
         .merge(meta, on=["symbol", "date"], how="inner").reset_index(drop=True))
    lo, hi = min(p["edate"]), max(p["edate"])
    spy = _spy_daily(lo, hi + pd.Timedelta(days=14).to_pytimedelta()).sort_values("date")
    return p, spy["date"].to_list(), spy["close"].to_numpy()


def day1_ret(p: pd.DataFrame, side: np.ndarray) -> np.ndarray:
    """Side-signed return at the FIRST session close. Observable before the
    decision to hold a second session, which is what makes the reversal
    family legal."""
    c0 = np.array([c[0] if len(c) else np.nan for c in p["closes"]])
    return side * (c0 / p["entry"].to_numpy() - 1)


def rules(p: pd.DataFrame, side: np.ndarray) -> list[tuple]:
    n = len(p)
    d1 = day1_ret(p, side)
    guid = np.isin(p["guidance"].to_numpy(), ["raised", "lowered"])
    conf = p["confidence"].to_numpy()
    ones = np.ones(n, int)
    out: list[tuple] = []

    for k in (1, 2, 3, 4, 5):
        out.append((f"plain T+{k}", ones * k, None, None))
    for k in (2, 3, 4, 5):
        out.append((f"guidance T+{k}/T+1", np.where(guid, k, 1), None, None))
    # ADVERSE REVERSAL: only stay if day one already went your way.
    for k in (2, 3, 4, 5):
        out.append((f"reversal hold T+{k} if d1>0", np.where(d1 > 0, k, 1),
                    None, None))
    for thr in (0.01, 0.02, 0.03):
        for k in (3, 5):
            out.append((f"runner T+{k} if d1>{thr*100:.0f}%",
                        np.where(d1 > thr, k, 1), None, None))
    # Reversal with a tolerance band: a tiny red day is not a failed reaction.
    for tol in (-0.01, -0.02):
        for k in (3, 5):
            out.append((f"reversal T+{k} if d1>{tol*100:.0f}%",
                        np.where(d1 > tol, k, 1), None, None))
    for k in (3, 5):
        out.append((f"conf T+{k} if high/med", np.where(
            np.isin(conf, ["high", "medium"]), k, 1), None, None))
        out.append((f"conf T+{k} if high", np.where(conf == "high", k, 1),
                    None, None))
    # Static brackets on the two strongest bases.
    bases = [("T+1", ones), ("T+3", ones * 3), ("T+5", ones * 5),
             ("gd T+3/T+1", np.where(guid, 3, 1)),
             ("rev T+3", np.where(d1 > 0, 3, 1)),
             ("rev T+5", np.where(d1 > 0, 5, 1))]
    for bname, hz in bases:
        for stop in (None, 5.0, 8.0, 10.0, 15.0):
            for tgt in (None, 10.0, 15.0, 20.0, 30.0):
                if stop is None and tgt is None:
                    continue
                out.append((
                    f"{bname} sl={stop or '-'} tp={tgt or '-'}", hz,
                    None if stop is None else np.full(n, stop),
                    None if tgt is None else np.full(n, tgt)))
    return out


def main() -> None:
    p, sessions, spy_close = build()
    side = mutation_sides(p).get(FULL_MUT)
    if side is None:
        raise SystemExit("FULL POLICY side vector unavailable")
    live = side != 0
    yr = p["year"].to_numpy()
    train = np.isin(yr, ["2021", "2022", "2023"])
    test = np.isin(yr, ["2024", "2025", "2026"])

    rows = []
    for name, hz, stop, tgt in rules(p, side):
        ret, _ = resolve(p.assign(side=side), hz, stop, tgt)
        m = live
        a = account_slots(p[m].reset_index(drop=True), ret[m], hz[m],
                          sessions, spy_close, n_slots=SLOTS)
        def sh(mask):
            mm = m & mask
            if mm.sum() < 30:
                return np.nan, np.nan, 0
            aa = account_slots(p[mm].reset_index(drop=True), ret[mm], hz[mm],
                               sessions, spy_close, n_slots=SLOTS)
            return aa["sharpe"], ret[mm].mean() * 1e4, int(mm.sum())
        s_tr, bp_tr, n_tr = sh(train)
        s_te, bp_te, n_te = sh(test)
        rows.append({"rule": name, "sharpe_all": a["sharpe"],
                     "cagr": a["cagr_pct"], "maxdd": a["max_dd_pct"],
                     "bp": ret[m].mean() * 1e4,
                     "win%": (ret[m] > 0).mean() * 100,
                     "sh_train": s_tr, "sh_test": s_te,
                     "n": int(m.sum()), "avg_hz": hz[m].mean()})
    df = pd.DataFrame(rows).sort_values("sh_test", ascending=False)
    pd.set_option("display.width", 200)
    print(f"\n{int(live.sum())} live events, FULL POLICY sides, {SLOTS} slots\n")
    print("=== TOP 20 BY TEST SHARPE (2024-26) ===")
    print(df.head(20).to_string(index=False,
          float_format=lambda v: f"{v:.2f}"))
    print("\n=== the baselines, for reference ===")
    base = df[df["rule"].str.match(r"plain|guidance T\+3/T\+1")]
    print(base.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print("\n=== rules where train and test DISAGREE in sign (discard these) ===")
    bad = df[(df["sh_train"] * df["sh_test"] < 0)]
    print(f"{len(bad)} of {len(df)} rules")
    df.to_csv("exit_rules_sweep.csv", index=False)
    print("\nwrote exit_rules_sweep.csv")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
