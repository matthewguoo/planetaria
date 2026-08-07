"""Conditional exits applied to the policy the paper actually concludes for.

The horizon sweep in Section 5.3 found that holding to T+3 when the model
reports guidance raised or lowered, and to T+1 otherwise, beats a flat T+1 on
the gated book -- 23.2% CAGR at Sharpe 1.78 against 22.2% at 1.58. Section
5.4 separately concluded that the gate is the wrong use of the verdicts and
that never standing down dominates it.

Those two conclusions were never combined. The never-stand-down curve resolves
at a flat T+1 (`np.ones(len(p_mut), int)` in stage_best), so the best exit
rule found by the study has only ever been measured on the book the study
argues against.

This tests the cross. It costs no inference: every path and every verdict is
already on disk, and only the horizon vector changes.

WHY THE HORIZON VECTOR IS PASSED TO account() AND NOT ONLY TO resolve().
A longer hold ties capital up for longer, so a rule that sometimes holds
three sessions commits more of the account per trade than one that always
holds one. Passing the conditional vector to resolve() alone would collect
the extra return while pretending it was free. account() takes the same
vector so the capital cost is charged.

THE SELECTION PROBLEM, STATED. `T+1, T+3 if confirmed` peeks: it decides the
horizon from the sign of the T+1 return, which is not known at entry. It is
retained ONLY as a labelled upper bound, never as a candidate. Every other
rule keys off fields present in the verdict at entry time -- guidance,
confidence, quality flags -- or off the tape at entry.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from research_holding_period import (
    account,
    mutation_sides,
    paths_file,
    resolve,
    scored_events,
)

FULL = ("FULL POLICY: with the tape if the verdict agrees, against it "
        "otherwise")
LLMDIR = "pure LLM direction (gate + fade, tape ignored)"
FLOOR_MUSD = 100.0


def _panel(effort: str, all_events: bool, panel: str) -> pd.DataFrame:
    """The FULL panel — every scored event, verdict fields included.

    stage_best's own panel is filtered to gated events, so it cannot express
    a policy that trades refusals. This mirrors the rebuild stage_best does
    for exactly that reason.
    """
    pf = paths_file(panel)
    if not pf.exists():
        raise SystemExit(f"no {pf.name} — run the `paths` stage first")
    meta = scored_events(effort, all_events, panel)[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "dv",
         "direction", "confidence", "eps_vs_consensus",
         "revenue_vs_consensus", "guidance", "quality_flags", "gated"]]
    return (pd.read_parquet(pf)
            .drop(columns=["gated", "side"], errors="ignore")
            .merge(meta, on=["symbol", "date"], how="inner")
            .reset_index(drop=True))


def horizons(p: pd.DataFrame, base1: np.ndarray) -> dict[str, np.ndarray]:
    """Candidate exit rules. Every one except the flagged peeker is
    decidable from information available when the position goes on."""
    n = len(p)
    guid = p["guidance"].to_numpy()
    conf = p["confidence"].to_numpy()
    move = np.abs(p["move_pct"].to_numpy())
    moved = np.isin(guid, ["raised", "lowered"])
    high = conf == "high"
    flags = p["quality_flags"].apply(
        lambda v: len(v) if isinstance(v, (list, np.ndarray)) else 0).to_numpy()

    H = {"T+1 (what the paper runs)": np.ones(n, int)}
    for k in (2, 3, 4):
        H[f"T+{k} flat"] = np.full(n, k)
    H["T+1, T+3 if guidance moved"] = np.where(moved, 3, 1)
    H["T+1, T+2 if guidance moved"] = np.where(moved, 2, 1)
    H["T+1, T+4 if guidance moved"] = np.where(moved, 4, 1)
    H["T+1, T+3 if high confidence"] = np.where(high, 3, 1)
    H["T+1, T+3 if guidance moved AND high conf"] = np.where(moved & high, 3, 1)
    H["T+1, T+3 if guidance moved OR high conf"] = np.where(moved | high, 3, 1)
    H["T+1, T+3 if no quality flags"] = np.where(flags == 0, 3, 1)
    H["T+1, T+3 if move under 10%"] = np.where(move < 10, 3, 1)
    # PEEKS. Reported only to bound what any entry-time rule could reach.
    H["[peek] T+1, T+3 if T+1 was right"] = np.where(base1 > 0, 3, 1)
    return H


def compute(args=None) -> dict:
    effort = getattr(args, "effort", None) or "medium"
    all_events = bool(getattr(args, "all_events", False))
    panel = getattr(args, "panel", None) or "v2"
    from research_llm_contamination import _spy_daily

    p = _panel(effort, all_events, panel)
    spy = _spy_daily(min(p["edate"]),
                     max(p["edate"]) + pd.Timedelta(days=14).to_pytimedelta())
    spy = spy.sort_values("date")
    sessions, spy_close = spy["date"].to_list(), spy["close"].to_numpy()

    sides = mutation_sides(p)
    books = {"never stand down": sides.get(FULL),
             "pure LLM direction": sides.get(LLMDIR)}
    floor = sides.get(FULL)
    if floor is not None:
        books["never stand down, $100M floor"] = np.where(
            p["dv"].to_numpy() >= FLOOR_MUSD * 1e6, floor, 0)

    out: dict = {"books": {}, "panel_n": int(len(p))}
    for book, side in books.items():
        if side is None:
            continue
        live = side != 0
        base1, _ = resolve(p.assign(side=side), np.ones(len(p), int), None, None)
        rows = []
        for name, hz in horizons(p, base1).items():
            ret, _ = resolve(p.assign(side=side), hz, None, None)
            a = account(p[live].reset_index(drop=True), ret[live],
                        hz[live], sessions, spy_close)
            rows.append({"rule": name,
                         "bp": float(np.mean(ret[live]) * 1e4),
                         "cagr_pct": float(a["cagr_pct"]),
                         "sharpe": float(a["sharpe"]),
                         "max_dd_pct": float(a["max_dd_pct"]),
                         "mean_hold": float(np.mean(hz[live])),
                         "n": int(live.sum()),
                         "peek": name.startswith("[peek]")})
        out["books"][book] = rows
    return out


def report(args=None) -> None:
    r = compute(args)
    for book, rows in r["books"].items():
        base = rows[0]
        print("=" * 92)
        print(f"{book}   (n={base['n']} positions)")
        print("=" * 92)
        print(f"  {'exit rule':<44}{'bp':>8}{'CAGR':>8}{'Sharpe':>8}"
              f"{'maxDD':>8}{'hold':>7}   vs base")
        for row in rows:
            d = row["sharpe"] - base["sharpe"]
            mark = "" if row is base else f"{d:+.2f} Sharpe"
            if row["peek"]:
                mark += "   <- PEEKS, not investable"
            print(f"  {row['rule']:<44}{row['bp']:>8.1f}{row['cagr_pct']:>7.1f}%"
                  f"{row['sharpe']:>8.2f}{row['max_dd_pct']:>7.1f}%"
                  f"{row['mean_hold']:>7.2f}   {mark}")
        live = [x for x in rows if not x["peek"]]
        best = max(live, key=lambda x: x["sharpe"])
        print(f"\n  best investable: {best['rule']} — Sharpe {best['sharpe']:.2f} "
              f"({best['sharpe'] - base['sharpe']:+.2f}), "
              f"CAGR {best['cagr_pct']:.1f}%")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["report", "json"])
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--panel", default="v2")
    ap.add_argument("--all-events", dest="all_events", action="store_true")
    a = ap.parse_args()
    if a.stage == "json":
        print(json.dumps(compute(a), indent=2, default=float))
    else:
        report(a)


if __name__ == "__main__":
    main()
