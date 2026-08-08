"""What the money would have done: bracket sweep, then a real account.

TWO THINGS THAT ARE OFTEN CONFLATED AND ARE NOT THE SAME.

  mean net bp per trade  — the statistic the rest of this study reports. It is
                           the right one for asking whether a signal exists,
                           because it is per-trade and unaffected by how much
                           capital you chose to put behind it.

  CAGR and Sharpe        — what an ACCOUNT would have done. These depend on
                           position sizing, on how many trades you can hold at
                           once, and on the order they arrived in. A positive
                           mean does not survive to a positive CAGR if the
                           good trades all overlapped and you could only hold
                           four of them.

The account model below is deliberately the same shape as the PEAD study's
(`research_leadup_account_sim.py`): fixed fractional risk per position, a hard
cap on concurrent positions, and a slot allocator that walks trades in
arrival order and SKIPS anything that arrives with every slot full. That skip
is the part that matters and the part a per-trade mean cannot see — an
intraday strategy on twelve symbols fires far more often than an account can
take, and which trades you get is then a function of the clock rather than of
the signal.

THE BRACKET SWEEP. Every number elsewhere in this study uses the structural
stop and a 2R target, fixed in advance so the exhaustive run could not be
tuned into significance. That is the right discipline for the hypothesis test
and the wrong way to find out whether the bracket was simply badly chosen, so
the sweep re-runs the exit engine over a 4x4 grid of stop scales and target
multiples. It is reported as a SENSITIVITY SURFACE, not as a search for the
best cell: with 16 cells x 5 strategies the best of 80 is expected to look
good, and the honest reading is whether the surface is positive in a
CONTIGUOUS REGION or whether one cell is winning a lottery.

Run:
    python scripts/research_chart_account.py sweep
    python scripts/research_chart_account.py account
"""

from __future__ import annotations

import argparse
from datetime import date

from _paths import CACHE, NOTES

import numpy as np
import pandas as pd

from research_chart_data import COST_BP, UNIVERSE, bars_path
from research_chart_setups import (
    SETUPS,
    STRATEGIES,
    TF,
    TIME_STOP,
    prep,
    run_exit,
)

SWEEP = CACHE / "bracket_sweep.parquet"

STOP_SCALES = [0.5, 0.75, 1.0, 1.5]     # multiples of the structural stop
TARGET_RS = [1.0, 1.5, 2.0, 3.0]        # multiples of the (scaled) stop

# Account defaults. Chosen to be boring and defensible rather than optimal:
# 1% of equity risked per position is the number every retail risk-management
# video states, and six concurrent positions is what the PEAD study's flagship
# settled on.
EQUITY0 = 100_000.0
RISK_PCT = 0.01
MAX_SLOTS = 6
TRADING_DAYS = 252


# -------------------------------------------------------------------- sweep

def _rerun(setups: pd.DataFrame, stop_scale: float, target_r: float,
           frames: dict) -> np.ndarray:
    """Net bp for every setup under a different bracket.

    Re-runs the exit engine rather than rescaling the recorded outcome,
    because a wider stop does not merely scale a result — it changes which
    barrier is touched first, and on a 2R bracket that is most of the answer.
    """
    out = np.empty(len(setups))
    for k, (_, row) in enumerate(setups.iterrows()):
        df = frames[row["symbol"]]
        h, l, c = df["_h"], df["_l"], df["_c"]
        i, side, entry = int(row["i"]), int(row["side"]), float(row["entry"])
        r = abs(entry - float(row["stop"])) * stop_scale
        res, _, _ = run_exit(h, l, c, df["_day"], i, side, entry,
                             entry - side * r, entry + side * target_r * r,
                             TIME_STOP)
        out[k] = (res / entry) * 1e4 - COST_BP
    return out


def _frames() -> dict:
    out = {}
    for s in UNIVERSE:
        d = prep(pd.read_parquet(bars_path(s)), TF)
        out[s] = {"_h": d["h"].to_numpy(), "_l": d["l"].to_numpy(),
                  "_c": d["c"].to_numpy(), "_day": d["day"].to_numpy()}
    return out


def stage_sweep(args) -> None:
    s = pd.read_parquet(SETUPS)
    frames = _frames()
    rows = []
    for ss in STOP_SCALES:
        for tr in TARGET_RS:
            net = _rerun(s, ss, tr, frames)
            d = s.assign(net=net)
            for name in [*STRATEGIES, "ALL"]:
                g = d if name == "ALL" else d[d["strategy"] == name]
                v = g["net"].to_numpy()
                rows.append({
                    "strategy": name, "stop_scale": ss, "target_r": tr,
                    "n": len(v), "mean_bp": v.mean(),
                    "win_pct": (v > 0).mean() * 100,
                    "t": v.mean() / (v.std(ddof=1) / np.sqrt(len(v))),
                })
            print(f"  stop x{ss} target {tr}R: all={net.mean():+.2f}bp",
                  flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(SWEEP)

    lines = [f"# Bracket sweep — {date.today()}", "",
             f"{len(s):,} setups, {COST_BP:.0f}bp round trip. Every cell "
             f"re-runs the exit engine; nothing is rescaled.", "",
             "Mean net bp per trade. Rows are the stop as a multiple of the "
             "setup's structural stop, columns the target in units of that "
             "stop.", ""]
    for name in [*STRATEGIES, "ALL"]:
        g = out[out["strategy"] == name]
        if g.empty:
            continue
        lines += [f"### {name}", "",
                  "| stop | " + " | ".join(f"{t}R" for t in TARGET_RS) + " |",
                  "|---" * (len(TARGET_RS) + 1) + "|"]
        for ss in STOP_SCALES:
            cells = []
            for tr in TARGET_RS:
                r = g[(g["stop_scale"] == ss) & (g["target_r"] == tr)]
                cells.append(f"{r['mean_bp'].iat[0]:+.2f} "
                             f"({r['t'].iat[0]:+.1f})" if len(r) else "—")
            lines.append(f"| x{ss} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["Cells are `mean bp (t-stat)`. Read the SURFACE, not the best "
              "cell: 16 cells x 5 strategies means the maximum of 80 draws is "
              "expected to look good on noise alone. A real effect shows up "
              "as a contiguous positive region, not as one bright square.", ""]
    NOTES.mkdir(parents=True, exist_ok=True)
    p = NOTES / f"bracket_sweep_{date.today():%Y%m%d}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {p}")


# ------------------------------------------------------------------ account

def simulate(trades: pd.DataFrame, equity0: float = EQUITY0,
             risk_pct: float = RISK_PCT, slots: int = MAX_SLOTS) -> dict:
    """Sequential capital through a slot-limited account.

    Trades arrive in timestamp order. A trade is TAKEN only if a slot is free
    at its entry; otherwise it is skipped and recorded as such. Size is set so
    that a stop-out loses `risk_pct` of CURRENT equity, which is what makes
    the curve compound rather than merely accumulate.

    The exit time is the bar the exit engine actually reached, so a trade that
    ran to the session close holds its slot all afternoon — which is the
    constraint that makes concurrency bind.
    """
    t = trades.sort_values("ts").reset_index(drop=True)
    equity = equity0
    free_at = np.zeros(slots)                # slot -> busy until (unix seconds)
    curve, taken, skipped = [], [], 0
    for _, row in t.iterrows():
        ts = row["ts"].timestamp()
        exit_ts = row["exit_ts"].timestamp()
        idx = np.argmin(free_at)
        if free_at[idx] > ts:
            skipped += 1
            continue
        free_at[idx] = exit_ts
        # Risk the same fraction of equity regardless of how wide the stop is;
        # the stop distance sets the SIZE, not the risk.
        stop_bp = max(float(row["r_bp"]), 1e-9)
        notional = equity * risk_pct / (stop_bp / 1e4)
        # A 5bp stop would imply 200x leverage. Real accounts cannot do that,
        # and letting the sim do it would manufacture a curve out of the
        # tightest-stop trades. Cap at 4x equity per position (Reg-T daytrade).
        notional = min(notional, equity * 4)
        pnl = notional * float(row["net_bp"]) / 1e4
        equity += pnl
        curve.append({"ts": row["exit_ts"], "equity": equity,
                      "pnl": pnl, "symbol": row["symbol"],
                      "strategy": row["strategy"]})
        taken.append(row.name)
    if not curve:
        return {"n": 0}
    c = pd.DataFrame(curve).sort_values("ts")
    # FORWARD-FILL, don't drop. A day with no exits is a day the account held
    # flat and returned zero, and dropping it removes a zero from the return
    # series — which raises the mean and lowers the count, flattering Sharpe
    # in both directions at once. Weekends are dropped because there is no
    # tape and no risk, so they are not observations.
    daily = c.set_index("ts")["equity"].resample("1D").last().ffill().dropna()
    daily = daily[daily.index.weekday < 5]
    if len(daily) < 5:
        return {"n": len(c)}
    rets = daily.pct_change().dropna()
    peak = daily.cummax()
    span_days = (daily.index[-1] - daily.index[0]).days
    out = {
        "n": len(c), "skipped": skipped, "final": float(daily.iat[-1]),
        "total_ret": float(daily.iat[-1] / equity0 - 1),
        "sharpe": float(rets.mean() / rets.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if rets.std(ddof=1) > 0 else 0.0,
        "maxdd": float(((daily - peak) / peak).min()),
        "win_pct": float((c["pnl"] > 0).mean() * 100),
        "span_days": span_days, "curve": daily,
    }
    # CAGR is an ANNUALISED rate and annualising a three-day sample produces
    # numbers like +349,190%, which is what the first run of this reported off
    # a partially-filled verdict panel. Below a quarter of data it is not a
    # rate, it is a division artefact; report the total return instead.
    out["cagr"] = (float((daily.iat[-1] / equity0)
                         ** (365.25 / span_days) - 1)
                   if span_days >= 90 else float("nan"))
    return out


def _cagr(r: dict) -> str:
    """CAGR, or the total return when the sample is too short to annualise."""
    if r.get("cagr") is None or np.isnan(r.get("cagr", np.nan)):
        return f"{r['total_ret'] * 100:+.1f}% (raw, {r['span_days']}d)"
    return f"{r['cagr'] * 100:+.1f}%"


def _with_exits(t: pd.DataFrame) -> pd.DataFrame:
    """Attach each trade's wall-clock exit time, which the slot allocator
    needs and the setups table stores only as a bar index."""
    out = []
    for sym, g in t.groupby("symbol"):
        df = prep(pd.read_parquet(bars_path(sym)), TF)
        ts = df["ts"].to_numpy()
        g = g.copy()
        g["exit_ts"] = pd.to_datetime(ts[g["exit_i"].to_numpy()], utc=True)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def stage_account(args) -> None:
    from research_chart_score import gate_panel

    s = pd.read_parquet(SETUPS)
    s = _with_exits(s)
    s["ts"] = pd.to_datetime(s["ts"], utc=True)

    lines = [f"# Account simulation — {date.today()}", "",
             f"${EQUITY0:,.0f} start, {RISK_PCT * 100:.0f}% of equity risked "
             f"per position, {MAX_SLOTS} concurrent positions max, "
             f"{COST_BP:.0f}bp round trip, position notional capped at 4x "
             f"equity. Trades that arrive with every slot full are SKIPPED, "
             f"not queued.", ""]

    lines += ["## Mechanical, per strategy (all setups, no LLM)", "",
              "| strategy | trades taken | skipped | final | CAGR | Sharpe | "
              "max DD | win% |", "|---|---|---|---|---|---|---|---|"]
    for name in [*STRATEGIES, "ALL"]:
        g = s if name == "ALL" else s[s["strategy"] == name]
        r = simulate(g, slots=args.slots)
        if not r.get("n"):
            continue
        lines.append(
            f"| {name} | {r['n']:,} | {r['skipped']:,} | "
            f"${r['final']:,.0f} | {_cagr(r)} | "
            f"{r['sharpe']:+.2f} | {r['maxdd'] * 100:.1f}% | "
            f"{r['win_pct']:.1f}% |")
    lines.append("")

    # ------------------------------------------------------- gated vs not
    for model in args.models.split(","):
        g = gate_panel("gate", model)
        if g.empty:
            continue
        keep_ids = set(g.loc[g["keep"], "setup_id"])
        panel_ids = set(g["setup_id"])
        sub = s[s["setup_id"].isin(panel_ids)]
        lines += [f"## `{model}` gate, on the {len(panel_ids):,}-setup panel",
                  "",
                  "Both rows are the same account model on the same setups; "
                  "the only difference is whether the model's verdict was "
                  "allowed to veto. The gated row also holds FEWER "
                  "positions, which frees slots — so part of any difference "
                  "is capacity, not skill, and the per-trade spread in "
                  "`research_chart_score.py` is the cleaner statistic.",
                  "",
                  "| arm | trades taken | skipped | final | CAGR | Sharpe | "
                  "max DD |", "|---|---|---|---|---|---|---|"]
        for label, tr in (("ungated", sub),
                          ("gated", sub[sub["setup_id"].isin(keep_ids)])):
            r = simulate(tr, slots=args.slots)
            if not r.get("n"):
                continue
            lines.append(
                f"| {label} | {r['n']:,} | {r['skipped']:,} | "
                f"${r['final']:,.0f} | {_cagr(r)} | "
                f"{r['sharpe']:+.2f} | {r['maxdd'] * 100:.1f}% |")
        lines.append("")

    # ------------------------------------------------- slot sensitivity
    lines += ["## Slot sensitivity (ALL strategies, mechanical)", "",
              "| slots | trades taken | skipped | CAGR | Sharpe | max DD |",
              "|---|---|---|---|---|---|"]
    for k in (2, 4, 6, 10, 20):
        r = simulate(s, slots=k)
        if not r.get("n"):
            continue
        lines.append(f"| {k} | {r['n']:,} | {r['skipped']:,} | "
                     f"{_cagr(r)} | {r['sharpe']:+.2f} | "
                     f"{r['maxdd'] * 100:.1f}% |")
    lines += ["", "More slots means more of the signal is actually traded and "
              "less idiosyncratic luck about which trades the clock let you "
              "take. It also means more concurrent risk, which the fixed "
              "per-position sizing does not charge for.", ""]

    NOTES.mkdir(parents=True, exist_ok=True)
    p = NOTES / f"account_{date.today():%Y%m%d}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["sweep", "account"])
    ap.add_argument("--slots", type=int, default=MAX_SLOTS)
    ap.add_argument("--models", default="phi4,qwen3")
    args = ap.parse_args()
    {"sweep": stage_sweep, "account": stage_account}[args.stage](args)


if __name__ == "__main__":
    main()
