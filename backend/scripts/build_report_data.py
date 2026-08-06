"""Assemble every number the research report cites into one JSON.

The report is generated from this file, never from hand-transcribed figures
— a paper whose numbers drift from the harness that produced them is worse
than no paper. Run after `score` / `curve` / `curve --brackets` / `brackets`.

Run: .venv/Scripts/python.exe scripts/build_report_data.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_llm_contamination import (  # noqa: E402
    CACHE,
    COSTS_BP,
    GATE,
    MIN_DV,
    TOP_PER_DAY,
    _bars_for,
    _gate,
    _permutation_p,
    _spread,
    load_universe,
)

PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
OUT = Path(__file__).resolve().parents[2] / "docs" / "report_data.json"


def verdicts() -> pd.DataFrame:
    rows = []
    path = CACHE / "llm_contam" / "results_named.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        _, effort, symbol, ymd = r["custom_id"].split("_")
        rows.append({"effort": effort, "symbol": symbol,
                     "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                     **r["verdict"], "in": r.get("in"), "out": r.get("out")})
    return pd.DataFrame(rows)


def funnel() -> list[dict]:
    """The selection cascade, from every AMC earnings release with an
    after-hours tape down to what this study scores."""
    frames = []
    for path in sorted(CACHE.glob("pead_events_*.parquet")):
        ev = pd.read_parquet(path)
        ev["date"] = pd.to_datetime(ev["date"]).dt.date
        ev["move"] = (ev["react"] / ev["anchor"] - 1) * 100
        lo, hi = str(ev["date"].min()), str(ev["date"].max())
        bars = _bars_for(lo, hi)
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
        ("AMC earnings releases with an after-hours tape", allev),
        (f"|reaction| ≥ {GATE:.0f}% (the strategy's gate)", g5),
        (f"prior-session dollar volume ≥ ${MIN_DV/1e6:.0f}M", liq),
        ("top 8 per day — the live watchlist size", top8),
        (f"top {TOP_PER_DAY} per day — scored here", top5),
    ]
    return [{"stage": label, "total": len(df), "per_year": round(len(df) / years)}
            for label, df in stages]


def main() -> None:
    universe = load_universe()
    universe["date"] = universe["date"].astype(str)
    res = verdicts()
    data: dict = {"universe_n": int(len(universe)),
                  "span": [universe["date"].min(), universe["date"].max()],
                  "gate_pct": GATE, "top_per_day": TOP_PER_DAY,
                  "min_dv_musd": MIN_DV / 1e6, "costs_bp": COSTS_BP,
                  "funnel": funnel()}

    # --- effort calibration (paired, same events, both efforts) -----------
    data["effort"] = {}
    for eff in ("low", "medium"):
        m = res[res["effort"] == eff].merge(universe, on=["symbol", "date"])
        m["pnl"] = np.sign(m["move_pct"]) * m["fwd_bp"] - COSTS_BP
        k = _gate(m)
        data["effort"][eff] = {
            "n": len(m), "mech": round(m["pnl"].mean(), 1),
            "gated_n": int(k.sum()), "gated": round(m[k]["pnl"].mean(), 1),
            "vetoed": round(m[~k]["pnl"].mean(), 1),
            "spread": round(_spread(m.rename(columns={"pnl": "mech_pnl"})), 1),
            "out_tokens": int(m["out"].mean()),
        }
    paired = res.pivot_table(index=["symbol", "date"], columns="effort",
                             values="direction", aggfunc="first").dropna()
    data["effort"]["agreement_pct"] = round(
        float((paired["low"] == paired["medium"]).mean()) * 100, 1)

    # --- headline (medium, the shipped effort) ---------------------------
    m = res[res["effort"] == "medium"].merge(universe, on=["symbol", "date"])
    m["mech_pnl"] = np.sign(m["move_pct"]) * m["fwd_bp"] - COSTS_BP
    k = _gate(m)
    g = m[k]
    data["headline"] = {
        "n": len(m), "gated_n": int(k.sum()), "vetoed_n": int((~k).sum()),
        "keep_pct": round(float(k.mean()) * 100),
        "mech": round(m["mech_pnl"].mean(), 1),
        "gated": round(g["mech_pnl"].mean(), 1),
        "vetoed": round(m[~k]["mech_pnl"].mean(), 1),
        "spread": round(_spread(m), 1),
        "t": round(g["mech_pnl"].mean()
                   / (g["mech_pnl"].std(ddof=1) / math.sqrt(len(g))), 2),
        "perm_p": _permutation_p(m, np.random.default_rng(20260806)),
        "win_gated": round(float((g["mech_pnl"] > 0).mean()) * 100, 1),
        "win_all": round(float((m["mech_pnl"] > 0).mean()) * 100, 1),
        "long_n": int((np.sign(g["move_pct"]) > 0).sum()),
        "long": round(g[np.sign(g["move_pct"]) > 0]["mech_pnl"].mean(), 1),
        "short_n": int((np.sign(g["move_pct"]) < 0).sum()),
        "short": round(g[np.sign(g["move_pct"]) < 0]["mech_pnl"].mean(), 1),
        "mix": {kk: round(v * 100)
                for kk, v in m["direction"].value_counts(normalize=True).items()},
    }

    data["years"] = []
    for year, yg in m.groupby("year"):
        kk = _gate(yg)
        data["years"].append({
            "year": year, "n": len(yg), "mech": round(yg["mech_pnl"].mean(), 1),
            "gated": round(yg[kk]["mech_pnl"].mean(), 1),
            "vetoed": round(yg[~kk]["mech_pnl"].mean(), 1),
            "spread": round(_spread(yg), 1)})

    m["dvM"] = m["dv"] / 1e6
    m["q"] = pd.qcut(m["dvM"], 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
    data["liquidity"] = []
    for q, qg in m.groupby("q", observed=True):
        kk = _gate(qg)
        data["liquidity"].append({
            "q": str(q), "lo": round(qg["dvM"].min()), "hi": round(qg["dvM"].max()),
            "n": len(qg), "mech": round(qg["mech_pnl"].mean(), 1),
            "gated": round(qg[kk]["mech_pnl"].mean(), 1),
            "spread": round(_spread(qg), 1)})

    m["rank"] = m.groupby("date")["dv"].rank(ascending=False, method="first")
    data["ranks"] = []
    for r in range(1, TOP_PER_DAY + 1):
        rg = m[m["rank"] == r]
        if len(rg) < 40:
            continue
        kk = _gate(rg)
        data["ranks"].append({
            "rank": r, "n": len(rg), "mech": round(rg["mech_pnl"].mean(), 1),
            "gated": round(rg[kk]["mech_pnl"].mean(), 1),
            "spread": round(_spread(rg), 1)})

    x = (pd.Timestamp("2026-08-06") - pd.to_datetime(g["date"])).dt.days.to_numpy(float)
    y = g["mech_pnl"].to_numpy(float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    se = float(np.sqrt((resid @ resid) / (len(x) - 2)
                       / ((x - x.mean()) @ (x - x.mean()))))
    data["gradient"] = {"bp_per_year": round(slope * 365, 1),
                        "se": round(se * 365, 1), "t": round(slope / se, 2)}

    # --- brackets --------------------------------------------------------
    br = pd.read_parquet(CACHE / "brackets_gated.parquet")
    br["ret_bp"] = (br["side"] * (br["exit_px"] / br["entry"] - 1)) * 1e4 - COSTS_BP
    data["brackets"] = {
        "n": len(br), "mean_bp": round(br["ret_bp"].mean(), 1),
        "win_pct": round(float((br["ret_bp"] > 0).mean()) * 100, 1),
        "by_reason": [
            {"reason": r, "n": len(gg), "pct": round(len(gg) / len(br) * 100, 1),
             "mean_bp": round(gg["ret_bp"].mean(), 1)}
            for r, gg in br.groupby("reason")],
        "median_sl_pct": round(float(br["sl_pct"].median()) * 100, 1),
    }

    for tag, name in (("raw", "study-curve.json"),
                      ("bracketed", "study-curve-bracketed.json")):
        path = PUBLIC / name
        if path.exists():
            data[f"curve_{tag}"] = json.loads(path.read_text(encoding="utf-8"))

    usage = CACHE / "llm_contam" / "usage.json"
    data["spend_usd"] = round(json.loads(usage.read_text())["usd"], 2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"universe {data['universe_n']} · headline gated "
          f"{data['headline']['gated']}bp · bracketed "
          f"{data['brackets']['mean_bp']}bp · spend ${data['spend_usd']}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
