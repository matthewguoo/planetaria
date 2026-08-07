"""Capacity: how many positions the account must hold at once, and what a
finite number of slots costs.

THE PROBLEM WITH THE ACCOUNT SIMULATION AS IT STANDS. `account()` sizes every
position at target_gross / AVERAGE concurrency and takes every signal. That
makes horizons comparable, which is what it was written for, but average
concurrency is not what an account has to fund. Earnings cluster into season:
under the flat T+1 book the mean is 2.7 concurrent positions and the maximum
is 10, so a book sized to average 30% gross is carrying 110% on its busiest
day. Under the conditional exit rule, which holds some names three sessions,
the peak is 17 and the requirement is 146%. Neither figure appears anywhere
in the paper, and both are leverage.

WHAT THIS MODULE DOES INSTEAD. Fix a number of slots S and a gross budget.
Each position takes one slot and is sized at gross / S, so the book is at
gross when every slot is busy and never above it. A position occupies its
slot for the whole holding period. An event arriving when no slot is free
for every session it would need is SKIPPED, and the skip is counted.

That converts hidden leverage into visible coverage, which is the honest
trade: the account is now fundable by construction, and the cost shows up
as trades not taken rather than as margin nobody budgeted for.

WHICH EVENT WINS A CONTESTED SLOT. Prior-session dollar volume, descending
-- the same rule the live watchlist uses to rank names. Ordering by anything
that correlates with the outcome would be lookahead dressed as capacity
management.

THE TENSION THE SWEEP RESOLVES. Few slots means large positions and high
utilisation but many skipped events. Many slots means nothing is skipped but
each position is too small to matter and most slots sit idle. Total return
scales with taken/S, which favours few; risk-adjusted return also wants
diversification, which favours many. The optimum is an empirical question and
is what `sweep` reports.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from research_conditional_exit import FLOOR_MUSD, FULL, LLMDIR, _panel
from research_holding_period import mutation_sides, resolve

MAX_SLOTS = 24


def simulate(p: pd.DataFrame, ret: np.ndarray, hz: np.ndarray, side: np.ndarray,
             sessions: list, n_slots: int, gross: float,
             spy_close: np.ndarray | None = None) -> dict:
    """One pass through the calendar with a hard slot ceiling."""
    index = {d: i for i, d in enumerate(sessions)}
    ent = np.array([index.get(d, -1) for d in p["edate"]])
    live = (ent >= 0) & (side != 0)
    n = len(sessions)

    # Contested slots go to the most liquid name, which is the live rule.
    dv = p["dv"].to_numpy()
    order = np.lexsort((-np.nan_to_num(dv), ent))

    occ = np.zeros(n, dtype=int)
    taken = np.zeros(len(p), dtype=bool)
    weight = gross / n_slots
    daily = np.zeros(n)
    for j in order:
        if not live[j]:
            continue
        a = ent[j]
        b = min(a + int(hz[j]), n - 1)
        # Every session the position would span must have a free slot; a
        # position that cannot be carried to its exit is not a position.
        if occ[a:b + 1].max() >= n_slots:
            continue
        occ[a:b + 1] += 1
        taken[j] = True
        daily[b] += weight * ret[j]

    equity = np.cumprod(1 + daily)
    peak = np.maximum.accumulate(equity)
    years = n / 252
    active = occ[occ > 0]
    out = {
        "slots": n_slots,
        "gross_pct": gross * 100,
        "weight_pct": weight * 100,
        "taken": int(taken.sum()),
        "eligible": int(live.sum()),
        "skipped": int(live.sum() - taken.sum()),
        "skip_pct": 100 * (1 - taken.sum() / max(live.sum(), 1)),
        # Deployed capital averaged over EVERY session, including the idle
        # ones. This is the number an allocator cares about, and it is much
        # lower than the headline gross because the strategy is often flat.
        "deployed_pct": float(occ.mean() * weight * 100),
        "utilisation_pct": float(occ.mean() / n_slots * 100),
        "busy_sessions_pct": 100 * len(active) / n,
        "cagr_pct": float((equity[-1] ** (1 / years) - 1) * 100),
        "total_pct": float((equity[-1] - 1) * 100),
        "max_dd_pct": float((1 - equity / peak).max() * 100),
        "sharpe": float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)),
    }
    if spy_close is not None:
        sr = np.diff(spy_close, prepend=spy_close[0]) / spy_close
        beta, alpha_d = np.polyfit(sr, daily, 1)
        out["beta"] = float(beta)
        out["alpha_annual_pct"] = float(alpha_d * 252 * 100)
    out["_equity"] = equity
    return out


def books(p: pd.DataFrame) -> dict:
    sides = mutation_sides(p)
    full = sides.get(FULL)
    out = {"never stand down": full,
           "pure LLM direction": sides.get(LLMDIR)}
    if full is not None:
        out["never stand down, $100M floor"] = np.where(
            p["dv"].to_numpy() >= FLOOR_MUSD * 1e6, full, 0)
    return {k: v for k, v in out.items() if v is not None}


def compute(args=None) -> dict:
    effort = getattr(args, "effort", None) or "medium"
    panel = getattr(args, "panel", None) or "v2"
    gross = float(getattr(args, "gross", None) or 0.30)
    from research_llm_contamination import _spy_daily

    p = _panel(effort, bool(getattr(args, "all_events", False)), panel)
    spy = _spy_daily(min(p["edate"]),
                     max(p["edate"]) + pd.Timedelta(days=14).to_pytimedelta())
    spy = spy.sort_values("date")
    sessions, spy_close = spy["date"].to_list(), spy["close"].to_numpy()

    moved = np.isin(p["guidance"].to_numpy(), ["raised", "lowered"])
    rules = {"T+1 flat": np.ones(len(p), int),
             "T+1, T+3 if guidance moved": np.where(moved, 3, 1)}

    out: dict = {"gross_pct": gross * 100, "sweeps": {}, "n_sessions": len(sessions)}
    for bname, side in books(p).items():
        for rname, hz in rules.items():
            ret, _ = resolve(p.assign(side=side), hz, None, None)
            rows = []
            for s in range(1, MAX_SLOTS + 1):
                r = simulate(p, ret, hz, side, sessions, s, gross, spy_close)
                r.pop("_equity", None)
                rows.append(r)
            out["sweeps"][f"{bname} | {rname}"] = rows
    return out


def _best(rows: list, key: str) -> dict:
    return max(rows, key=lambda r: r[key])


def report(args=None) -> None:
    r = compute(args)
    for name, rows in r["sweeps"].items():
        print("=" * 100)
        print(f"{name}   (gross budget {r['gross_pct']:.0f}%, "
              f"{rows[0]['eligible']} eligible events)")
        print("=" * 100)
        print(f"  {'slots':>6}{'per name':>10}{'taken':>8}{'skipped':>9}"
              f"{'skip%':>7}{'deployed':>10}{'util%':>7}"
              f"{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}")
        for x in rows:
            print(f"  {x['slots']:>6}{x['weight_pct']:>9.1f}%{x['taken']:>8}"
                  f"{x['skipped']:>9}{x['skip_pct']:>6.1f}%"
                  f"{x['deployed_pct']:>9.1f}%{x['utilisation_pct']:>6.1f}%"
                  f"{x['cagr_pct']:>7.1f}%{x['sharpe']:>8.2f}"
                  f"{x['max_dd_pct']:>7.1f}%")
        bc, bs = _best(rows, "cagr_pct"), _best(rows, "sharpe")
        full = next((x for x in rows if x["skip_pct"] < 1.0), None)
        print(f"\n  max CAGR   : {bc['slots']} slots — {bc['cagr_pct']:.1f}% "
              f"at Sharpe {bc['sharpe']:.2f}, {bc['skip_pct']:.1f}% skipped")
        print(f"  max Sharpe : {bs['slots']} slots — {bs['sharpe']:.2f} "
              f"at CAGR {bc['cagr_pct']:.1f}%, {bs['skip_pct']:.1f}% skipped")
        if full:
            print(f"  no skipping: {full['slots']} slots — CAGR "
                  f"{full['cagr_pct']:.1f}%, Sharpe {full['sharpe']:.2f}, "
                  f"only {full['utilisation_pct']:.0f}% of slots used")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["report", "json"])
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--panel", default="v2")
    ap.add_argument("--gross", type=float, default=0.30)
    ap.add_argument("--all-events", dest="all_events", action="store_true")
    a = ap.parse_args()
    if a.stage == "json":
        print(json.dumps(compute(a), indent=2, default=float))
    else:
        report(a)


if __name__ == "__main__":
    main()
