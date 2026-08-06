"""What the edge is worth once you have to actually cross the spread.

The study's headline is net of a FLAT 13bp round trip. research_spread_model
measures what the round trip really costs; this module spends those
measurements, three ways:

1. RESTATE. Re-run the gate's returns with each event charged its OWN measured
   quoted spread instead of a constant. This is the honest number.

2. RANDOMISE. A fill is not the midpoint of the quote you happened to
   screenshot. The realised spread is drawn per event, per replication, from
   the EMPIRICAL distribution of measured spreads in the same liquidity and
   move-size bucket — not from a fitted normal. Earnings-night spreads are
   violently right-skewed, and a Gaussian would quietly delete the tail that
   does the damage. Bootstrapping the real distribution keeps it.

3. BREAK EVEN. Solve for the round-trip cost that takes the edge to zero, and
   compare it to what was measured. This is the number that decides whether
   the strategy is tradeable, and it is a single scalar a reader can check.

WHAT THIS IS NOT. It is not a claim to model market impact. Position sizes
here are small relative to post-earnings after-hours volume, and the study has
no order-book depth beyond top-of-book size, so impact beyond the touch is
outside what the data can support. The `aggression` sweep is the stand-in: at
1.0 you pay half the quoted spread on each leg (a fill at the touch), and the
2.0 column asks what happens if you are consistently twice as bad as that.

Run:
    python scripts/research_spread_sim.py report
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

from research_holding_period import paths_file, resolve, scored_events  # noqa: E402
from research_llm_contamination import COSTS_BP  # noqa: E402
from research_spread_model import SPREADS  # noqa: E402

N_REP = 400          # Monte Carlo replications
AGGRESSION = (0.5, 1.0, 2.0)   # fraction of the QUOTED spread paid per round trip
SEED = 20260806


def _panel(effort: str = "medium", panel: str = "v2") -> pd.DataFrame:
    """The scored panel with gross (pre-cost) T+1 returns attached."""
    m = scored_events(effort, False, panel)
    paths = pd.read_parquet(paths_file(panel))
    p = paths.drop(columns=["gated", "side"], errors="ignore").merge(
        m[["symbol", "date", "year", "move_pct", "dv", "direction", "gated"]],
        on=["symbol", "date"], how="inner").reset_index(drop=True)
    p["side"] = np.sign(p["move_pct"]).astype(int)
    ret, _ = resolve(p, np.ones(len(p), int), None, None)
    # resolve() already deducts the flat assumption; add it back so the cost
    # model here is the ONLY cost applied, rather than being stacked on top of
    # a constant that is exactly what it is meant to replace.
    p["gross"] = ret + COSTS_BP / 1e4
    return p


def _with_spreads(p: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Attach measured quoted spreads, imputing the gaps by bucket median.

    An event with no readable book is not an event with a zero spread. Where
    the quote is missing the bucket median stands in, and the share imputed is
    reported rather than absorbed — if most of the panel were imputed, the
    result would be a statement about the imputation, not about the market.
    """
    if not SPREADS.exists():
        raise SystemExit("no measured spreads — run research_spread_model measure")
    cols = ["symbol", "date", "entry_bp", "exit_bp"]
    raw = pd.read_parquet(SPREADS)
    # The ordinary-after-hours control and the decay reading are carried
    # through so the paper can state "post-earnings is wide, but far tighter
    # than ordinary after-hours" as a measurement rather than an intuition.
    for extra in ("quiet_bp", "decay_bp"):
        if extra in raw.columns:
            cols.append(extra)
    p = p.merge(raw[cols], on=["symbol", "date"], how="left")

    p["liq_q"] = pd.qcut(p["dv"].rank(method="first"), 4, labels=False)
    p["mv_q"] = pd.qcut(p["move_pct"].abs().rank(method="first"), 3, labels=False)
    stats = {}
    for col in ("entry_bp", "exit_bp"):
        miss = p[col].isna()
        stats[f"{col}_measured_n"] = int((~miss).sum())
        stats[f"{col}_imputed_n"] = int(miss.sum())
        med = p.groupby(["liq_q", "mv_q"])[col].transform("median")
        p[col] = p[col].fillna(med).fillna(p[col].median())
    # Computed from the pre-fill counts. Reading notna() AFTER the fillna
    # always returns 100% — it measures that the imputation ran, not that the
    # data was there, and printing it as "measured on 100% of events" is a
    # false claim about coverage.
    n_meas = stats["entry_bp_measured_n"]
    stats["measured_pct"] = round(100.0 * n_meas / max(len(p), 1), 1)
    return p, stats


def _bucket_pools(p: pd.DataFrame) -> dict:
    """Empirical spread pools, one per (liquidity, move-size) bucket."""
    pools = {}
    for key, g in p.groupby(["liq_q", "mv_q"]):
        pools[key] = (g["entry_bp"].to_numpy(float),
                      g["exit_bp"].to_numpy(float))
    return pools


def _edge(gross: np.ndarray, cost_bp: np.ndarray, gated: np.ndarray) -> dict:
    net = gross - cost_bp / 1e4
    g, v = net[gated], net[~gated]
    return {"gated_bp": float(g.mean() * 1e4),
            "refused_bp": float(v.mean() * 1e4),
            "spread_bp": float((g.mean() - v.mean()) * 1e4)}


def _alpha_under_costs(p, gross, gated, half, rng, pools, idx_by_key) -> dict:
    """Compound the gated book and regress it on SPY, under three cost
    assumptions plus a Monte Carlo band.

    The benchmark and the sizing rules are imported, not re-implemented, so a
    difference between this table and Section 5.5's is a difference in the
    cost model and nothing else.
    """
    from research_holding_period import account
    from research_llm_contamination import _spy_daily

    g = p[gated].reset_index(drop=True)
    gg = gross[gated]
    hh = half[gated]
    if "edate" not in g.columns:
        g["edate"] = pd.to_datetime(g["date"]).dt.date
    spy = _spy_daily(min(g["edate"]),
                     max(g["edate"]) + pd.Timedelta(days=14).to_pytimedelta())
    spy = spy.sort_values("date")
    sessions, spy_close = spy["date"].to_list(), spy["close"].to_numpy()
    h1 = np.ones(len(g), int)

    def run(cost_bp):
        a = account(g, gg - cost_bp / 1e4, h1, sessions, spy_close)
        return {k: (round(float(v), 2) if isinstance(v, (int, float)) else None)
                for k, v in a.items()
                if k in ("total_pct", "cagr_pct", "max_dd_pct", "sharpe",
                         "alpha_pct", "beta")}

    rows = [{"label": "no costs", "round_trip_bp": 0.0, **run(np.zeros(len(g)))},
            {"label": f"flat {COSTS_BP:.0f}bp (as assumed)",
             "round_trip_bp": COSTS_BP,
             **run(np.full(len(g), float(COSTS_BP)))}]
    for a in AGGRESSION:
        rows.append({"label": f"measured spread x{a:g}",
                     "round_trip_bp": round(float(np.median(hh * a)), 1),
                     **run(hh * a)})

    # Monte Carlo band on the headline (aggression 1.0) case.
    alphas, totals = [], []
    keys = list(zip(p["liq_q"], p["mv_q"]))
    gsel = np.where(gated)[0]
    for _ in range(60):
        cost = np.empty(len(p))
        for k, (epool, xpool) in pools.items():
            i = idx_by_key[k]
            if len(i) == 0 or len(epool) == 0:
                continue
            cost[i] = 0.5 * (rng.choice(epool, size=len(i), replace=True)
                             + rng.choice(xpool, size=len(i), replace=True))
        r = run(cost[gsel])
        alphas.append(r["alpha_pct"])
        totals.append(r["total_pct"])
    del keys
    return {"rows": rows,
            "mc_alpha_mean": round(float(np.mean(alphas)), 2),
            "mc_alpha_p05": round(float(np.percentile(alphas, 5)), 2),
            "mc_alpha_p95": round(float(np.percentile(alphas, 95)), 2),
            "mc_total_p05": round(float(np.percentile(totals, 5)), 1),
            "mc_total_p95": round(float(np.percentile(totals, 95)), 1),
            "mc_negative_alpha_frac": round(
                float(np.mean(np.asarray(alphas) < 0)), 3)}


GROSS_LEVELS = (0.15, 0.30, 0.50, 0.75, 1.00)


def _deployment(p, gross, gated, half) -> dict:
    """CAGR against how much capital is actually deployed.

    Every account figure in this paper runs at 30% average gross exposure —
    a risk cap in the simulation, not a property of the signal. The book is
    idle two thirds of the time, so a reader comparing its CAGR to a
    fully-invested index is comparing two different amounts of risk. This
    sweep makes the dial explicit.

    ONE TRAP, AND IT IS SILENT. account() also caps any single position at
    max_weight, and the per-name cap binds before the gross target does:
    at 2.7 concurrent positions a 20% per-name cap tops out near 54% gross,
    so asking for 100% and reading back the number would report a leverage
    that never happened. max_weight is raised with the target here, and the
    REALISED gross is reported next to the requested one so the reader can
    see whether the cap bound anyway.
    """
    from research_holding_period import account
    from research_llm_contamination import _spy_daily

    if "edate" not in p.columns:
        p = p.assign(edate=pd.to_datetime(p["date"]).dt.date)
    spy = _spy_daily(min(p["edate"]),
                     max(p["edate"]) + pd.Timedelta(days=14).to_pytimedelta())
    spy = spy.sort_values("date")
    sessions, spy_close = spy["date"].to_list(), spy["close"].to_numpy()

    # TWO BOOKS, because sizing a table around the weaker one is misleading.
    # `gross` was computed on the tape's side, so never-standing-down is the
    # same trade with its sign flipped wherever the gate would have refused —
    # no re-resolution needed, and exactly the Section 5.4 policy.
    books = {
        "the gate": (p[gated].reset_index(drop=True),
                     gross[gated] - half[gated] / 1e4),
        "never stand down": (p.reset_index(drop=True),
                             gross * np.where(gated, 1.0, -1.0) - half / 1e4),
    }

    # Every equity figure in Section 5 is drawn on the flat cost assumption.
    # The curves below are the same book priced at the spreads the market
    # actually quoted, so Section 6 can show its own chart rather than asking
    # the reader to mentally discount one drawn twenty pages earlier.
    STRIDE = max(1, len(sessions) // 400)
    out: dict = {"policies": [],
                 "dates": [str(d) for d in sessions[::STRIDE]]}
    for name, (book, ret) in books.items():
        rows, curves = [], {}
        h1 = np.ones(len(book), int)
        for target in GROSS_LEVELS:
            a = account(book, ret, h1, sessions, spy_close,
                        target_gross=target, max_weight=target)
            realised = a["weight_pct"] / 100 * a["avg_concurrent"]
            rows.append({
                "target_gross_pct": round(target * 100),
                "realised_gross_pct": round(realised * 100, 1),
                "per_name_pct": round(a["weight_pct"], 1),
                "total_pct": round(a["total_pct"], 1),
                "cagr_pct": round(a["cagr_pct"], 2),
                "max_dd_pct": round(a["max_dd_pct"], 1),
                "sharpe": round(a["sharpe"], 2),
                "alpha_pct": round(a["alpha_pct"], 2),
            })
            curves[str(round(target * 100))] = [
                round(float(v), 4) for v in a["equity"][::STRIDE]]
        out["policies"].append({"policy": name, "n": int(len(book)),
                                "rows": rows, "curves": curves})
    sc = spy_close[::STRIDE]
    out["spy_curve"] = [round(float(v / spy_close[0]), 4) for v in sc]
    # Kept for the existing renderer: the gate's ladder stays at the top level.
    out["rows"] = out["policies"][0]["rows"]
    cur = spy_close / spy_close[0]
    peak = np.maximum.accumulate(cur)
    out["spy_cagr_pct"] = round(float((cur[-1] ** (252 / len(cur)) - 1) * 100), 2)
    out["spy_max_dd_pct"] = round(float((1 - cur / peak).max() * 100), 1)
    out["note"] = ("gross exposure is a sizing choice, not a finding; "
                   "drawdown scales with it")
    return out


def compute(args=None) -> dict:
    """Everything the paper cites about fill costs. Pure, so build_report_data
    and the dated note cannot disagree."""
    p = _panel()
    p, stats = _with_spreads(p)
    gated = p["gated"].to_numpy(bool)
    gross = p["gross"].to_numpy(float)
    n = len(p)

    out: dict = {"n": n, "n_gated": int(gated.sum()), **stats}
    out["measured"] = {
        "entry_median": round(float(p["entry_bp"].median()), 1),
        "entry_mean": round(float(p["entry_bp"].mean()), 1),
        "entry_p95": round(float(p["entry_bp"].quantile(.95)), 1),
        "exit_median": round(float(p["exit_bp"].median()), 1),
        "exit_mean": round(float(p["exit_bp"].mean()), 1),
        "assumed_bp": COSTS_BP,
    }
    # Ordinary after-hours in the same names, matched pair by pair. This is the
    # control that turns "the post-earnings book is wide" into the more useful
    # "the post-earnings book is wide but much tighter than it would otherwise
    # be, because the whole market is watching this one stock".
    if "quiet_bp" in p.columns:
        both = p[["entry_bp", "quiet_bp"]].dropna()
        if len(both) > 30:
            ratio = (both["entry_bp"] / both["quiet_bp"]).replace(
                [np.inf, -np.inf], np.nan).dropna()
            out["measured"]["quiet_median"] = round(float(both["quiet_bp"].median()), 1)
            out["measured"]["quiet_mean"] = round(float(both["quiet_bp"].mean()), 1)
            out["measured"]["quiet_n"] = int(len(both))
            out["measured"]["ratio_median"] = round(float(ratio.median()), 2)
            out["measured"]["tighter_pct"] = round(float((ratio < 1).mean() * 100), 0)
    if "decay_bp" in p.columns and p["decay_bp"].notna().sum() > 30:
        out["measured"]["decay_median"] = round(float(p["decay_bp"].median()), 1)

    # --- histogram for the figure ------------------------------------------
    # Fixed, unequal bins: the distribution is right-skewed over two orders of
    # magnitude, so equal-width bins would put everything in the first bar.
    edges = [0, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, float("inf")]
    labels = ["<10", "10-20", "20-30", "30-50", "50-75", "75-100",
              "100-150", "150-200", "200-300", "300-500", "500+"]
    counts, _ = np.histogram(p["entry_bp"].to_numpy(float),
                             bins=[e if np.isfinite(e) else 1e12 for e in edges])
    out["hist"] = [{"label": lb, "n": int(c)} for lb, c in zip(labels, counts)]
    # Where the flat assumption falls, in bar-index units, so the figure can
    # draw it as a line THROUGH the measured distribution rather than beside it.
    pos = None
    for i in range(len(edges) - 1):
        if edges[i] <= COSTS_BP < edges[i + 1]:
            width = edges[i + 1] - edges[i]
            pos = i + (COSTS_BP - edges[i]) / width if np.isfinite(width) else i
            break
    out["assumed_bucket"] = round(float(pos), 3) if pos is not None else None

    # --- the flat assumption, restated against the measurement --------------
    # Half the quoted spread on each leg is a fill AT THE TOUCH: the standard
    # benchmark, and the most favourable one that is not fictional.
    half = 0.5 * (p["entry_bp"].to_numpy(float) + p["exit_bp"].to_numpy(float))
    out["implied_round_trip_bp"] = round(float(np.median(half)), 1)

    out["by_aggression"] = []
    for a in AGGRESSION:
        e = _edge(gross, half * a, gated)
        out["by_aggression"].append({"aggression": a,
                                     "round_trip_bp": round(float(np.median(half * a)), 1),
                                     **{k: round(v, 1) for k, v in e.items()}})

    # --- flat-cost sweep: where does the edge die? -------------------------
    # Reported as a curve, not a single number, because a reader's own cost
    # assumption is probably not this paper's.
    out["cost_curve"] = []
    for c in (0, 13, 25, 50, 75, 100, 150, 200, 300):
        e = _edge(gross, np.full(n, float(c)), gated)
        out["cost_curve"].append({"cost_bp": c, **{k: round(v, 1) for k, v in e.items()}})

    # Break-even: the flat round trip at which the GATED leg reaches zero.
    # The selection spread is cost-invariant (both legs pay the same), so it is
    # the gated leg's own profitability that a cost assumption can kill.
    out["breakeven_bp"] = round(float(gross[gated].mean() * 1e4), 1)

    # --- Monte Carlo: draw the realised spread per event, per replication ---
    rng = np.random.default_rng(SEED)
    pools = _bucket_pools(p)
    keys = list(zip(p["liq_q"], p["mv_q"]))
    idx_by_key = {k: np.where([kk == k for kk in keys])[0] for k in pools}

    reps_gated, reps_spread, reps_cost = [], [], []
    for _ in range(N_REP):
        cost = np.empty(n)
        for k, (epool, xpool) in pools.items():
            i = idx_by_key[k]
            if len(i) == 0 or len(epool) == 0:
                continue
            draw_e = rng.choice(epool, size=len(i), replace=True)
            draw_x = rng.choice(xpool, size=len(i), replace=True)
            cost[i] = 0.5 * (draw_e + draw_x)
        e = _edge(gross, cost, gated)
        reps_gated.append(e["gated_bp"])
        reps_spread.append(e["spread_bp"])
        reps_cost.append(float(np.median(cost)))

    def pct(a, q):
        return round(float(np.percentile(a, q)), 1)

    out["mc"] = {
        "reps": N_REP,
        "gated_mean": round(float(np.mean(reps_gated)), 1),
        "gated_p05": pct(reps_gated, 5), "gated_p95": pct(reps_gated, 95),
        "spread_mean": round(float(np.mean(reps_spread)), 1),
        "spread_p05": pct(reps_spread, 5), "spread_p95": pct(reps_spread, 95),
        "cost_mean": round(float(np.mean(reps_cost)), 1),
        "loss_frac": round(float(np.mean(np.asarray(reps_gated) < 0)), 3),
    }

    # --- account-level alpha, which is what an investor actually gets -------
    # Per-trade basis points do not answer "would this have beaten the index".
    # Compound the gated book through the real calendar under each cost
    # assumption and regress the daily series on SPY, exactly as Section 5.5
    # does, so the two are comparable line for line.
    out["alpha"] = _alpha_under_costs(p, gross, gated, half, rng, pools,
                                      idx_by_key)
    out["deployment"] = _deployment(p, gross, gated, half)

    # --- the same picture per year, because liquidity is not stationary -----
    out["by_year"] = []
    for year, g in p.groupby("year"):
        i = g.index.to_numpy()
        if len(i) < 20:
            continue
        h = 0.5 * (g["entry_bp"].to_numpy(float) + g["exit_bp"].to_numpy(float))
        e = _edge(gross[i], h, gated[i])
        out["by_year"].append({
            "year": year, "n": len(i),
            "entry_median": round(float(g["entry_bp"].median()), 1),
            "round_trip_bp": round(float(np.median(h)), 1),
            **{k: round(v, 1) for k, v in e.items()}})

    return out


def stage_report(args) -> None:
    d = compute(args)
    m = d["measured"]
    print(f"\n=== fills, {d['n']} events ({d['n_gated']} gated) ===")
    print(f"measured on {d['measured_pct']}% of events; the rest imputed by "
          f"liquidity x move-size bucket median")
    print(f"\nentry quoted spread   median {m['entry_median']:7.1f}bp   "
          f"mean {m['entry_mean']:7.1f}bp   p95 {m['entry_p95']:7.1f}bp")
    print(f"exit quoted spread    median {m['exit_median']:7.1f}bp   "
          f"mean {m['exit_mean']:7.1f}bp")
    print(f"\nimplied round trip at the touch: {d['implied_round_trip_bp']}bp "
          f"against the {m['assumed_bp']:.0f}bp this study assumed "
          f"({d['implied_round_trip_bp'] / m['assumed_bp']:.1f}x)")
    print(f"\n{'aggression':>11}{'round trip':>12}{'gated':>10}{'refused':>10}{'selection':>11}")
    for r in d["by_aggression"]:
        print(f"{r['aggression']:>11.1f}{r['round_trip_bp']:>11.1f}bp"
              f"{r['gated_bp']:>+9.1f}{r['refused_bp']:>+10.1f}"
              f"{r['spread_bp']:>+10.1f}")
    mc = d["mc"]
    print(f"\nMonte Carlo ({mc['reps']} reps, spreads bootstrapped by bucket):")
    print(f"  gated edge   {mc['gated_mean']:+.1f}bp  "
          f"[{mc['gated_p05']:+.1f}, {mc['gated_p95']:+.1f}] 90% interval")
    print(f"  selection    {mc['spread_mean']:+.1f}bp  "
          f"[{mc['spread_p05']:+.1f}, {mc['spread_p95']:+.1f}]")
    print(f"  replications with a LOSING gated leg: {mc['loss_frac'] * 100:.1f}%")
    print(f"\nbreak-even round trip (gated leg to zero): {d['breakeven_bp']:.1f}bp")
    al = d.get("alpha") or {}
    if al.get("rows"):
        print(f"\n{'cost assumption':30s}{'round trip':>12}{'total':>10}"
              f"{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}{'alpha':>9}")
        for r in al["rows"]:
            print(f"{r['label']:30s}{r['round_trip_bp']:>11.1f}bp"
                  f"{r['total_pct']:>+9.1f}%{r['cagr_pct']:>7.2f}%"
                  f"{r['max_dd_pct']:>7.1f}%{r['sharpe']:>8.2f}"
                  f"{r['alpha_pct']:>+8.2f}%")
        print(f"\n  Monte Carlo alpha vs SPY: {al['mc_alpha_mean']:+.2f}%/yr "
              f"[{al['mc_alpha_p05']:+.2f}, {al['mc_alpha_p95']:+.2f}], "
              f"negative in {al['mc_negative_alpha_frac'] * 100:.0f}% of runs")
    dep = d.get("deployment") or {}
    for pol in dep.get("policies", []):
        print(f"\n{pol['policy']} ({pol['n']} trades)")
        print(f"{'gross':>10}{'per name':>10}{'total':>11}{'CAGR':>8}"
              f"{'maxDD':>8}{'Sharpe':>8}")
        for r in pol["rows"]:
            print(f"{r['realised_gross_pct']:>9.0f}%{r['per_name_pct']:>9.1f}%"
                  f"{r['total_pct']:>+10.0f}%{r['cagr_pct']:>7.1f}%"
                  f"{r['max_dd_pct']:>7.1f}%{r['sharpe']:>8.2f}")
    if dep:
        print(f"{'SPY':>10}{'—':>10}{'':>11}{dep['spy_cagr_pct']:>7.1f}%"
              f"{dep['spy_max_dd_pct']:>7.1f}%")

    doc = Path(__file__).resolve().parents[2] / "docs" / "notes"
    doc.mkdir(parents=True, exist_ok=True)
    out = doc / f"spread_sim_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    lines = ["# Fill costs, measured", "",
             f"- events {d['n']} ({d['n_gated']} gated); "
             f"{d['measured_pct']}% with a measured book",
             f"- entry quoted spread median {m['entry_median']}bp, "
             f"exit {m['exit_median']}bp",
             f"- implied round trip at the touch {d['implied_round_trip_bp']}bp "
             f"vs {m['assumed_bp']:.0f}bp assumed",
             f"- break-even round trip {d['breakeven_bp']}bp", "",
             "| aggression | round trip | gated | refused | selection |",
             "|---|---|---|---|---|"]
    for r in d["by_aggression"]:
        lines.append(f"| {r['aggression']} | {r['round_trip_bp']}bp | "
                     f"{r['gated_bp']:+.1f} | {r['refused_bp']:+.1f} | "
                     f"{r['spread_bp']:+.1f} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["report"])
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--panel", default="v2", choices=["v1", "v2"])
    args = ap.parse_args()
    {"report": stage_report}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
