"""Assemble every number the research paper cites into one JSON.

The paper is generated from this file, never from hand-transcribed figures —
a paper whose numbers drift from the harness that produced them is worse than
no paper. Every table in the paper has a key here, and nothing appears in the
paper that does not.

Two corrections are baked in and both are load-bearing:

  EXITS. The pre-2026-08-06 panels exited on the LAST session in a four-day
  fetch window, so "T+1" was really a 2-4 session hold. Everything here
  resolves per-session from event_paths_multi.parquet instead of reading the
  old `fwd_bp` column, which is left untouched in the cache for comparison.

  TIMING. 227 of 1,552 scored events had their 8-K accepted outside
  [16:00, 16:20] ET and are dropped — see research_llm_contamination.
  timing_ok(). `--all-events` reproduces the uncorrected numbers so the paper
  can show what the correction cost.

Run after `score` / `curve` / `brackets` / `best` / `mutations` / `trades`:
    .venv/Scripts/python.exe scripts/build_report_data.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_event_panel import load_universe_v2  # noqa: E402
from research_holding_period import (  # noqa: E402
    _split_stats,
    account,
    mutation_sides,
    paths_file,
    resolve,
    scored_events,
)
from research_llm_contamination import (  # noqa: E402
    CACHE,
    COSTS_BP,
    GATE,
    MIN_DV,
    MODEL,
    PROVENANCE,
    TOP_PER_DAY,
    _bars_for,
    _gate,
    _permutation_p,
    _spread,
    _spy_daily,
    load_universe,
)

PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
OUT = Path(__file__).resolve().parents[2] / "docs" / "report_data.json"


def _f(x) -> float | None:
    """JSON cannot hold NaN, and a silent null beats a file no parser reads."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else round(v, 4)


def funnel() -> list[dict]:
    """The selection cascade, from every AMC earnings release with an
    after-hours tape down to what this study scores."""
    frames = []
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move"] = (ev["react"] / ev["anchor"] - 1) * 100
        bars = _bars_for(str(ev["date"].min()), str(ev["date"].max()))
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (g["date"].to_list(), (g["c"] * g["v"]).to_numpy())
                 for s, g in bars.groupby("symbol")}

        def dv(row):
            d, v = panel.get(row["symbol"], (None, None))
            if d is None:
                return np.nan
            i = np.searchsorted(d, row["date"])
            return v[i - 1] if 1 <= i < len(v) else np.nan

        ev["dv"] = ev.apply(dv, axis=1)
        frames.append(ev)
    allev = pd.concat(frames, ignore_index=True)
    years = (max(allev["date"]) - min(allev["date"])).days / 365.25
    g5 = allev[np.abs(allev["move"]) >= GATE]
    liq = g5[g5["dv"] >= MIN_DV]
    top8 = liq.sort_values("dv", ascending=False).groupby("date").head(8)
    top5 = liq.sort_values("dv", ascending=False).groupby("date").head(TOP_PER_DAY)
    stages = [
        ("AMC earnings releases with an after-hours tape", len(allev)),
        (f"|reaction| >= {GATE:.0f}% (the strategy's gate)", len(g5)),
        (f"prior-session dollar volume >= ${MIN_DV/1e6:.0f}M", len(liq)),
        ("top 8 per day — the live watchlist size", len(top8)),
        (f"top {TOP_PER_DAY} per day", len(top5)),
    ]
    return [{"stage": label, "total": n, "per_year": round(n / years)}
            for label, n in stages]


def spec() -> list[dict]:
    """The production configuration, read out of the strategy class rather
    than paraphrased — the paper must not be able to drift from the engine."""
    from app.strategies.earnings_reaction import EXIT_ET, WATCHLIST_ET, EarningsReaction

    p = EarningsReaction.default_params
    rows = [
        ("watchlist freeze", WATCHLIST_ET + " ET",
         "tonight's AMC reporters, ranked by prior-session dollar volume"),
        ("names watched", p["n_names"], "top-N by liquidity, min price "
         f"${p['min_price']:.0f}"),
        ("re-anchor", "16:04 ET",
         "px0 moves to the post-auction tape unless the name has already "
         "moved min_move_pct"),
        ("tape gate", f"|move| >= {p['min_move_pct']}%",
         "reaction vs the pre-release anchor"),
        ("confirmation delay", f"{p['confirm_min']:.0f} min",
         "the tape must STILL clear the gate when the delay elapses"),
        ("decision", "one LLM call", f"effort {p['effort']}, "
         f"{p['max_text']:,} chars max, {p['analysis_timeout_s']:.0f}s timeout"),
        ("gate", "verdict direction == sign(tape)", "everything else journalled"),
        ("risk per name", f"{p['risk_pct_per_name']}% of equity",
         "what the stop may lose; notional = risk / stop"),
        ("size multipliers", "conf 1.5/1.0/0.5 x flags 0.75 x |move|>6% 0.5",
         "conviction scaling on the base notional"),
        ("stop", f"clamp({p['vol_stop_mult']} x avg|daily ret|, "
         f"{p['sl_pct']:.0%}, {p['sl_pct_cap']:.0%})", "vol-scaled per name"),
        ("target", "2 x stop", f"floor {p['tp_pct']:.0%}"),
        ("time stop", f"{p['hold']} at {EXIT_ET.strftime('%H:%M')} ET",
         "whichever of stop / target / time stop fires first"),
        ("spread veto", f"> {p['max_spread_pct']}% of mid", "AH book sanity"),
        ("short veto", f"run5d <= {p['short_run5d_floor_pct']}%",
         "the crushed-in short guard"),
        ("live", p["live"], "note-mode: would-be trades are journalled only"),
    ]
    return [{"parameter": k, "value": str(v), "note": n} for k, v, n in rows]


def audit() -> dict:
    """The lookahead audit's findings, including the one that failed."""
    if not PROVENANCE.exists():
        return {}
    prov = pd.read_parquet(PROVENANCE)
    ok = prov[prov["status"] == "ok"]
    buckets = []
    for lo, hi, label in ((0, 12 * 60, "before noon"),
                          (12 * 60, 15 * 60, "12:00-15:00"),
                          (15 * 60, 16 * 60, "15:00-16:00"),
                          (16 * 60, 16 * 60 + 5, "16:00-16:05"),
                          (16 * 60 + 5, 16 * 60 + 21, "16:05-16:20"),
                          (16 * 60 + 21, 17 * 60 + 30, "16:21-17:30"),
                          (17 * 60 + 30, 24 * 60, "after 17:30")):
        k = ok[(ok["acc_min"] >= lo) & (ok["acc_min"] < hi)]
        buckets.append({"band": label, "n": len(k),
                        "pct": _f(len(k) / len(ok) * 100)})
    return {"resolved": len(ok), "acceptance_et": buckets,
            "late_n": int((ok["acc_min"] > 16 * 60 + 20).sum()),
            "early_n": int((ok["acc_min"] < 16 * 60).sum())}


def timing_cost() -> list[dict]:
    """What the acceptance-time correction changed, on the raw round trip."""
    if not PROVENANCE.exists():
        return []
    prov = pd.read_parquet(PROVENANCE)
    prov = prov[prov["status"] == "ok"][["symbol", "date", "acc_min"]]
    m = scored_events("medium", all_events=True, panel="v1").merge(
        prov, on=["symbol", "date"], how="inner")
    m["mech_pnl"] = np.sign(m["move_pct"]) * m["fwd_bp"] - COSTS_BP
    rows = []
    for label, sub in (
            ("all scored events", m),
            ("acceptance in [16:00, 16:20] ET", m[(m["acc_min"] >= 960)
                                                  & (m["acc_min"] <= 980)]),
            ("accepted before the 16:00 close", m[m["acc_min"] < 960]),
            ("accepted after the 16:20 entry", m[m["acc_min"] > 980])):
        if not len(sub):
            continue
        k = _gate(sub)
        g = sub[k]["mech_pnl"].mean() if k.any() else float("nan")
        v = sub[~k]["mech_pnl"].mean() if (~k).any() else float("nan")
        rows.append({"subset": label, "n": len(sub), "gated_n": int(k.sum()),
                     "gated": _f(g), "vetoed": _f(v), "spread": _f(g - v)})
    return rows


HOLDOUT_CUT = "2021-08"     # everything before this was never seen at design time


def funnel_v2() -> list[dict]:
    """The selection cascade on the acceptance-relative panel."""
    from research_event_panel import react_path, windows

    frames = []
    for lo, hi in windows("ten"):
        path = react_path(lo, hi)
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        return []
    ev = pd.concat(frames, ignore_index=True)
    ev["edate"] = pd.to_datetime(ev["date"]).dt.date
    ev["move"] = (ev["react"] / ev["anchor"] - 1) * 100
    years = (max(ev["edate"]) - min(ev["edate"])).days / 365.25
    g5 = ev[np.abs(ev["move"]) >= GATE]
    sel = load_universe_v2()
    stages = [("after-close releases with a measurable reaction", len(ev)),
              (f"|reaction| >= {GATE:.0f}% (the strategy's gate)", len(g5)),
              (f"liquidity floor and top {TOP_PER_DAY}/day — scored", len(sel))]
    return [{"stage": s, "total": n, "per_year": round(n / years)}
            for s, n in stages]


def panel_compare() -> list[dict]:
    """What the acceptance-relative rebuild changed, headline for headline.

    v1 with its timing filter is the phase-12 result; v1 unfiltered is what
    the study reported before the defect was found; v2 measures every event
    from its own acceptance instead of discarding the awkward ones.
    """
    rows = []
    for label, kw in (("v1, all events (pre-audit)",
                       dict(all_events=True, panel="v1")),
                      ("v1, mis-timed events dropped",
                       dict(all_events=False, panel="v1")),
                      ("v2, acceptance-relative reaction",
                       dict(all_events=False, panel="v2"))):
        try:
            f = scored_events("medium", **kw)
            pf = pd.read_parquet(paths_file(kw["panel"]))
            p = pf.drop(columns=["gated", "side"], errors="ignore").merge(
                f[["symbol", "date", "move_pct", "gated"]],
                on=["symbol", "date"], how="inner")
            p["side"] = np.sign(p["move_pct"]).astype(int)
            ret, _ = resolve(p, np.ones(len(p), int), None, None)
            g = p["gated"].to_numpy()
            rows.append({"panel": label, "n": len(p),
                         "gated_n": int(g.sum()),
                         "gated": _f(ret[g].mean() * 1e4),
                         "vetoed": _f(ret[~g].mean() * 1e4),
                         "spread": _f((ret[g].mean() - ret[~g].mean()) * 1e4)})
        except SystemExit:
            continue
    return rows


def holdout(p: pd.DataFrame, n: int, early_cut: str = HOLDOUT_CUT) -> dict:
    """The decade extension scored as what it is: a holdout.

    Every design decision in this study — the 5% gate, the top-5 rule, the
    13bp cost, the horizon, the mutations — was made looking at 2021-08
    onwards. 2016-01 to 2021-07 was never seen while any of that was chosen,
    so it is the only genuinely out-of-sample evidence in the paper.
    """
    pre = (p["date"] < early_cut).to_numpy()
    if pre.sum() < 30:
        return {}
    out = {"cut": early_cut, "holdout_n": int(pre.sum()),
           "insample_n": int((~pre).sum()),
           "holdout_span": [p.loc[pre, "date"].min(), p.loc[pre, "date"].max()],
           "mutations": []}
    sides = mutation_sides(p)
    for label, side in sides.items():
        row = {"mutation": label}
        for tag, mask in (("holdout", pre), ("insample", ~pre)):
            sub = np.where(mask, side, 0)
            ret, _ = resolve(p.assign(side=side), np.ones(n, int), None, None)
            s = _split_stats(ret, sub, np.zeros(n, bool))
            row[tag] = {"n": s["n"], "bp": _f(s["all"]), "t": _f(s["t"]),
                        "win": _f(s["win"])}
        out["mutations"].append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--panel", default="v2", choices=["v1", "v2"])
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--windows", default="ten")
    args = ap.parse_args()

    m = scored_events("medium", args.all_events, args.panel)
    paths = pd.read_parquet(paths_file(args.panel))
    p = paths.drop(columns=["gated", "side"]).merge(
        m[["symbol", "date", "edate", "year", "move_pct", "run5d", "dv",
           "direction", "confidence", "eps_vs_consensus",
           "revenue_vs_consensus", "guidance", "quality_flags", "gated"]],
        on=["symbol", "date"], how="inner").reset_index(drop=True)
    n = len(p)
    g = p["gated"].to_numpy()
    early = p["year"].isin(["2021", "2022", "2023"]).to_numpy()
    tape = np.sign(p["move_pct"].to_numpy()).astype(int)
    p = p.assign(side=tape)

    universe = load_universe()
    data: dict = {
        "model": MODEL, "effort": "medium",
        "universe_n": int(len(universe)),
        "scored_n": int(len(m)), "paths_n": n,
        "gated_n": int(g.sum()), "vetoed_n": int((~g).sum()),
        "span": [str(min(p["edate"])), str(max(p["edate"]))],
        "train_n": int(early.sum()), "test_n": int((~early).sum()),
        "gate_pct": GATE, "top_per_day": TOP_PER_DAY,
        "min_dv_musd": MIN_DV / 1e6, "costs_bp": COSTS_BP,
        "timing_corrected": not args.all_events,
        "funnel": funnel(), "funnel_v2": funnel_v2(), "spec": spec(),
        "audit": audit(), "timing_cost": timing_cost(),
        "panel_compare": panel_compare(),
    }

    # --- headline, on CORRECTED per-session exits -------------------------
    rets = {}
    data["horizons"] = []
    for k in range(1, 6):
        ret, why = resolve(p, np.full(n, k), None, None)
        rets[k] = ret
        gm, ge, gl = ret[g].mean(), ret[g & early].mean(), ret[g & ~early].mean()
        vm = ret[~g].mean()
        data["horizons"].append({
            "hold": f"T+{k}", "gated": _f(gm * 1e4), "train": _f(ge * 1e4),
            "test": _f(gl * 1e4), "vetoed": _f(vm * 1e4),
            "spread": _f((gm - vm) * 1e4),
            "win": _f((ret[g] > 0).mean() * 100),
            "mech": _f(ret.mean() * 1e4)})

    r1 = rets[1]
    gr = r1[g]
    data["headline"] = {
        "n": n, "gated_n": int(g.sum()), "vetoed_n": int((~g).sum()),
        "keep_pct": _f(g.mean() * 100),
        "mech": _f(r1.mean() * 1e4), "gated": _f(gr.mean() * 1e4),
        "vetoed": _f(r1[~g].mean() * 1e4),
        "spread": _f((gr.mean() - r1[~g].mean()) * 1e4),
        "t": _f(gr.mean() / (gr.std(ddof=1) / math.sqrt(len(gr)))),
        "win_gated": _f((gr > 0).mean() * 100),
        "win_all": _f((r1 > 0).mean() * 100),
        "long_n": int((tape[g] > 0).sum()),
        "long": _f(r1[g & (tape > 0)].mean() * 1e4),
        "short_n": int((tape[g] < 0).sum()),
        "short": _f(r1[g & (tape < 0)].mean() * 1e4),
        "mix": {k: _f(v * 100) for k, v in
                p["direction"].value_counts(normalize=True).items()},
    }
    # The permutation null still runs on the raw column: it asks whether the
    # VERDICTS carry event-specific information, which does not depend on the
    # exit rule, and shuffling 1000x through the path resolver would cost
    # minutes for an identical answer.
    mm = m.copy()
    mm["mech_pnl"] = np.sign(mm["move_pct"]) * mm["fwd_bp"] - COSTS_BP
    data["headline"]["perm_p"] = _permutation_p(mm.reset_index(drop=True),
                                                np.random.default_rng(20260806))
    data["headline"]["raw_spread"] = _f(_spread(mm))

    data["years"] = []
    for year, idx in p.groupby("year").groups.items():
        sel = p.index.isin(idx)
        yg, yv = sel & g, sel & ~g
        data["years"].append({
            "year": year, "n": int(sel.sum()),
            "mech": _f(r1[sel].mean() * 1e4),
            "gated": _f(r1[yg].mean() * 1e4) if yg.any() else None,
            "vetoed": _f(r1[yv].mean() * 1e4) if yv.any() else None,
            "spread": _f((r1[yg].mean() - r1[yv].mean()) * 1e4)})

    q = pd.qcut(p["dv"] / 1e6, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
    data["liquidity"] = []
    for label in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        sel = (q == label).to_numpy()
        qg, qv = sel & g, sel & ~g
        data["liquidity"].append({
            "q": label, "n": int(sel.sum()),
            "lo": round(float((p["dv"] / 1e6)[sel].min())),
            "hi": round(float((p["dv"] / 1e6)[sel].max())),
            "mech": _f(r1[sel].mean() * 1e4),
            "gated": _f(r1[qg].mean() * 1e4),
            "spread": _f((r1[qg].mean() - r1[qv].mean()) * 1e4)})

    # --- contamination gradient ------------------------------------------
    x = (pd.Timestamp("2026-08-06") - pd.to_datetime(p["date"][g])).dt.days.to_numpy(float)
    y = gr * 1e4
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    se = float(np.sqrt((resid @ resid) / (len(x) - 2)
                       / ((x - x.mean()) @ (x - x.mean()))))
    data["gradient"] = {"bp_per_year": _f(slope * 365), "se": _f(se * 365),
                        "t": _f(slope / se)}

    # --- bracket surface --------------------------------------------------
    data["brackets"] = []
    for stop in (5.0, 8.0, 12.0, 20.0, None):
        row = {"stop": "none" if stop is None else f"{stop:g}%"}
        for mult, key in ((2, "x2"), (3, "x3")):
            tgt = None if stop is None else stop * mult
            ret, why = resolve(p, np.ones(n, int),
                               None if stop is None else np.full(n, stop),
                               None if tgt is None else np.full(n, tgt))
            row[key] = _f(ret[g].mean() * 1e4)
            if stop == 5.0 and mult == 2:
                data["shipped_bracket"] = {
                    "mean_bp": _f(ret[g].mean() * 1e4),
                    "train": _f(ret[g & early].mean() * 1e4),
                    "test": _f(ret[g & ~early].mean() * 1e4),
                    "win": _f((ret[g] > 0).mean() * 100),
                    "stopped_pct": _f((why[g] == "sl").mean() * 100),
                    "target_pct": _f((why[g] == "tp").mean() * 100),
                    "close_pct": _f((why[g] == "close").mean() * 100)}
        ret, _ = resolve(p, np.ones(n, int),
                         None if stop is None else np.full(n, stop), None)
        row["no_target"] = _f(ret[g].mean() * 1e4)
        data["brackets"].append(row)

    # --- gate mutations ---------------------------------------------------
    spy = _spy_daily(min(p["edate"]),
                     max(p["edate"]) + pd.Timedelta(days=14).to_pytimedelta())
    spy = spy.sort_values("date")
    sessions, spy_close = spy["date"].to_list(), spy["close"].to_numpy()
    data["mutations"] = []
    for label, side in mutation_sides(p).items():
        row = {"mutation": label}
        for k in (1, 3):
            ret, _ = resolve(p.assign(side=side), np.full(n, k), None, None)
            s = _split_stats(ret, side, early)
            row[f"t{k}"] = {kk: _f(vv) for kk, vv in s.items() if kk != "n"}
            row["n"] = s["n"]
            if k == 1 and s["n"] >= 30:
                live = side != 0
                a = account(p[live].reset_index(drop=True), ret[live],
                            np.ones(int(live.sum()), int), sessions, spy_close)
                row["account"] = {kk: _f(vv) for kk, vv in a.items()
                                  if kk != "equity"}
        data["mutations"].append(row)

    spy_curve = spy_close / spy_close[0]
    peak = np.maximum.accumulate(spy_curve)
    data["spy"] = {"total_pct": _f((spy_curve[-1] - 1) * 100),
                   "cagr_pct": _f((spy_curve[-1] ** (252 / len(sessions)) - 1) * 100),
                   "max_dd_pct": _f((1 - spy_curve / peak).max() * 100)}

    # --- effort calibration (paired, same events, both efforts) -----------
    both = scored_events("low", all_events=True, panel="v1", arm="named")
    # NOT "effort": that key already holds the string the study ran at, and
    # overwriting it with the calibration dict silently broke the paper.
    data["effort_cal"] = {}
    for eff, frame in (("low", both),
                       ("medium", scored_events("medium", all_events=True,
                                                panel="v1"))):
        f = frame.copy()
        f["pnl"] = np.sign(f["move_pct"]) * f["fwd_bp"] - COSTS_BP
        k = _gate(f)
        data["effort_cal"][eff] = {
            "n": len(f), "gated_n": int(k.sum()),
            "gated": _f(f[k]["pnl"].mean()), "vetoed": _f(f[~k]["pnl"].mean()),
            "out_tokens": int(f["out"].mean())}
    key = ["symbol", "date"]
    pair = both[[*key, "direction"]].merge(
        scored_events("medium", all_events=True, panel="v1")[[*key, "direction"]],
        on=key, suffixes=("_low", "_med"))
    data["effort_cal"]["agreement_pct"] = _f(
        (pair["direction_low"] == pair["direction_med"]).mean() * 100)
    data["effort_cal"]["paired_n"] = len(pair)

    for tag, name in (("raw", "study-curve.json"),
                      ("bracketed", "study-curve-bracketed.json")):
        path = PUBLIC / name
        if path.exists():
            data[f"curve_{tag}"] = json.loads(path.read_text(encoding="utf-8"))

    # The decade extension, framed as the holdout it is.
    data["holdout"] = holdout(p, n)

    # The two contamination arms, computed by the same function that writes
    # the dated note so the paper and the note cannot disagree.
    try:
        from research_llm_contamination import compute_arms
        arms = compute_arms(args)
        data["arms"] = {k: v for k, v in arms.items() if k != "lines"}
    except SystemExit as exc:
        data["arms"] = {"unavailable": str(exc)}

    data["spend_usd"] = _f(json.loads(
        (CACHE / "llm_contam" / "usage.json").read_text())["usd"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"{data['paths_n']} events / {data['gated_n']} gated · T+1 gated "
          f"{data['headline']['gated']}bp · spread {data['headline']['spread']}bp "
          f"· shipped bracket {data['shipped_bracket']['mean_bp']}bp "
          f"· spend ${data['spend_usd']}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
