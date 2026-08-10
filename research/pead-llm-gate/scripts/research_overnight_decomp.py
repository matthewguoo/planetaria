"""Where in the clock does the post-earnings drift actually accrue?

Lou-Polk-Skouras (JFE 2019) report that momentum and SUE profits accrue
essentially entirely OVERNIGHT; Bogousslavsky (JFE 2021) puts part of the
overnight return in the open print itself. If that anatomy holds for our
panel, the flagship is holding through six and a half intraday hours per
session that contribute nothing — or negative — and an exit at the OPEN
(a MOO order, which Alpaca supports) buys the same drift in less time with
less variance. The engine can trade an open auction; nothing about the
after-hours entry changes.

Two stages, both entirely offline — every input is already in cache/:

  mech      the season replay the overnight-alpha note queued as probe #1:
            every AMC reporter in events_v2 (11,662 events, 2016-2026),
            signed by the direction of its own after-hours reaction, no LLM
            anywhere. Legs: react -> next open (the carry the engine would
            hold), open -> close (the leg Berkman says retail overpays at),
            and the following nights/days out to T+3. Filters mirror the
            probe: prior-session dollar volume >= $50M, price >= $5, with
            the |reaction| gate swept at 2/5/10%.

  flagship  the paper's own panel (scored events x cached verdicts), FULL
            POLICY sides, conditional T+1/T+3 horizon — re-scored under
            exit-at-open variants and a nights-only ladder, through the
            same 6-slot account model the paper reports. The question: does
            swapping the 15:55 close exit for the next OPEN raise Sharpe?

PRICE BASES — the two traps this file walked into and out of.

1. The daily panels are Adjustment.ALL; the entry prints are raw. Mixing
   levels across the two bases fabricates the split factor as a return.
2. `anchor` is NOT the event-day close: it is the last print at or before
   16:05 ET, and when the press release crosses the wire before the 8-K is
   accepted (common for acceptances 16:05-16:45), that print is already
   post-reaction tape. ROKU 2022-11-02: official close 54.32, anchor 42.00
   — treating CT/anchor as an adjustment factor imports the crash into the
   entry price, and the error correlates with exactly the events the gate
   trades. Anchor levels are therefore never used here.

So: adjusted prices are only ever used as SAME-SESSION ratios (O_k/C_k,
which no adjustment can touch), chained onto raw session closes from the
paths cache. Close-exit policies reproduce the paper's numbers identically
by construction; open exits differ from them only by the session's own
open/close ratio. The mech stage, which has no raw closes, divides the
(adjusted) close-to-open gap by the (basis-free) after-hours move instead —
and restricts itself to acceptances at or after 16:30, because before that
the measured move itself can be tape-vs-tape rather than reaction-vs-close.
Ex-dividend drops land in the night legs unhedged, exactly as they land in
the paper's own close-to-close holds.

Costs: COSTS_BP (13bp: 10 AH entry + 3 RTH exit) for any single round trip,
open exits included — a MOO print is charged like the close print. The
nights-only ladder pays LADDER_BP more per additional night (close entry +
open exit, 3bp each side). A 23.2bp sensitivity row uses the measured
round trip from the paper's Section 6.

Run:
    python scripts/research_overnight_decomp.py mech
    python scripts/research_overnight_decomp.py flagship
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from _paths import BACKEND, CACHE, NOTES, SCRIPTS

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_llm_contamination import (  # noqa: E402
    COSTS_BP,
    ET,
    MIN_DV,
)

LADDER_BP = 6.0        # each extra night in the ladder: close entry + open exit
MEASURED_BP = 23.2     # Section 6's measured round trip, the sensitivity case
MIN_PRICE = 5.0
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")


# ------------------------------------------------------------------- panel

def load_ohlc() -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Every daily OHLC panel stitched into one per-symbol lookup:
    symbol -> (dates[str], open, close, dollar_volume). Overlapping windows
    carry duplicate (symbol, date) rows; the values agree, keep the first."""
    frames = [pd.read_parquet(p) for p in sorted(CACHE.glob("bars_ohlc_*.parquet"))]
    bars = pd.concat(frames, ignore_index=True)
    # Some panels store `date` as datetime.date, others as str — one basis.
    bars["date"] = bars["date"].astype(str)
    bars = (bars.drop_duplicates(subset=["symbol", "date"])
            .sort_values(["symbol", "date"]))
    out = {}
    for s, g in bars.groupby("symbol"):
        out[s] = (g["date"].to_numpy(),
                  g["o"].to_numpy(float),
                  g["c"].to_numpy(float),
                  (g["c"] * g["v"]).to_numpy(float))
    return out


def attach_legs(df: pd.DataFrame, panel: dict, k_sessions: int = 3,
                price_col: str = "react") -> pd.DataFrame:
    """For each event row, the opens and closes of the next k sessions plus
    the prior session's dollar volume. Events whose symbol or dates are not
    in the panel come back NaN and are counted by the caller."""
    n = len(df)
    cols = {}
    for k in range(1, k_sessions + 1):
        cols[f"O{k}"] = np.full(n, np.nan)
        cols[f"C{k}"] = np.full(n, np.nan)
    dv_prior = np.full(n, np.nan)
    ct = np.full(n, np.nan)        # the panel's own close ON the event day
    sym = df["symbol"].to_numpy()
    dat = df["date"].astype(str).to_numpy()
    for i in range(n):
        got = panel.get(sym[i])
        if got is None:
            continue
        dates, o, c, dv = got
        j = np.searchsorted(dates, dat[i], side="right")  # first session AFTER
        on_day = j - 1 >= 0 and dates[j - 1] == dat[i]
        if on_day:
            ct[i] = c[j - 1]
            if j - 2 >= 0:
                dv_prior[i] = dv[j - 2]
        elif j - 1 >= 0:
            dv_prior[i] = dv[j - 1]
        for k in range(1, k_sessions + 1):
            idx = j + k - 1
            if idx < len(dates):
                cols[f"O{k}"][i] = o[idx]
                cols[f"C{k}"][i] = c[idx]
    out = df.copy()
    for name, arr in cols.items():
        out[name] = arr
    out["dv_prior"] = dv_prior
    out["CT"] = ct
    return out


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


# -------------------------------------------------------------------- mech

def stage_mech(args) -> None:
    """The mechanical season replay: every AMC reporter, signed by its own
    after-hours reaction, gross of costs (the probe's convention — costs are
    a constant subtraction the reader can apply per round trip)."""
    panel = load_ohlc()
    evs = []
    for p in sorted(CACHE.glob("events_v2_*.parquet")):
        evs.append(pd.read_parquet(p))
    ev = pd.concat(evs, ignore_index=True).drop_duplicates(subset=["symbol", "date"])
    ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
    ev = attach_legs(ev, panel)
    total = len(ev)
    # Acceptances before 16:30 ET can have a post-reaction print AS the
    # anchor (the PR beats the 8-K to the tape), which mismeasures the move
    # and its sign. Keep the events where the anchor is trustworthy.
    late_enough = ev["acc_min"] >= 16 * 60 + 30
    n_early = int((~late_enough).sum())
    ev = ev[late_enough
            & (ev["dv_prior"] >= MIN_DV) & (ev["anchor"] >= MIN_PRICE)
            & np.isfinite(ev["O1"]) & np.isfinite(ev["C1"])
            & np.isfinite(ev["CT"])]
    ev["year"] = ev["date"].str[:4]
    # All adjusted prices enter as ratios only. night0 divides the adjusted
    # close->open gap by the basis-free AH move: O1raw/react_raw =
    # (O1/CT)_adj / (1 + move), up to the ~bp-scale drift between the 16:04
    # anchor print and the official close.
    s = np.sign(ev["move_pct"].to_numpy())
    onemove = 1 + ev["move_pct"].to_numpy() / 100
    gap = (ev["O1"] / ev["CT"]).to_numpy()             # adjusted, basis-free
    daia = (ev["C1"] / ev["O1"]).to_numpy()            # adjusted, basis-free
    ev["night0"] = s * (gap / onemove - 1) * 1e4       # react -> next open
    ev["day1"] = s * (daia - 1) * 1e4                  # open -> close
    ev["t1cc"] = s * (gap * daia / onemove - 1) * 1e4  # the paper's leg

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Overnight decomposition, mechanical — {STAMP}")
    emit()
    emit(f"{total} AMC events in events_v2; {n_early} dropped for acceptance "
         f"before 16:30 ET (anchor can be post-reaction tape there), and "
         f"{len(ev)} survive that plus the ${MIN_DV / 1e6:.0f}M prior-session "
         f"dollar-volume floor, the ${MIN_PRICE:.0f} price floor and "
         "event-day + next-session OHLC rows. Legs signed by the sign of "
         "each event's own after-hours reaction; GROSS of costs (~13bp "
         "friendly, 23.2bp measured per round trip). Adjusted daily prices "
         "enter as same-session and adjacent-session ratios only; the entry "
         "leg divides the close->open gap by the basis-free AH move.")
    emit()
    emit("night0 = reaction print -> next open. day1 = next open -> next "
         "close. t1cc = reaction -> next close (the paper's mechanical leg; "
         "night0 and day1 compound to it).")
    emit()
    for gate in (2.0, 5.0, 10.0):
        g = ev[np.abs(ev["move_pct"]) >= gate]
        emit(f"## |reaction| >= {gate:.0f}%  (n={len(g)})")
        emit()
        emit("| slice | n | night0 med | night0 mean | t | cont% | day1 mean | t | t1cc mean |")
        emit("|---|---|---|---|---|---|---|---|---|")

        def row(label, m):
            night, day, cc = m["night0"].to_numpy(), m["day1"].to_numpy(), m["t1cc"].to_numpy()
            if not len(night):
                return
            emit(f"| {label} | {len(m)} | {np.median(night):+.0f} "
                 f"| {night.mean():+.0f} | {tstat(night):+.2f} "
                 f"| {(night > 0).mean() * 100:.0f} "
                 f"| {day.mean():+.0f} | {tstat(day):+.2f} "
                 f"| {cc.mean():+.0f} |")

        row("ALL", g)
        row("AH up", g[g["move_pct"] > 0])
        row("AH down", g[g["move_pct"] < 0])
        for year in sorted(g["year"].unique()):
            row(year, g[g["year"] == year])
        emit()

    # The deeper ladder on the middle gate: where does the T+3 drift live?
    g = ev[np.abs(ev["move_pct"]) >= 5.0].copy()
    have3 = g[[f"O{k}" for k in (1, 2, 3)] + [f"C{k}" for k in (1, 2, 3)]].notna().all(axis=1)
    g = g[have3]
    s = np.sign(g["move_pct"].to_numpy())
    onemove = 1 + g["move_pct"].to_numpy() / 100
    legs = {
        "night0 (react->O1)": s * ((g["O1"] / g["CT"]) / onemove - 1) * 1e4,
        "day1 (O1->C1)": s * (g["C1"] / g["O1"] - 1) * 1e4,
        "night1 (C1->O2)": s * (g["O2"] / g["C1"] - 1) * 1e4,
        "day2 (O2->C2)": s * (g["C2"] / g["O2"] - 1) * 1e4,
        "night2 (C2->O3)": s * (g["O3"] / g["C2"] - 1) * 1e4,
        "day3 (O3->C3)": s * (g["C3"] / g["O3"] - 1) * 1e4,
    }
    emit("## Leg ladder to T+3, |reaction| >= 5% "
         f"(n={len(g)}, needs all six legs)")
    emit()
    emit("| leg | mean bp | median | t | >0 % |")
    emit("|---|---|---|---|---|")
    for name, x in legs.items():
        x = np.asarray(x, float)
        emit(f"| {name} | {x.mean():+.1f} | {np.median(x):+.1f} "
             f"| {tstat(x):+.2f} | {(x > 0).mean() * 100:.0f} |")
    nights = legs["night0 (react->O1)"] + legs["night1 (C1->O2)"] + legs["night2 (C2->O3)"]
    days = legs["day1 (O1->C1)"] + legs["day2 (O2->C2)"] + legs["day3 (O3->C3)"]
    emit()
    emit(f"Nights sum {np.mean(nights):+.1f}bp (t {tstat(np.asarray(nights)):+.2f}) vs "
         f"days sum {np.mean(days):+.1f}bp (t {tstat(np.asarray(days)):+.2f}) — "
         "additive approximation of the T+3 close-to-close hold.")

    out = NOTES / f"overnight_decomp_mech_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# ---------------------------------------------------------------- flagship

def stage_flagship(args) -> None:
    from research_holding_period import (
        FULL_MUT,
        account_slots,
        mutation_sides,
        paths_file,
        scored_events,
    )

    panel = load_ohlc()
    meta = scored_events(args.effort, False, "v2")[
        ["symbol", "date", "edate", "year", "move_pct", "run5d", "dv",
         "anchor", "direction", "confidence", "eps_vs_consensus",
         "revenue_vs_consensus", "guidance", "quality_flags", "gated"]]
    paths = pd.read_parquet(paths_file("v2"))
    p = (paths.drop(columns=["gated", "side"], errors="ignore")
         .merge(meta, on=["symbol", "date"], how="inner")
         .reset_index(drop=True))
    p = attach_legs(p, panel)

    # Horizon first (it decides which legs an event needs), then the leg mask.
    guid_moved = np.isin(p["guidance"].to_numpy(), ["raised", "lowered"])
    horizon = np.where(guid_moved, 3, 1)
    closes_len = np.array([len(c) for c in p["closes"]])
    need3 = p[[f"O{k}" for k in (1, 2, 3)] + [f"C{k}" for k in (1, 2, 3)]].notna().all(axis=1).to_numpy()
    need1 = p[["O1", "C1"]].notna().all(axis=1).to_numpy()
    ok = np.where(horizon == 3, need3 & (closes_len >= 3),
                  need1 & (closes_len >= 1))
    dropped = int((~ok).sum())
    p = p[ok].reset_index(drop=True)
    horizon = horizon[ok]
    n = len(p)

    sides = mutation_sides(p)
    entry = p["entry"].to_numpy()          # raw, the paper's own entry print
    # Raw session closes from the paths cache (15:55 prints, same basis as
    # the entry). Raw opens are recovered by scaling each session's ADJUSTED
    # open/close ratio — which no split or dividend adjustment can touch —
    # onto that session's raw close. Close-exit policies therefore reproduce
    # the paper's numbers identically; open exits differ only by the
    # session's own open/close ratio.
    Cr = {k: np.array([c[k - 1] if len(c) >= k else np.nan
                       for c in p["closes"]]) for k in (1, 2, 3)}
    Orw = {k: (p[f"O{k}"] / p[f"C{k}"]).to_numpy() * Cr[k] for k in (1, 2, 3)}

    def legs_for(side):
        s = side.astype(float)
        return {
            "night0": s * (Orw[1] / entry - 1),
            "day1": s * (Cr[1] / Orw[1] - 1),
            "night1": s * (Orw[2] / Cr[1] - 1),
            "day2": s * (Cr[2] / Orw[2] - 1),
            "night2": s * (Orw[3] / Cr[2] - 1),
            "day3": s * (Cr[3] / Orw[3] - 1),
        }

    def policy_returns(side):
        """Per-event net returns for each exit policy, at COSTS_BP."""
        s = side.astype(float)
        k3 = horizon == 3
        exit_close = np.where(k3, Cr[3], Cr[1])
        exit_open = np.where(k3, Orw[3], Orw[1])
        base = COSTS_BP / 1e4
        r = {
            "cond_close (paper flagship)": s * (exit_close / entry - 1) - base,
            "cond_open (exit at the open)": s * (exit_open / entry - 1) - base,
            "t1_close": s * (Cr[1] / entry - 1) - base,
            "t1_open": s * (Orw[1] / entry - 1) - base,
            # The decomposition's own suggestion: T+1 trades earn nothing
            # after the open (day1 t≈1.3), T+3 trades earn day2 (t≈3.3) —
            # so exit shorts holds at the open and long holds at the close.
            "hybrid (T+1 at open, T+3 at close)":
                s * (np.where(k3, Cr[3], Orw[1]) / entry - 1) - base,
        }
        ladder = (Orw[1] / entry) * np.where(k3, (Orw[2] / Cr[1]) * (Orw[3] / Cr[2]), 1.0)
        ladder_cost = (COSTS_BP + np.where(k3, 2 * LADDER_BP, 0.0)) / 1e4
        r["nights_only (ladder)"] = s * (ladder - 1) - ladder_cost
        days = (Cr[1] / Orw[1]) * np.where(k3, (Cr[2] / Orw[2]) * (Cr[3] / Orw[3]), 1.0)
        r["days_only (diagnostic)"] = s * (days - 1) - ladder_cost
        return r

    spy = pd.read_parquet(CACHE / "bench_SPY_2016-01-13_2026-08-06.parquet").sort_values("date")
    spy["d"] = pd.to_datetime(spy["date"]).dt.date
    lo, hi = p["edate"].min(), p["edate"].max()
    spy = spy[(spy["d"] >= lo)]
    sessions = spy["d"].to_list()
    spy_close = spy["close"].to_numpy()

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# Overnight decomposition, flagship panel — {STAMP}")
    emit()
    emit(f"{n} scored events with the legs their horizon needs ({dropped} "
         f"dropped for missing daily rows), conditional horizon T+3 on moved "
         f"guidance ({int((horizon == 3).sum())} extended), costs "
         f"{COSTS_BP:.0f}bp per round trip, ladder legs {LADDER_BP:.0f}bp per "
         "extra night. Close exits use the paper's own raw 15:55 closes, so "
         "t1_close and cond_close are the paper's mutation-table numbers by "
         "construction — read them as the regression check. Opens are the "
         "adjusted same-session open/close ratio chained onto those closes.")
    emit()

    for book_name, side in (("FULL POLICY (never stand down)", sides[FULL_MUT]),
                            ("THE GATE (agreements only)",
                             sides["THE GATE (verdict agrees with tape)"])):
        live = side != 0
        legs = legs_for(side)
        emit(f"## {book_name} — where the drift lives (n={int(live.sum())})")
        emit()
        emit("| leg | mean bp | t | >0 % |")
        emit("|---|---|---|---|")
        k3 = (horizon == 3) & live
        for name in ("night0", "day1"):
            x = legs[name][live] * 1e4
            emit(f"| {name} (all trades) | {x.mean():+.1f} | {tstat(x):+.2f} "
                 f"| {(x > 0).mean() * 100:.0f} |")
        for name in ("night1", "day2", "night2", "day3"):
            x = legs[name][k3] * 1e4
            emit(f"| {name} (T+3 subset, n={int(k3.sum())}) | {x.mean():+.1f} "
                 f"| {tstat(x):+.2f} | {(x > 0).mean() * 100:.0f} |")
        emit()

    emit("## Exit policies through the 6-slot account (FULL POLICY sides)")
    emit()
    side = sides[FULL_MUT]
    live = side != 0
    rets = policy_returns(side)
    emit(f"| policy | mean bp | win% | t | CAGR | Sharpe | maxDD | "
         f"CAGR@{MEASURED_BP}bp |")
    emit("|---|---|---|---|---|---|---|---|")
    for name, ret in rets.items():
        r = ret[live]
        a = account_slots(p[live].reset_index(drop=True), r, horizon[live],
                          sessions, spy_close)
        extra = (MEASURED_BP - COSTS_BP) / 1e4   # sensitivity: measured spread
        a2 = account_slots(p[live].reset_index(drop=True), r - extra,
                           horizon[live], sessions, spy_close)
        emit(f"| {name} | {r.mean() * 1e4:+.1f} | {(r > 0).mean() * 100:.1f} "
             f"| {tstat(r):+.2f} | {a['cagr_pct']:.1f}% | {a['sharpe']:.2f} "
             f"| {a['max_dd_pct']:.1f}% | {a2['cagr_pct']:.1f}% |")
    emit()
    emit("Same trades, same slots, same sessions — only the exit clock moves. "
         "The ladder pays its extra legs; the diagnostic days-only book is "
         "what the exits would leave behind.")

    out = NOTES / f"overnight_decomp_flagship_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["mech", "flagship"])
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    {"mech": stage_mech, "flagship": stage_flagship}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
