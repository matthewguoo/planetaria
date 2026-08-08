"""The retail canon, mechanised: five setups, one exit engine, one null.

WHAT IS BEING TESTED HERE, AND WHAT IS NOT. This module answers the boring
half of the study — do the patterns themselves pay — and it answers it
EXHAUSTIVELY: every detector runs on every session of every symbol, because
that is free (it is numpy) and because a sampled mechanical baseline would be
a strictly worse thing to compare the LLM against. The interesting half, "does
a model reading the chart add anything to the rule", lives in
`research_chart_gate.py` and runs on a sample.

THE PRIOR THIS STUDY INHERITS. `research/pead-llm-gate/notes/ict_backtest_20260805.md`
already put 24 configurations and ~130k trades through a near-identical
harness: iFVG came out indistinguishable from random-minus-costs, and the
PO3 sweep-reclaim fade was ACTIVELY anti-predictive (null percentiles 0.0-2.5
— random entries beat it). So the base rate for "this pattern pays" in this
repo is zero for two of two. The five setups below are chosen to be the ones
retail actually trades and to overlap that prior work as little as possible:
four are new, and the one sweep setup deliberately uses the PRIOR DAY's RTH
extremes rather than PO3's overnight range so it is a different level, not a
rerun.

THE FIVE. Parameters come from how the setups are taught, not from anything
fitted here. Every one of them is a number a YouTube video would state out
loud, and none was moved after seeing a result — which is the only thing that
makes the exhaustive run honest, since with 12 symbols x 5 strategies there
are plenty of knobs to torture.

  orb     15-minute opening range (09:30-09:45), first 5m close outside it.
          Stop at the opposite extreme, so R is the range height.
  vwap    Anchored session VWAP. Five consecutive closes on one side, then a
          close through it. Stop at the 5-bar swing.
  ema     9/21 EMA cross on closes. Stop at the 5-bar swing.
  sweep   Prior day's RTH high/low taken out intrabar and closed back inside
          on the same bar. Fade it. Stop at the sweep extreme.
  flag    >=0.5% pole over <=6 bars, 3-8 bars of consolidation inside 40% of
          the pole's range, break in the pole's direction.

ALL FIVE SHARE: entries only 09:30-15:00 ET, 2R target, 100-bar time stop,
flat by the close, stop wins intrabar ties. Fixing the bracket across
strategies is what makes the random null a fair comparison — it holds the
geometry constant so the only thing varying is WHERE the entry came from.

Run:
    python scripts/research_chart_setups.py build            # -> cache/setups.parquet
    python scripts/research_chart_setups.py report           # the exhaustive table
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from _paths import BACKEND, CACHE, NOTES

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_chart_data import (  # noqa: E402
    COST_BP,
    UNIVERSE,
    bars_path,
    resample,
    rth_mask,
    session_minutes,
)

SETUPS = CACHE / "setups.parquet"

TF = 5                  # minutes per bar; the timeframe the canon is taught on
TP_R = 2.0              # 2R target on every strategy
TIME_STOP = 100         # bars
ENTRY_LO = 15           # no entry in the first 15 min (the opening range itself)
ENTRY_HI = 330          # no entry after 15:00 ET — 30 min is not a hold
N_NULL = 200            # random-entry portfolios per strategy/symbol cell

ORB_MIN = 15            # opening range = 09:30-09:45
VWAP_SIDE_BARS = 5      # consecutive closes on one side before a reclaim counts
SWING_BARS = 5          # swing lookback for stops
FLAG_POLE_BARS = 6      # max bars in the pole
FLAG_POLE_PCT = 0.5     # min pole size, %
FLAG_MIN, FLAG_MAX = 3, 8       # consolidation length, bars
FLAG_TIGHT = 0.4        # consolidation range as a fraction of the pole's

STRATEGIES = ["orb", "vwap", "ema", "sweep", "flag"]


# ----------------------------------------------------------- exit engine

def run_exit(h, l, c, day, i, side, entry, stop, tp,
             max_hold=TIME_STOP) -> tuple[float, int, str]:
    """Walk forward from bar i+1. Returns ($/share gross, exit index, reason).

    Stop wins intrabar ties: if a bar's range covers both the stop and the
    target we assume the stop. That is the conservative read and it is the
    one place a backtest can flatter itself by an entire strategy's worth of
    edge, because on a 2R bracket the losers are the frequent ones.

    Never holds overnight: a gap is not a thing an intraday setup earned.
    """
    n = len(c)
    end = min(i + 1 + max_hold, n)
    for j in range(i + 1, end):
        if day[j] != day[i]:
            return side * (c[j - 1] - entry), j - 1, "session_end"
        if side > 0:
            if l[j] <= stop:
                return stop - entry, j, "stop"
            if h[j] >= tp:
                return tp - entry, j, "target"
        else:
            if h[j] >= stop:
                return entry - stop, j, "stop"
            if l[j] <= tp:
                return entry - tp, j, "target"
    j = min(end, n) - 1
    return side * (c[j] - entry), j, "time_stop"


# ------------------------------------------------------------- frame prep

def prep(df1m: pd.DataFrame, tf: int = TF) -> pd.DataFrame:
    """RTH-only resampled frame with the per-session scaffolding every
    detector needs: session date, minutes since 09:30, anchored VWAP, EMAs,
    and the PRIOR session's RTH extremes.

    Everything here is causal by construction — cumulative sums and ewm both
    look backwards only — but `sweep` needs yesterday's completed extremes,
    which is the one place a shift is required and it is done explicitly.
    """
    df = resample(df1m, tf)
    df = df[rth_mask(df.index)].copy()
    if df.empty:
        return df
    df["day"] = df.index.normalize()
    df["min"] = session_minutes(df.index)

    tp = (df["h"] + df["l"] + df["c"]) / 3
    g = df.groupby("day", sort=False)
    df["vwap"] = ((tp * df["v"]).groupby(df["day"]).cumsum()
                  / g["v"].cumsum().replace(0, np.nan))

    # EMAs run continuously across sessions, which is what a retail platform
    # shows on an RTH chart. The overnight gap becomes a jump in the series;
    # that is the series the setup is actually taught on.
    df["ema9"] = df["c"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()

    daily = df.groupby("day").agg(dh=("h", "max"), dl=("l", "min"))
    df["pdh"] = df["day"].map(daily["dh"].shift(1))
    df["pdl"] = df["day"].map(daily["dl"].shift(1))

    orb = df[df["min"] < ORB_MIN].groupby("day").agg(orh=("h", "max"),
                                                     orl=("l", "min"))
    df["orh"] = df["day"].map(orb["orh"])
    df["orl"] = df["day"].map(orb["orl"])
    return df.reset_index().rename(columns={"index": "ts"})


def _features(df: pd.DataFrame, i: int) -> dict:
    """The handful of numbers a chart-reader is actually looking at, measured
    at the decision bar and never after it.

    These exist for the feature-matched null in `research_chart_score.py`:
    if a logistic regression on THESE gates the same trades as well as the
    LLM does, then whatever the model is doing is arithmetic anyone could do
    in a spreadsheet, and "the model reads charts" is not the right
    description of it.
    """
    c = df["c"].to_numpy()
    lo = max(0, i - 20)
    win = c[lo:i + 1]
    rets = np.diff(win) / win[:-1] if len(win) > 2 else np.array([0.0])
    today = df.iloc[:i + 1]
    today = today[today["day"] == df["day"].iat[i]]
    dh, dl = today["h"].max(), today["l"].min()
    v = df["v"].to_numpy()
    vmed = np.median(v[lo:i]) if i > lo else v[i]
    return {
        "f_ret20_bp": float((c[i] / win[0] - 1) * 1e4) if len(win) > 1 else 0.0,
        "f_rvol_bp": float(np.std(rets) * 1e4),
        "f_pos_in_day": float((c[i] - dl) / (dh - dl)) if dh > dl else 0.5,
        "f_vol_ratio": float(v[i] / vmed) if vmed > 0 else 1.0,
        "f_min": int(df["min"].iat[i]),
        "f_vwap_dist_bp": float((c[i] / df["vwap"].iat[i] - 1) * 1e4)
        if df["vwap"].iat[i] > 0 else 0.0,
    }


# ------------------------------------------------------------- detectors
#
# Each returns a list of (bar index, side, stop). Entry is always the CLOSE of
# the signal bar, so the decision uses only bars 0..i and the exit engine only
# bars i+1.., and the renderer's window ends at i. That split is the whole
# no-lookahead argument and it is why the detectors return an index rather
# than a price.

def _sig_orb(df: pd.DataFrame) -> list[tuple[int, int, float]]:
    out, fired = [], set()
    c, orh, orl = df["c"].to_numpy(), df["orh"].to_numpy(), df["orl"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    for i in range(len(df)):
        if not (ORB_MIN <= mins[i] <= ENTRY_HI) or np.isnan(orh[i]):
            continue
        for side, key in ((1, "L"), (-1, "S")):
            if (days[i], key) in fired:
                continue
            if side > 0 and c[i] > orh[i]:
                out.append((i, 1, float(orl[i])))
                fired.add((days[i], key))
            elif side < 0 and c[i] < orl[i]:
                out.append((i, -1, float(orh[i])))
                fired.add((days[i], key))
    return out


def _swing_stop(df: pd.DataFrame, i: int, side: int) -> float:
    lo = max(0, i - SWING_BARS + 1)
    return (float(df["l"].iloc[lo:i + 1].min()) if side > 0
            else float(df["h"].iloc[lo:i + 1].max()))


def _sig_vwap(df: pd.DataFrame) -> list[tuple[int, int, float]]:
    out = []
    c, vw = df["c"].to_numpy(), df["vwap"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    above = c > vw
    for i in range(VWAP_SIDE_BARS, len(df)):
        if not (30 <= mins[i] <= ENTRY_HI) or np.isnan(vw[i]):
            continue
        prev = slice(i - VWAP_SIDE_BARS, i)
        # The whole run of prior closes must be same-session, else a
        # "reclaim" can be stitched out of yesterday's afternoon.
        if days[i - VWAP_SIDE_BARS] != days[i]:
            continue
        if above[i] and not above[prev].any():
            out.append((i, 1, _swing_stop(df, i, 1)))
        elif not above[i] and above[prev].all():
            out.append((i, -1, _swing_stop(df, i, -1)))
    return out


def _sig_ema(df: pd.DataFrame) -> list[tuple[int, int, float]]:
    out = []
    e9, e21 = df["ema9"].to_numpy(), df["ema21"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    for i in range(21, len(df)):
        if not (30 <= mins[i] <= ENTRY_HI) or days[i] != days[i - 1]:
            continue
        if e9[i] > e21[i] and e9[i - 1] <= e21[i - 1]:
            out.append((i, 1, _swing_stop(df, i, 1)))
        elif e9[i] < e21[i] and e9[i - 1] >= e21[i - 1]:
            out.append((i, -1, _swing_stop(df, i, -1)))
    return out


def _sig_sweep(df: pd.DataFrame) -> list[tuple[int, int, float]]:
    """Wick through yesterday's extreme, close back inside: fade it.

    Deliberately NOT the archived PO3 setup, which swept the OVERNIGHT range
    and came back anti-predictive. Same family, different level.
    """
    out, fired = [], set()
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    pdh, pdl = df["pdh"].to_numpy(), df["pdl"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    for i in range(len(df)):
        if not (ENTRY_LO <= mins[i] <= ENTRY_HI) or np.isnan(pdh[i]):
            continue
        if h[i] > pdh[i] and c[i] < pdh[i] and (days[i], "S") not in fired:
            out.append((i, -1, float(h[i])))
            fired.add((days[i], "S"))
        elif l[i] < pdl[i] and c[i] > pdl[i] and (days[i], "L") not in fired:
            out.append((i, 1, float(l[i])))
            fired.add((days[i], "L"))
    return out


def _sig_flag(df: pd.DataFrame) -> list[tuple[int, int, float]]:
    out = []
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    for i in range(FLAG_POLE_BARS + FLAG_MAX + 1, len(df)):
        if not (30 <= mins[i] <= ENTRY_HI):
            continue
        for f in range(FLAG_MIN, FLAG_MAX + 1):
            fs, pe = i - f, i - f            # flag bars [fs, i-1]; pole ends pe
            ps = pe - FLAG_POLE_BARS
            if ps < 0 or days[ps] != days[i]:
                continue
            pole = (c[pe] / c[ps] - 1) * 100
            pole_rng = max(h[ps:pe + 1].max() - l[ps:pe + 1].min(), 1e-9)
            flag_rng = h[fs:i].max() - l[fs:i].min()
            if flag_rng > FLAG_TIGHT * pole_rng:
                continue
            if pole >= FLAG_POLE_PCT and c[i] > h[fs:i].max():
                out.append((i, 1, float(l[fs:i].min())))
                break
            if pole <= -FLAG_POLE_PCT and c[i] < l[fs:i].min():
                out.append((i, -1, float(h[fs:i].max())))
                break
    return out


DETECTORS = {"orb": _sig_orb, "vwap": _sig_vwap, "ema": _sig_ema,
             "sweep": _sig_sweep, "flag": _sig_flag}


# ------------------------------------------------------------------ build

def build_symbol(symbol: str, tf: int = TF) -> pd.DataFrame:
    p = bars_path(symbol)
    if not p.exists():
        print(f"  {symbol}: no bars cached — run research_chart_data.py fetch")
        return pd.DataFrame()
    df = prep(pd.read_parquet(p), tf)
    if df.empty:
        return pd.DataFrame()
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    day = df["day"].to_numpy()
    rows = []
    for name, fn in DETECTORS.items():
        for i, side, stop in fn(df):
            entry = float(c[i])
            r = abs(entry - stop)
            # A stop inside a tick is not a trade, it is a division by zero
            # wearing a trade's clothes: R in the denominator of every R
            # multiple downstream. 5bp is ~30c on a $600 name.
            if r <= 0 or (r / entry) * 1e4 < 5:
                continue
            tp = entry + side * TP_R * r
            res, j, why = run_exit(h, l, c, day, i, side, entry, stop, tp)
            rows.append({
                "symbol": symbol, "strategy": name, "tf": tf,
                "ts": df["ts"].iat[i], "date": str(df["day"].iat[i].date()),
                "i": i, "side": side, "entry": entry, "stop": stop,
                "target": tp, "r_bp": (r / entry) * 1e4,
                "gross_bp": (res / entry) * 1e4,
                "exit_i": j, "exit_reason": why, "held": j - i,
                **_features(df, i),
            })
    return pd.DataFrame(rows)


def stage_build(args) -> None:
    syms = args.symbols.split(",") if args.symbols else UNIVERSE
    frames = []
    for sym in syms:
        d = build_symbol(sym, args.tf)
        if len(d):
            print(f"{sym}: {len(d):,} setups "
                  + " ".join(f"{k}={v}" for k, v in
                             d['strategy'].value_counts().sort_index().items()),
                  flush=True)
            frames.append(d)
    if not frames:
        raise SystemExit("no setups built")
    out = pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"])
    out["net_bp"] = out["gross_bp"] - COST_BP
    out = out.reset_index(drop=True)
    out["setup_id"] = (out["symbol"] + "_" + out["strategy"] + "_"
                       + out["ts"].dt.strftime("%Y%m%dT%H%M"))
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_parquet(SETUPS)
    print(f"\nwrote {len(out):,} setups -> {SETUPS.name} "
          f"({out['date'].min()}..{out['date'].max()})")


# ------------------------------------------------------------------- null

NULL_POOL = 3000        # random entries drawn per (symbol, strategy) cell

_PREP_CACHE: dict[tuple[str, int], pd.DataFrame] = {}


def _prep_cached(symbol: str, tf: int) -> pd.DataFrame:
    key = (symbol, tf)
    if key not in _PREP_CACHE:
        _PREP_CACHE[key] = prep(pd.read_parquet(bars_path(symbol)), tf)
    return _PREP_CACHE[key]


def random_null(symbol: str, n_trades: int, r_bp: float, tf: int,
                rng: np.random.Generator, runs: int = N_NULL) -> np.ndarray:
    """Mean net bp/trade for `runs` random-entry portfolios through the SAME
    bracket: random RTH bar, random side, stop at the strategy's own mean R,
    2R target, same time stop and session-end rule.

    This is the comparison that matters. A strategy whose mean sits inside
    this distribution has not shown that its pattern does anything — it has
    shown that a 2R bracket on a liquid name produces roughly that number no
    matter where you enter.

    DRAW ONCE, BOOTSTRAP MANY. The obvious implementation redraws a fresh set
    of random entries for each of the 200 portfolios, which is what the
    archived ICT harness did; at this study's size that is ~300M bar-steps of
    interpreted Python per strategy and it does not finish. Drawing one pool
    of NULL_POOL random entries and resampling portfolios from it is both
    ~60x cheaper and the cleaner statistic: it isolates the sampling variance
    of a portfolio of size n, which is the thing the strategy's own mean has
    to clear, instead of mixing that with variance in which bars got drawn.
    """
    df = _prep_cached(symbol, tf)
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    day = df["day"].to_numpy()
    n = len(c)
    idx = rng.integers(2, n - 2, size=NULL_POOL)
    sides = rng.choice([1, -1], size=NULL_POOL)
    pool = np.empty(NULL_POOL)
    for k, (i, s) in enumerate(zip(idx, sides)):
        entry = c[i]
        r = entry * r_bp / 1e4
        res, _, _ = run_exit(h, l, c, day, int(i), int(s), entry,
                             entry - s * r, entry + s * TP_R * r)
        pool[k] = (res / entry) * 1e4 - COST_BP
    draw = rng.integers(0, NULL_POOL, size=(runs, min(n_trades, NULL_POOL)))
    return pool[draw].mean(axis=1)


def stage_report(args) -> None:
    if not SETUPS.exists():
        raise SystemExit("no setups.parquet — run `build` first")
    s = pd.read_parquet(SETUPS)
    rng = np.random.default_rng(20260807)
    lines: list[str] = []

    def emit(t: str = "") -> None:
        print(t)
        lines.append(t)

    emit(f"# The retail canon, mechanised — {date.today()}")
    emit("")
    emit(f"{len(s):,} setups, {s['symbol'].nunique()} symbols, "
         f"{s['date'].min()}..{s['date'].max()}, {args.tf}m bars, "
         f"{COST_BP:.0f}bp round-trip costs. Every session of every symbol — "
         f"nothing sampled.")
    emit("")
    emit("## By strategy (pooled across symbols)")
    emit("")
    emit("| strategy | trades | win% | mean net bp | t | median R bp | "
         "target% | stop% | null pctile |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for name in STRATEGIES:
        g = s[s["strategy"] == name]
        if g.empty:
            continue
        net = g["net_bp"].to_numpy()
        t = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if len(net) > 1 else 0
        # Null is computed per symbol and pooled with the same symbol weights
        # the strategy actually has, so a strategy concentrated in one name is
        # compared against that name's tape rather than a universe average.
        nulls = []
        for sym, gs in g.groupby("symbol"):
            nulls.append(random_null(sym, len(gs), float(gs["r_bp"].mean()),
                                     args.tf, rng, runs=args.null_runs)
                         * len(gs))
        pooled = np.sum(nulls, axis=0) / len(g)
        pct = float((pooled < net.mean()).mean() * 100)
        rc = g["exit_reason"].value_counts(normalize=True) * 100
        emit(f"| {name} | {len(g):,} | {(net > 0).mean() * 100:.1f} | "
             f"{net.mean():+.2f} | {t:+.2f} | {g['r_bp'].median():.0f} | "
             f"{rc.get('target', 0):.0f} | {rc.get('stop', 0):.0f} | "
             f"{pct:.1f} |")
    emit("")
    emit("`null pctile` is the share of 200 random-entry portfolios that came "
         "in BELOW the strategy's mean, pushed through an identical bracket. "
         "50 means the pattern adds nothing over entering at random; under 50 "
         "means random entries did better.")
    emit("")

    emit("## By strategy x symbol (mean net bp, trade count)")
    emit("")
    piv = s.pivot_table(index="symbol", columns="strategy", values="net_bp",
                        aggfunc=["mean", "count"])
    emit("| symbol | " + " | ".join(STRATEGIES) + " |")
    emit("|---" * (len(STRATEGIES) + 1) + "|")
    for sym in sorted(s["symbol"].unique()):
        cells = []
        for st in STRATEGIES:
            try:
                m, n = piv[("mean", st)][sym], piv[("count", st)][sym]
                cells.append(f"{m:+.1f} ({int(n)})" if pd.notna(m) else "—")
            except KeyError:
                cells.append("—")
        emit(f"| {sym} | " + " | ".join(cells) + " |")
    emit("")

    emit("## Cost sensitivity (mean net bp per trade)")
    emit("")
    emit("| strategy | 0bp | 2bp | 5bp | 10bp |")
    emit("|---|---|---|---|---|")
    for name in STRATEGIES:
        g = s[s["strategy"] == name]["gross_bp"]
        if g.empty:
            continue
        emit(f"| {name} | " + " | ".join(f"{g.mean() - c:+.2f}"
                                         for c in (0, 2, 5, 10)) + " |")
    emit("")
    emit("The 2bp column is the headline. A strategy that only works at 0bp "
         "is a strategy that pays the spread to the market maker.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"mechanical_{date.today():%Y%m%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["build", "report"])
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--tf", type=int, default=TF)
    ap.add_argument("--null-runs", type=int, default=N_NULL)
    args = ap.parse_args()
    {"build": stage_build, "report": stage_report}[args.stage](args)


if __name__ == "__main__":
    main()
