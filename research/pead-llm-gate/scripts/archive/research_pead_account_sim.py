"""ACCOUNT-level sim of the flagship: LLM-gated PEAD reaction trades.

Composes the two validated layers: fable's cached verdicts (llm_ab
jsonl) select events; the engine's account mechanics execute them —
stop-risk sizing (RISK_PCT of current equity at the vol-scaled stop),
per-name/gross caps, whole shares, compounding. Entry at the historical
reaction print (announce-day AH, +COST_AH_BP); exits: next-day bracket
walk on daily H/L (stop-first, gap-through fills at the open) else the
T+1 15:55 close (+COST_RTH_BP). OOS months only (>= 2026-02) unless
--all. Books: llm (approved only), mech (every gated event), veto
(refused only — the counterfactual).

Run: .venv/Scripts/python.exe scripts/research_pead_account_sim.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Archived: one level below the study scripts, so reach _paths and the
# sibling research modules explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _paths import BACKEND, CACHE, NOTES
from zoneinfo import ZoneInfo

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ET = ZoneInfo("America/New_York")

GATE = 5.0
RISK_PCT = 0.5
PER_NAME_CAP = 0.20
GROSS_CAP = 1.00
SL_MULT, SL_FLOOR, SL_CAP = 2.5, 0.05, 0.12
COST_AH_BP, COST_RTH_BP = 10.0, 3.0
START_EQUITY = 100_000.0


def load() -> tuple[pd.DataFrame, dict]:
    ev = pd.read_parquet(CACHE / "pead_events_2025-02-01_2026-08-01_2873.parquet")
    ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
    ev["month"] = ev["date"].astype(str).str[:7]
    ev["date"] = pd.to_datetime(ev["date"]).dt.date
    ev = ev[np.abs(ev["move_pct"]) >= GATE]

    v = pd.DataFrame([json.loads(x) for x in
                      (CACHE / "llm_ab" / "claude-fable-5.jsonl")
                      .read_text(encoding="utf-8").splitlines()])
    v = v.drop_duplicates(subset=["symbol", "date"])
    v["direction"] = v["verdict"].apply(lambda x: x.get("direction"))
    v["date"] = pd.to_datetime(v["date"]).dt.date
    m = ev.merge(v[["symbol", "date", "direction"]], on=["symbol", "date"],
                 how="inner")

    bars = pd.read_parquet(
        [f for f in CACHE.glob("bars_ohlc_*") if "2025-01" in f.name][0])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["symbol", "date"])
    panel: dict = {}
    for sym, g in bars.groupby("symbol"):
        g = g.reset_index(drop=True)
        closes = g["c"].to_numpy()
        rets = np.abs(np.diff(closes) / closes[:-1])
        panel[sym] = {
            "dates": g["date"].to_list(), "o": g["o"].to_numpy(),
            "h": g["h"].to_numpy(), "l": g["l"].to_numpy(), "c": closes,
            "avgret": np.concatenate([[np.nan] * 6, np.array(
                [rets[i - 5:i].mean() for i in range(5, len(rets) + 1)])])
            if len(rets) >= 5 else np.full(len(closes), np.nan),
        }
    return m, panel


def simulate(events: pd.DataFrame, panel: dict, book: str,
             bracket: bool = True) -> dict:
    if book == "llm":
        sel = events[((events["direction"] == "bullish") & (events["move_pct"] > 0)) |
                     ((events["direction"] == "bearish") & (events["move_pct"] < 0))]
    elif book == "veto":
        sel = events[~(((events["direction"] == "bullish") & (events["move_pct"] > 0)) |
                       ((events["direction"] == "bearish") & (events["move_pct"] < 0)))]
    else:
        sel = events
    sel = sel.sort_values("date")

    equity = START_EQUITY
    curve, dates_axis = [], []
    open_pos: list[dict] = []
    stats = {"trades": 0, "tp": 0, "sl": 0, "deadline": 0, "wins": 0,
             "skipped_full": 0, "skipped_price": 0, "skipped_nodata": 0}
    all_days = sorted({d for p in panel.values() for d in p["dates"]})
    by_day = {d: g for d, g in sel.groupby("date")}

    for day in all_days:
        # exits first
        still = []
        for p in open_pos:
            g = panel[p["sym"]]
            i = np.searchsorted(g["dates"], day)
            if i >= len(g["dates"]) or g["dates"][i] != day:
                still.append(p)
                continue
            side, px_exit = p["side"], None
            if not bracket:
                px_exit = g["c"][i]; stats["deadline"] += 1
            elif side > 0 and g["l"][i] <= p["sl_px"]:
                px_exit = min(p["sl_px"], g["o"][i]); stats["sl"] += 1
            elif side < 0 and g["h"][i] >= p["sl_px"]:
                px_exit = max(p["sl_px"], g["o"][i]); stats["sl"] += 1
            elif side > 0 and g["h"][i] >= p["tp_px"]:
                px_exit = p["tp_px"]; stats["tp"] += 1
            elif side < 0 and g["l"][i] <= p["tp_px"]:
                px_exit = p["tp_px"]; stats["tp"] += 1
            else:
                px_exit = g["c"][i]; stats["deadline"] += 1
            pnl = p["qty"] * side * (px_exit - p["entry_px"])
            pnl -= p["qty"] * px_exit * COST_RTH_BP / 1e4
            equity += pnl
            stats["trades"] += 1
            stats["wins"] += pnl > 0
        open_pos = still

        # entries (announce-day AH, at the historical reaction print)
        gross = sum(p["qty"] * p["entry_px"] for p in open_pos)
        for _, e in by_day.get(day, pd.DataFrame()).iterrows():
            g = panel.get(e["symbol"])
            if g is None:
                stats["skipped_nodata"] += 1
                continue
            i = np.searchsorted(g["dates"], day)
            if i >= len(g["avgret"]):
                stats["skipped_nodata"] += 1
                continue
            avg = g["avgret"][i] if np.isfinite(g["avgret"][i]) else None
            sl = min(max(SL_MULT * avg, SL_FLOOR), SL_CAP) if avg else SL_FLOOR
            side = 1 if e["move_pct"] > 0 else -1
            entry_px = float(e["react"]) * (1 + side * COST_AH_BP / 1e4)
            notional = min(equity * RISK_PCT / 100 / sl, equity * PER_NAME_CAP)
            if gross + notional > equity * GROSS_CAP:
                stats["skipped_full"] += 1
                continue
            qty = int(notional // entry_px)
            if qty < 1:
                stats["skipped_price"] += 1
                continue
            open_pos.append({
                "sym": e["symbol"], "side": side, "qty": qty,
                "entry_px": entry_px,
                "sl_px": entry_px * (1 - side * sl),
                "tp_px": entry_px * (1 + side * 2 * sl),
            })
            gross += qty * entry_px
        curve.append(equity)
        dates_axis.append(day)

    arr = np.array(curve)
    peak = np.maximum.accumulate(arr)
    return {"book": book, "final": round(float(arr[-1])),
            "ret_pct": round(float(arr[-1] / START_EQUITY - 1) * 100, 1),
            "max_dd_pct": round(float(((arr - peak) / peak).min() * 100), 1),
            **stats,
            "win_pct": round(100 * stats["wins"] / max(stats["trades"], 1), 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="include tainted in-corpus months")
    args = ap.parse_args()
    m, panel = load()
    if not args.all:
        # OOS only. The panel is left whole on purpose: simulate() indexes
        # into it by event date, so extra history is unused, not lookahead.
        m = m[m["month"] >= "2026-02"]
    rows = [dict(simulate(m, panel, b), bracket="on")
            for b in ("llm", "mech", "veto")]
    rows += [dict(simulate(m, panel, b, bracket=False), bracket="off")
             for b in ("llm", "mech", "veto")]
    out = pd.DataFrame(rows)
    span = f"{min(m['date'])}..{max(m['date'])}"
    print(f"=== FLAGSHIP ACCOUNT SIM (fable-gated PEAD, {span}, "
          f"$100k, engine sizing/caps/brackets) ===")
    print(out.to_string(index=False))
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
    doc = (NOTES
           / f"pead_account_sim_{stamp}.md")
    doc.write_text(f"# Flagship account sim — {span} — {stamp}\n\n"
                   f"```\n{out.to_string(index=False)}\n```\n",
                   encoding="utf-8")
    print(f"wrote {doc}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
