"""Slot-level account sim for the confirmed-delayed-entry mechanical PEAD —
the missing piece wickout §5 named before any instance exists. No new
parameters: everything (features, +60m entry, walk-forward fit, thresholds)
is research_wickout.stage_entry's machinery reused verbatim; this adds the
6-slot equal-weight account view and daily alpha/beta, OOS 2019-26 only.

Two arms reported:
  ungated +60m   truly mechanical (no model in the loop) — the floor.
  P>=0.45 gate   the §3d candidate; carries best-of-family risk by the
                 wick study's own limitations note.

Run: python scripts/research_delayed_account.py
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _paths import CACHE, NOTES, SCRIPTS

sys.path.insert(0, str(SCRIPTS))

from research_common import stamp, write_note  # noqa: E402
from research_wickout import (  # noqa: E402
    COSTS_BP,
    build_paths,
    features_at,
    load_minutes,
    panel,
    r_hold,
    run_rule,
    tstat,
)

STAMP = stamp()
SLOTS = 6


def main() -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier

    p = panel()
    q = load_minutes(p)
    side = q["side"].to_numpy()
    paths = build_paths(q, side)
    em = q["em"].to_numpy()
    move = q["move_pct"].to_numpy()
    run5 = q["run5d"].to_numpy()
    dv = q["dv"].to_numpy()
    yr = q["year"].to_numpy().astype(int)
    dates = q["date"].astype(str).to_numpy()
    base, _ = run_rule(paths, r_hold())
    y = (base > 0).astype(int)

    feats, keep, px60 = [], [], []
    for i, pp in enumerate(paths):
        if pp is None or not np.isfinite(base[i]):
            continue
        f = features_at(pp, 60, False, em[i], move[i], run5[i], dv[i])
        if f is None:
            continue
        feats.append(f)
        keep.append(i)
        px60.append(pp.rc[min(pp.i_entry + 60, pp.n - 1)])
    X = np.nan_to_num(pd.DataFrame(feats).to_numpy(), nan=0.0)
    keep = np.array(keep)
    px60 = np.array(px60)
    yy, yrs = y[keep], yr[keep]
    prob = np.full(len(keep), np.nan)
    for t in range(2019, 2027):
        tr, te = yrs < t, yrs == t
        if te.sum() == 0 or tr.sum() < 200:
            continue
        gb = HistGradientBoostingClassifier(
            max_depth=3, max_iter=150, learning_rate=0.08,
            min_samples_leaf=40, random_state=7).fit(X[tr], yy[tr])
        prob[te] = gb.predict_proba(X[te])[:, 1]
    oos = np.isfinite(prob)

    t1 = np.array([paths[k].rc[paths[k].i_t1close] for k in keep])
    r60 = (t1 - px60) / 100 - COSTS_BP / 1e4
    dv_k = dv[keep]
    d_k = dates[keep]

    spy = pd.read_parquet(CACHE / "bench_SPY_2016-01-13_2026-08-06.parquet")
    spy["date"] = spy["date"].astype(str)
    spy = spy.sort_values("date")
    sessions = list(spy["date"])
    spy_ret = spy.set_index("date")["close"].pct_change()
    s_arr = np.array(sessions)

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Delayed-entry mech PEAD, 6-slot account (OOS 2019-26) — {STAMP}")
    emit()
    emit(f"{int(oos.sum())} OOS events; entry accept+60m, exit T+1 15:55, "
         f"tape sides, net {COSTS_BP:.0f}bp; {SLOTS} equal-weight slots, "
         "slot contention by dollar volume. P&L lands on the exit session; "
         "flat days included. The 23.2bp column restates the measured AH "
         "round trip (entry an hour after the release is still AH).")
    emit()
    emit("| arm | trades | net bp/tr | t | win% | net@23.2 | ann ret % "
         "| Sharpe | maxDD % | alpha %/yr | beta |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")

    daily_out = {}
    full = np.isfinite(r60)   # every kept event, 2016-26 — no model, no OOS cut
    for name, m in (("ungated FULL 2016-26 (no model)", full),
                    ("ungated +60m", oos),
                    ("P>=0.45 gate", oos & (prob >= 0.45))):
        sel = pd.DataFrame({"date": d_k[m], "ret": r60[m], "dv": dv_k[m]})
        sel = sel[np.isfinite(sel["ret"])]
        sel["t1"] = [s_arr[min(np.searchsorted(s_arr, d, "right"), len(s_arr) - 1)]
                     for d in sel["date"]]
        sel = sel.sort_values(["t1", "dv"], ascending=[True, False])
        daily = np.zeros(len(sessions))
        taken_rows = []
        for d, g in sel.groupby("t1"):
            i = sessions.index(d) if d in spy_ret.index else None
            if i is None:
                continue
            g = g.head(SLOTS)
            taken_rows.append(g)
            daily[i] += g["ret"].sum() / SLOTS
        taken = pd.concat(taken_rows)
        # OOS arms trim to 2019+ (their first possible OOS year); the full
        # arm keeps the whole decade.
        first = "2016-01-01" if name.startswith("ungated FULL") else "2019-01-01"
        start = next(i for i, d in enumerate(sessions) if d >= first)
        dser = pd.Series(daily[start:], index=sessions[start:], name=name)
        eq = np.cumprod(1 + dser.to_numpy())
        years = len(dser) / 252
        ann = float(eq[-1] ** (1 / years) - 1) * 100
        sharpe = float(dser.mean() / dser.std(ddof=1) * np.sqrt(252))
        mdd = float((1 - eq / np.maximum.accumulate(eq)).max() * 100)
        j = pd.concat([dser, spy_ret], axis=1, join="inner").dropna()
        yv, xv = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
        beta = float(np.cov(yv, xv, ddof=1)[0, 1] / np.var(xv, ddof=1))
        alpha = float((yv.mean() - beta * xv.mean()) * 252 * 100)
        r = taken["ret"].to_numpy()
        emit(f"| {name} | {len(r)} | {r.mean() * 1e4:+.1f} | {tstat(r):+.2f} "
             f"| {(r > 0).mean() * 100:.0f} | {(r.mean() - 10.2 / 1e4) * 1e4:+.1f} "
             f"| {ann:+.2f} | {sharpe:+.2f} | {mdd:.1f} | {alpha:+.2f} "
             f"| {beta:+.3f} |")
        daily_out[name] = dser
        if name == "ungated +60m":
            pd.DataFrame({"d": dser.index, "ret": dser.to_numpy()}).to_parquet(
                CACHE / "account_daily_delayed_ungated.parquet")
        elif name == "P>=0.45 gate":
            pd.DataFrame({"d": dser.index, "ret": dser.to_numpy()}).to_parquet(
                CACHE / "account_daily_delayed_gated.parquet")

    emit()
    emit("## By year, net bp/trade (ungated | gated)")
    emit()
    emit("| year | ungated n | bp | t | gated n | bp | t |")
    emit("|---|---|---|---|---|---|---|")
    for yv2 in range(2019, 2027):
        m_y = oos & (yrs == yv2)
        g_y = m_y & (prob >= 0.45)
        a = r60[m_y]
        b = r60[g_y]
        a, b = a[np.isfinite(a)], b[np.isfinite(b)]
        emit(f"| {yv2} | {len(a)} | {a.mean() * 1e4:+.1f} | {tstat(a):+.2f} "
             f"| {len(b)} | {b.mean() * 1e4 if len(b) else 0:+.1f} "
             f"| {tstat(b):+.2f} |")
    emit()
    emit("The gate's threshold carries best-of-family risk (wick study's own "
         "limitations note); the ungated row is the selection-free floor.")

    write_note(NOTES / f"delayed_account_{STAMP}.md", lines)


if __name__ == "__main__":
    main()
