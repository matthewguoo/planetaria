"""Equity curves for a handful of exit rules, driven by EITHER model's verdicts.

The exit sweep (research_exit_rules.py) ranks ~170 rules on the Opus panel.
This module takes the few that mattered and runs them under both models, so
the question "does this exit rule work because earnings reactions behave that
way, or because it was fitted to a contaminated entry signal?" has a picture
attached to it.

Writes JSON (curves + stats) for charting. Never writes into paper/ or cache/
— the payload path is redirected before anything runs.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

import research_holding_period as hp
from research_llm_contamination import _load_results as _orig_load
from research_llm_contamination import _spy_daily


def panel_for(model: str):
    hp._load_results = lambda *a, **k: _orig_load(model=model)
    paths = pd.read_parquet(hp.paths_file("v2"))
    meta = hp.scored_events("medium", False, "v2")[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "confidence",
         "guidance", "quality_flags", "gated", "direction",
         "eps_vs_consensus", "revenue_vs_consensus"]]
    p = (paths.drop(columns=["gated"], errors="ignore")
         .merge(meta, on=["symbol", "date"], how="inner").reset_index(drop=True))
    lo, hi = min(p["edate"]), max(p["edate"])
    spy = _spy_daily(lo, hi + pd.Timedelta(days=14).to_pytimedelta()).sort_values("date")
    return p, spy["date"].to_list(), spy["close"].to_numpy()


def rule_set(p: pd.DataFrame, side: np.ndarray):
    n = len(p)
    c0 = np.array([c[0] if len(c) else np.nan for c in p["closes"]])
    d1 = side * (c0 / p["entry"].to_numpy() - 1)
    guid = np.isin(p["guidance"].to_numpy(), ["raised", "lowered"])
    ones = np.ones(n, int)
    return [
        ("T+1 flat", ones, None, None),
        ("flagship (T+3 on guidance)", np.where(guid, 3, 1), None, None),
        ("flagship + 10% target", np.where(guid, 3, 1), None, np.full(n, 10.0)),
        ("adverse-reversal T+3", np.where(d1 > 0, 3, 1), None, None),
        ("adverse-reversal T+3 + 10% target",
         np.where(d1 > 0, 3, 1), None, np.full(n, 10.0)),
        ("T+3 + 10% stop", ones * 3, np.full(n, 10.0), None),
    ]


def run(model: str, mutation: str) -> dict:
    p, sessions, spy_close = panel_for(model)
    sides = hp.mutation_sides(p)
    side = sides.get(mutation)
    if side is None:
        raise SystemExit(f"no mutation {mutation!r}; have: {list(sides)[:8]}")
    live = side != 0
    out = {}
    for name, hz, stop, tgt in rule_set(p, side):
        ret, _ = hp.resolve(p.assign(side=side), hz, stop, tgt)
        a = hp.account_slots(p[live].reset_index(drop=True), ret[live],
                             hz[live], sessions, spy_close, n_slots=hp.SLOTS)
        out[name] = {
            "equity": [round(float(v), 5) for v in a["equity"]],
            "sharpe": round(float(a["sharpe"]), 3),
            "cagr": round(float(a["cagr_pct"]), 2),
            "maxdd": round(float(a["max_dd_pct"]), 2),
            "win": round(float((ret[live] > 0).mean() * 100), 1),
            "bp": round(float(ret[live].mean() * 1e4), 1),
            "n": int(live.sum()),
        }
    out["_meta"] = {"model": model, "mutation": mutation,
                    "sessions": [str(s) for s in sessions],
                    "spy": [round(float(v / spy_close[0]), 5) for v in spy_close]}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="exit_curves.json")
    args = ap.parse_args()
    doc = {}
    for label, model, mut in (
            ("opus_full", "claude-opus-5", hp.FULL_MUT),
            ("phi_full", "phi-4-q8", hp.FULL_MUT),
            ("opus_llmdir", "claude-opus-5",
             "pure LLM direction (gate + fade, tape ignored)"),
            ("phi_llmdir", "phi-4-q8",
             "pure LLM direction (gate + fade, tape ignored)")):
        try:
            doc[label] = run(model, mut)
            m = doc[label]
            print(f"\n=== {label} (n={m['T+1 flat']['n']}) ===")
            for k, v in m.items():
                if k.startswith("_"):
                    continue
                print(f"  {k:36s} sharpe={v['sharpe']:+6.2f} cagr={v['cagr']:+7.2f}% "
                      f"maxDD={v['maxdd']:5.1f}% win={v['win']:4.1f}%")
        except SystemExit as e:
            print(f"{label}: {e}")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
