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
from datetime import date, timedelta
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


SIX_MONTHS = "2026-02-01"
CORPUS_CUT = "2026-05"      # Opus 5's stated knowledge boundary


def late_filers(p: pd.DataFrame, ret: np.ndarray, gated: np.ndarray) -> dict:
    """Split every year by whether a fixed [16:00, 16:20] window could see it.

    WHY THIS EXISTS. The per-year table mixes two populations that behave
    differently, and until this was broken out the mixing was invisible. A
    company that files at 16:35 is measurable only on the acceptance-relative
    panel; the fixed-window panel every earlier revision used could not see it
    at all. Roughly a quarter of releases are in that group.

    That matters for reading 2026 in particular. Its headline is negative, and
    a reader who remembers the earlier revisions being strongly positive is
    owed the reason: the earlier numbers were computed on the early-filing
    half only. This table gives both halves on the same basis, so the question
    "is 2026 worse, or measured differently?" has an answer on the page rather
    than in a footnote.
    """
    late = (p["acc_min"].to_numpy() > 16 * 60 + 20)
    rows = []
    for year in sorted(p["year"].unique()):
        y = (p["year"] == year).to_numpy()
        cell = {"year": year, "n": int(y.sum()),
                "late_pct": _f(late[y].mean() * 100)}
        for tag, sel in (("early", y & ~late & gated), ("late", y & late & gated)):
            cell[f"{tag}_n"] = int(sel.sum())
            cell[f"{tag}_bp"] = _f(ret[sel].mean() * 1e4) if sel.sum() >= 8 else None
        rows.append(cell)
    return {"threshold_et": "16:20", "late_share_pct": _f(late.mean() * 100),
            "rows": rows,
            "early_bp": _f(ret[~late & gated].mean() * 1e4),
            "late_bp": _f(ret[late & gated].mean() * 1e4),
            "early_n": int((~late & gated).sum()),
            "late_n": int((late & gated).sum())}


def effort_test(p: pd.DataFrame, gross: np.ndarray) -> dict:
    """Does reasoning effort change the answer? Paired, on identical events.

    The calibration table reports each effort's numbers side by side, which
    invites a comparison it cannot support: the two arms ran on different
    numbers of events, so a gap between them mixes the effect of effort with
    the effect of sampling. This pairs them — same event, both efforts — and
    tests the difference directly, which is the only form in which the
    question has an answer.

    Reported as an equivalence bound as well as a significance test, for the
    same reason Section 5.9 is: "not significant" is not "the same", and the
    interesting claim here is the null one.
    """
    res = _load_results_local()
    arms = {}
    for eff in ("low", "medium"):
        a = res[(res["effort"] == eff) & (res["arm"] == "named")
                & (res["model"] == MODEL)][["symbol", "date", "direction"]]
        arms[eff] = a.drop_duplicates(subset=["symbol", "date"])
    key = ["symbol", "date"]
    both = arms["low"].merge(arms["medium"], on=key,
                             suffixes=("_low", "_medium"))
    m = p[[*key, "move_pct"]].copy()
    m["_i"] = np.arange(len(p))
    both = both.merge(m, on=key, how="inner")
    if len(both) < 100:
        return {}

    i = both["_i"].to_numpy()
    tape = np.sign(both["move_pct"].to_numpy())
    cost = COSTS_BP / 1e4
    # `gross` arrives TAPE-SIGNED — it is the long-or-short return the tape's
    # own direction would have earned. Multiplying it by an arm's side again
    # would apply the sign twice and silently invert every short. Undo it once
    # to recover the raw forward move, then sign it per arm.
    raw = tape * gross[i]
    out: dict = {"paired_n": int(len(both))}
    signed = {}
    for eff in ("low", "medium"):
        side = np.where(both[f"direction_{eff}"] == "bullish", 1,
                        np.where(both[f"direction_{eff}"] == "bearish", -1, 0))
        # A neutral verdict is not a trade, so it earns nothing and pays
        # nothing — charging it the round trip would penalise abstention.
        signed[eff] = np.where(side == 0, 0.0, side * raw - cost)
        agree = (side != 0) & (side == tape)
        g = np.where(agree, tape * raw - cost, np.nan)
        v = np.where(~agree, tape * raw - cost, np.nan)
        out[eff] = {
            "gated_n": int(agree.sum()),
            "gated_bp": _f(np.nanmean(g) * 1e4),
            "spread_bp": _f((np.nanmean(g) - np.nanmean(v)) * 1e4),
            "neutral_pct": _f((side == 0).mean() * 100),
        }
    diff = (signed["medium"] - signed["low"]) * 1e4
    se = float(diff.std(ddof=1) / math.sqrt(len(diff)))
    out["diff"] = {
        "estimate": _f(diff.mean()), "se": _f(se),
        "t": _f(diff.mean() / se) if se else None,
        "ci90": [_f(diff.mean() - 1.645 * se), _f(diff.mean() + 1.645 * se)],
        "bound_bp": _f(max(abs(diff.mean() - 1.645 * se),
                           abs(diff.mean() + 1.645 * se))),
        "significant": bool(abs(diff.mean() / se) > 1.96) if se else False,
    }
    out["agreement_pct"] = _f(
        (both["direction_low"] == both["direction_medium"]).mean() * 100)
    return out


def liquidity_study(p: pd.DataFrame, ret: np.ndarray, gated: np.ndarray) -> dict:
    """Where the liquidity floor should sit, and whether the edge reaches
    past the five names a night the study actually trades.

    Two questions that look alike and are not. The FLOOR asks whether the
    least liquid names the study already scores are worth scoring — answerable
    from the events in hand, for nothing. The RANK WINDOW asks whether the
    sixth through tenth most liquid reporter on a given night carries the same
    edge, which needs verdicts that were never bought, because the top-five
    rule discarded those events before the model ever saw them.

    The second is the more interesting of the two and the smaller sample: most
    nights do not have six qualifying reporters, so the cap rarely binds.
    """
    dvm = (p["dv"] / 1e6).to_numpy()
    out: dict = {"floor_now_musd": MIN_DV / 1e6, "rows": []}
    for floor in (50, 75, 100, 150, 200, 300, 500):
        sel = dvm >= floor
        gs, vs = sel & gated, sel & ~gated
        if gs.sum() < 40 or vs.sum() < 40:
            continue
        gr = ret[gs]
        out["rows"].append({
            "floor_musd": floor, "n": int(sel.sum()), "gated_n": int(gs.sum()),
            "kept_pct": _f(sel.mean() * 100),
            "gated_bp": _f(gr.mean() * 1e4),
            "t": _f(gr.mean() / (gr.std(ddof=1) / math.sqrt(len(gr)))),
            "spread_bp": _f((gr.mean() - ret[vs].mean()) * 1e4)})

    # Deciles, because the floor sweep is cumulative and hides where the
    # weakness actually sits.
    dec = pd.qcut(pd.Series(dvm).rank(method="first"), 10, labels=False).to_numpy()
    out["deciles"] = []
    for d in range(10):
        sel = (dec == d) & gated
        if sel.sum() < 20:
            continue
        out["deciles"].append({
            "decile": d + 1,
            "lo_musd": round(float(dvm[dec == d].min())),
            "hi_musd": round(float(dvm[dec == d].max())),
            "n": int(sel.sum()), "gated_bp": _f(ret[sel].mean() * 1e4)})

    # --- is the liquidity effect really a CONTAMINATION effect? -------------
    # The obvious objection to "the edge lives in liquid names" is that liquid
    # names are also the most written-about, so the model may simply remember
    # them better. That is a real mechanism and it is separately measurable.
    # Three readings settle which story fits: whether recall tracks liquidity,
    # whether the memorisation ADVANTAGE tracks liquidity, and whether the
    # gross edge survives once the quoted spread is taken out separately.
    try:
        from research_spread_model import SPREADS
        if SPREADS.exists():
            sp = pd.read_parquet(SPREADS)[["symbol", "date", "entry_bp", "exit_bp"]]
            q = p[["symbol", "date"]].merge(sp, on=["symbol", "date"], how="left")
            for c in ("entry_bp", "exit_bp"):
                q[c] = q[c].fillna(q[c].median())
            half = 0.5 * (q["entry_bp"] + q["exit_bp"]).to_numpy()
            gross = ret + COSTS_BP / 1e4
            quint = pd.qcut(pd.Series(dvm).rank(method="first"), 5,
                            labels=False).to_numpy()
            out["cost_by_quintile"] = []
            for i in range(5):
                sel = (quint == i) & gated
                if sel.sum() < 30:
                    continue
                out["cost_by_quintile"].append({
                    "q": f"Q{i + 1}",
                    "lo_musd": round(float(dvm[quint == i].min())),
                    "hi_musd": round(float(dvm[quint == i].max())),
                    "spread_bp": _f(half[sel].mean()),
                    "gross_bp": _f(gross[sel].mean() * 1e4),
                    "net_bp": _f((gross[sel] - half[sel] / 1e4).mean() * 1e4)})
    except Exception as exc:                              # noqa: BLE001
        out["cost_by_quintile_unavailable"] = f"{type(exc).__name__}: {exc}"

    # Does the model recall the big names better? The no-text probe answers
    # directly: a volunteered direction with no release in front of it IS the
    # recall, so its rate against liquidity is the cleanest reading available.
    try:
        nt = _load_results_local()
        nt = nt[(nt["arm"] == "notext") & (nt["effort"] == "medium")
                & (nt["model"] == MODEL)]
        nt = nt.merge(p[["symbol", "date", "dv"]], on=["symbol", "date"],
                      how="inner")
        if len(nt) >= 120:
            nt["vol"] = nt["direction"] != "neutral"
            terc = pd.qcut(nt["dv"].rank(method="first"), 3, labels=False)
            out["recall_by_liquidity"] = [
                {"tercile": ["least liquid", "middle", "most liquid"][t],
                 "lo_musd": round(float(g["dv"].min() / 1e6)),
                 "hi_musd": round(float(g["dv"].max() / 1e6)),
                 "n": int(len(g)),
                 "volunteered_pct": _f(g["vol"].mean() * 100)}
                for t, g in nt.groupby(terc)]
    except Exception as exc:                              # noqa: BLE001
        out["recall_by_liquidity_unavailable"] = f"{type(exc).__name__}: {exc}"

    # --- ranks 6-10: names the live watchlist sees and the study skipped ----
    try:
        from research_event_panel import load_universe_v2
        ext = load_universe_v2("ten", rank_lo=6, rank_hi=10)
        res = _load_results_local()
        res = res[(res["effort"] == "medium") & (res["arm"] == "named")]
        paths = pd.read_parquet(paths_file("v2"))
        first = {(s, d): c[0] for s, d, c in
                 zip(paths["symbol"], paths["date"], paths["closes"])}
        ext["exit"] = [first.get((s, d), np.nan) for s, d
                       in zip(ext["symbol"], ext["date"])]
        ext = ext[ext["exit"].notna()]
        e = res.merge(ext[["symbol", "date", "move_pct", "react", "exit", "dv"]],
                      on=["symbol", "date"], how="inner")
        if len(e) >= 60:
            side = np.sign(e["move_pct"].to_numpy())
            fwd = (e["exit"] / e["react"] - 1).to_numpy()
            pnl = side * fwd - COSTS_BP / 1e4
            k = _gate(e).to_numpy()
            gr = pnl[k]
            out["ranks_6_10"] = {
                "n": int(len(e)), "gated_n": int(k.sum()),
                "median_dv_musd": round(float(e["dv"].median() / 1e6)),
                "gated_bp": _f(gr.mean() * 1e4),
                "t": _f(gr.mean() / (gr.std(ddof=1) / math.sqrt(len(gr)))),
                "vetoed_bp": _f(pnl[~k].mean() * 1e4),
                "spread_bp": _f((gr.mean() - pnl[~k].mean()) * 1e4),
                "win_pct": _f((gr > 0).mean() * 100)}
    except Exception as exc:                              # noqa: BLE001
        out["ranks_6_10_unavailable"] = f"{type(exc).__name__}: {exc}"
    return out


def contamination_estimate(panel: str = "v1") -> dict:
    """How much of the edge could be memorisation? Two estimators, both with
    their uncertainty attached, because the honest answer here is an interval
    and not a number.

    STEP TEST. Split the gate at the model's knowledge boundary. If the edge
    is recall, it should be present before the cutoff and absent after it, so
    the contamination effect is spread(in-corpus) - spread(post-cutoff). The
    difficulty is power: the study ends weeks after the boundary, so the
    post-cutoff leg is small and its interval is wide enough to be nearly
    uninformative on its own. Reported anyway, with the interval, because a
    wide interval IS the finding.

    GRADIENT TEST. The continuous version, and much better powered: regress
    gated P&L on event age over the whole sample. Memorisation is not a step
    function — older events are more discussed, more repeated and more
    recallable — so a recalled edge should RISE with age. The slope's sign is
    the informative part, and every gated event contributes to it rather than
    only the handful past the boundary.
    """
    m = scored_events("medium", all_events=False, panel=panel)
    paths = pd.read_parquet(paths_file(panel))
    p = paths.drop(columns=["gated", "side"], errors="ignore").merge(
        m[["symbol", "date", "move_pct", "direction"]],
        on=["symbol", "date"], how="inner")
    if p.empty:
        return {}
    p["side"] = np.sign(p["move_pct"]).astype(int)
    ret, _ = resolve(p, np.ones(len(p), int), None, None)
    p["bp"] = ret * 1e4
    p["gated"] = _gate(p)

    def leg(sub):
        k = sub["gated"]
        if k.sum() < 5 or (~k).sum() < 5:
            return None
        g, v = sub.loc[k, "bp"], sub.loc[~k, "bp"]
        se_g = g.std(ddof=1) / math.sqrt(len(g))
        se_v = v.std(ddof=1) / math.sqrt(len(v))
        return {"n": len(sub), "gated_n": int(k.sum()),
                "gated": _f(g.mean()), "gated_se": _f(se_g),
                "vetoed": _f(v.mean()), "vetoed_se": _f(se_v),
                "spread": _f(g.mean() - v.mean()),
                "spread_se": _f(math.sqrt(se_g ** 2 + se_v ** 2))}

    pre, post = leg(p[p["date"] < CORPUS_CUT]), leg(p[p["date"] >= CORPUS_CUT])
    out = {"cut": CORPUS_CUT, "in_corpus": pre, "post_cutoff": post}
    if pre and post:
        d = pre["spread"] - post["spread"]
        se = math.sqrt(pre["spread_se"] ** 2 + post["spread_se"] ** 2)
        out["step"] = {
            "delta_spread": _f(d), "se": _f(se), "t": _f(d / se if se else 0),
            "ci95": [_f(d - 1.96 * se), _f(d + 1.96 * se)],
            # The number that actually answers the question: how much of the
            # in-corpus edge could be memorisation, at the top of the interval?
            "max_share_pct": _f(min(100.0, max(0.0, (d + 1.96 * se))
                                    / pre["spread"] * 100)),
        }
    # Gradient, on the gated leg only — that is the leg the alpha comes from.
    g = p[p["gated"]]
    if len(g) > 50:
        x = (pd.Timestamp("2026-08-06") - pd.to_datetime(g["date"])).dt.days.to_numpy(float)
        y = g["bp"].to_numpy(float)
        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)
        se = float(np.sqrt((resid @ resid) / (len(x) - 2)
                           / ((x - x.mean()) @ (x - x.mean()))))
        span_yrs = float((x.max() - x.min()) / 365.25)
        out["gradient"] = {
            "bp_per_year": _f(slope * 365), "se": _f(se * 365),
            "t": _f(slope / se if se else 0), "n": len(g),
            "span_years": _f(span_yrs),
            # End-to-end implied difference between the oldest and newest
            # events, which is the memorisation story's own prediction.
            "oldest_vs_newest_bp": _f(slope * 365 * span_yrs),
            "ci95_per_year": [_f((slope - 1.96 * se) * 365),
                              _f((slope + 1.96 * se) * 365)]}
        # THE BOUND. Take the 95% upper end of the age slope and assume the
        # worst case it describes: memorisation contributes nothing at the
        # corpus edge and grows linearly into the past. Then the end-to-end
        # difference is slope_hi x span, and the AVERAGE contribution across
        # the sample is half of that. Expressed against the gated mean, which
        # is the leg the alpha actually comes from.
        hi = (slope + 1.96 * se) * 365
        end_to_end = max(0.0, hi * span_yrs)
        gated_mean = float(g["bp"].mean())
        out["gradient"]["max_avg_bp"] = _f(end_to_end / 2)
        out["gradient"]["max_share_of_gated_pct"] = _f(
            min(100.0, end_to_end / 2 / gated_mean * 100) if gated_mean > 0 else None)
        out["gradient"]["gated_mean"] = _f(gated_mean)
    return out


def model_compare(panel: str = "v2") -> dict:
    """Opus at medium, Opus at low, and Fable — same events, same entries.

    Scored on the six months from 2026-02, the only window where all three
    arms overlap.

    ON THE PANEL. An earlier version ran this on v1, reasoning that a model
    comparison only needs the entry to be COMMON across arms rather than to
    match the rest of the paper, and that re-entering on v2 costs half the
    paired events. That reasoning is locally true and globally wrong: it put a
    +249bp spread in Section 5.6 for a window where Section 5.2 reports a
    negative one, with nothing on the page to explain the difference, and a
    reader comparing the two is entitled to conclude the paper contradicts
    itself. v1 measures every reaction in a fixed [16:00, 16:20] slice, so it
    can only see issuers that moved inside those twenty minutes — a real
    subsample, not a neutral one. Fewer events on the same basis as everything
    else beats more events on a basis the paper elsewhere rejects.
    """
    ab_dir = CACHE / "llm_ab"
    if not ab_dir.exists():
        return {}

    def arm(fn: str) -> pd.DataFrame:
        path = ab_dir / fn
        if not path.exists():
            return pd.DataFrame()
        rows = [json.loads(x) for x in
                path.read_text(encoding="utf-8").splitlines() if x.strip()]
        return pd.DataFrame([{"symbol": r["symbol"], "date": r["date"],
                              **r["verdict"]} for r in rows])

    res = _load_results_local()
    arms = {
        "Opus 5 · medium": res[(res["effort"] == "medium") & (res["arm"] == "named")],
        "Opus 5 · low": res[(res["effort"] == "low") & (res["arm"] == "named")],
        "Fable 5 · default": pd.concat(
            [arm("claude-fable-5.jsonl"), arm("claude-fable-5_2022.jsonl")],
            ignore_index=True),
    }
    arms = {k: v[v["date"] >= SIX_MONTHS] for k, v in arms.items() if len(v)}
    if len(arms) < 2:
        return {}
    paired = None
    for v in arms.values():
        keys = set(zip(v["symbol"], v["date"]))
        paired = keys if paired is None else (paired & keys)
    if not paired or len(paired) < 30:
        return {}

    paths = pd.read_parquet(paths_file(panel))
    base = scored_events("medium", all_events=(panel == "v1"), panel=panel)[
        ["symbol", "date", "move_pct"]]
    out = {"since": SIX_MONTHS, "paired_n": len(paired), "panel": panel,
           "arms": []}
    ref = None
    for label, v in arms.items():
        v = v[[(s, d) in paired for s, d in zip(v["symbol"], v["date"])]]
        v = v.drop_duplicates(subset=["symbol", "date"])
        p = paths.drop(columns=["side", "gated"], errors="ignore").merge(
            base, on=["symbol", "date"], how="inner").merge(
            v[["symbol", "date", "direction", "confidence"]],
            on=["symbol", "date"], how="inner")
        if p.empty:
            continue
        p["side"] = np.sign(p["move_pct"]).astype(int)
        ret, _ = resolve(p, np.ones(len(p), int), None, None)
        p["ret_bp"] = ret * 1e4
        k = _gate(p)
        if k.sum() == 0 or (~k).sum() == 0:
            continue
        g, ve = p.loc[k, "ret_bp"].mean(), p.loc[~k, "ret_bp"].mean()
        t = g / (p.loc[k, "ret_bp"].std(ddof=1) / math.sqrt(int(k.sum())))
        row = {"model": label, "n": len(p), "gated_n": int(k.sum()),
               "keeps": _f(k.mean() * 100), "mech": _f(p["ret_bp"].mean()),
               "gated": _f(g), "vetoed": _f(ve), "spread": _f(g - ve),
               "t": _f(t),
               "mix": {kk: _f(vv * 100) for kk, vv
                       in p["direction"].value_counts(normalize=True).items()}}
        keyed = dict(zip(zip(p["symbol"], p["date"]), p["direction"]))
        if ref is None:
            ref = keyed
        else:
            shared = [kk for kk in keyed if kk in ref]
            row["agree_pct"] = _f(
                sum(keyed[kk] == ref[kk] for kk in shared) / max(len(shared), 1) * 100)
        out["arms"].append(row)
    return out


def _load_results_local():
    from research_llm_contamination import _load_results
    return _load_results()


STRESS_DD_PCT = 15.0     # a year the index gave back this much is a stress test


def regimes(span: list[str]) -> dict:
    """Which years in the sample were actually stressed, measured rather than
    remembered.

    The first draft asserted "2022 is the only bear market in the sample",
    which was true of a 2021-2026 panel and stops being true the moment the
    decade extension lands — 2018's fourth quarter and 2020's crash are both
    in it. Per-year max drawdown on SPY decides it, so the claim tracks the
    panel instead of the author's memory.
    """
    lo = date.fromisoformat(span[0])
    hi = date.fromisoformat(span[1])
    spy = _spy_daily(lo, hi + timedelta(days=7)).sort_values("date")
    if spy.empty:
        return {}
    spy["year"] = [d.year for d in spy["date"]]
    out = []
    for year, g in spy.groupby("year"):
        c = g["close"].to_numpy()
        dd = float((1 - c / np.maximum.accumulate(c)).max()) * 100
        out.append({"year": str(year), "max_dd_pct": _f(dd),
                    "ret_pct": _f((c[-1] / c[0] - 1) * 100),
                    "stressed": bool(dd >= STRESS_DD_PCT)})
    return {"threshold_pct": STRESS_DD_PCT, "years": out,
            "stressed": [r["year"] for r in out if r["stressed"]]}


def reaction_shape() -> dict:
    """How much of the measured move is the release, and how much preceded it.

    `anchor` is the 16:05 post-auction print, which is what the live engine
    anchors to, so `move` = anticipation + reaction. `anchor_pre` is the last
    print before the 8-K crossed, so the move measured from it is the reaction
    alone. The gap between the two medians is the part of the "reaction" that
    had already happened before the news was public — drift, leakage, or a
    peer's earlier print — and it is not something the strategy can trade.
    """
    from research_event_panel import react_path, windows

    frames = [pd.read_parquet(react_path(lo, hi))
              for lo, hi in windows("ten") if react_path(lo, hi).exists()]
    if not frames:
        return {}
    ev = pd.concat(frames, ignore_index=True)
    ev["move"] = np.abs(ev["react"] / ev["anchor"] - 1) * 100
    ev["move_pre"] = np.abs(ev["react"] / ev["anchor_pre"] - 1) * 100
    g = ev[ev["move"] >= GATE]
    return {
        "n": len(ev),
        "median_move": _f(ev["move"].median()),
        "median_move_pre": _f(ev["move_pre"].median()),
        "gated_median_move": _f(g["move"].median()),
        "gated_median_move_pre": _f(g["move_pre"].median()),
        "median_acc_min": int(ev["acc_min"].median()),
        "median_react_lag": int((ev["react_min"] - ev["acc_min"]).median()),
        "pct_after_1620": _f((ev["acc_min"] > 16 * 60 + 20).mean() * 100),
    }


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
    # How much did the sparsity cap actually bind? For most early years the
    # answer is "not at all" — after-hours tape coverage was thin in 2016-18,
    # so the qualifying set is already below the cap. Saying "sampled" where
    # nothing was dropped would overstate the sparsity; saying nothing where
    # it did bind would understate it. Report per year.
    full = load_universe_v2(sample_per_year=None)
    kept = set(zip(p["symbol"], p["date"]))
    out["sampling"] = []
    for year, g in full[full["date"] < early_cut].groupby("year"):
        n_kept = sum((s, d) in kept for s, d in zip(g["symbol"], g["date"]))
        out["sampling"].append({"year": year, "qualifying": len(g),
                                "scored": n_kept,
                                "capped": bool(len(g) > n_kept)})
    no_split = np.zeros(n, bool)      # _split_stats' train/test axis is unused
    for label, side in mutation_sides(p).items():
        # One resolve per mutation; the two periods are masks over the same
        # per-event returns, not two different resolutions of them.
        ret, _ = resolve(p.assign(side=side), np.ones(n, int), None, None)
        row = {"mutation": label}
        for tag, mask in (("holdout", pre), ("insample", ~pre)):
            s = _split_stats(ret, np.where(mask, side, 0), no_split)
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
    p = paths.drop(columns=["gated", "side"], errors="ignore").merge(
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
        # The v1 funnel, the timing-cost table and the three-panel comparison
        # were all statements ABOUT the superseded panel rather than results
        # from the current one. The selection cascade is the v2 one.
        "funnel_v2": funnel_v2(), "spec": spec(),
        "audit": audit(),
        "reaction_shape": reaction_shape(),
        "model_compare": model_compare(),
        "contamination": contamination_estimate("v2"),
        "regimes": regimes([str(min(p["edate"])), str(max(p["edate"]))]),
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

    # The same years, split by whether the pre-revision fixed window could see
    # the event at all. Without this the per-year row is an average over two
    # populations and a reader cannot tell a bad year from a differently
    # measured one.
    data["late_filers"] = late_filers(p, r1, g)
    data["liquidity_study"] = liquidity_study(p, r1, g)

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
    # On v2 like everything else. The low-effort arm was bought on 180 events
    # and 81 survive onto the acceptance-relative panel, which is a thinner
    # calibration than it was — but it is a calibration of the same quantity
    # the rest of the paper reports, which the v1 version was not.
    both = scored_events("low", all_events=False, panel="v2", arm="named")
    # NOT "effort": that key already holds the string the study ran at, and
    # overwriting it with the calibration dict silently broke the paper.
    data["effort_cal"] = {}
    for eff, frame in (("low", both),
                       ("medium", scored_events("medium", all_events=False,
                                                panel="v2"))):
        f = frame.copy()
        f["pnl"] = np.sign(f["move_pct"]) * f["fwd_bp"] - COSTS_BP
        k = _gate(f)
        data["effort_cal"][eff] = {
            "n": len(f), "gated_n": int(k.sum()),
            "gated": _f(f[k]["pnl"].mean()), "vetoed": _f(f[~k]["pnl"].mean()),
            "out_tokens": int(f["out"].mean())}
    key = ["symbol", "date"]
    pair = both[[*key, "direction"]].merge(
        scored_events("medium", all_events=False, panel="v2")[[*key, "direction"]],
        on=key, suffixes=("_low", "_med"))
    data["effort_cal"]["agreement_pct"] = _f(
        (pair["direction_low"] == pair["direction_med"]).mean() * 100)
    data["effort_cal"]["paired_n"] = len(pair)
    # The paired test, which is the only form the comparison is valid in.
    data["effort_cal"]["test"] = effort_test(p, r1 + COSTS_BP / 1e4)

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

    # Measured fill costs, and the out-of-training test. Both are optional:
    # a missing quote cache or an uncollected batch must degrade the paper by
    # one section, never fail the whole build. The reason is carried into the
    # payload so the page can say WHY a section is absent rather than
    # rendering an unexplained gap.
    for key, mod, fn in (("spreads", "research_spread_sim", "compute"),
                         ("xmodel", "research_out_of_training", "compute")):
        try:
            data[key] = getattr(__import__(mod), fn)(args)
        except SystemExit as exc:
            data[key] = {"unavailable": str(exc)}
        except Exception as exc:                      # noqa: BLE001
            data[key] = {"unavailable": f"{type(exc).__name__}: {exc}"}
        if "unavailable" in data[key]:
            print(f"  {key}: {data[key]['unavailable']}")

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
