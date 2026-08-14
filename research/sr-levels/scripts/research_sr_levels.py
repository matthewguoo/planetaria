"""The stop-loss meme, mechanised: three level patterns, one bracket, one null.

THE PROMPT. A meme that every trading feed eventually serves: "HOW TO SET
STOP LOSS", three panels — a rising TRENDLINE with the stop under the line, a
horizontal BREAKOUT with the stop under the broken level, a ZONE TO ZONE range
with the stop under the support zone. The question this study answers is the
one the meme begs: if you detect those three patterns mechanically on 5-minute
candles and place the stop exactly where the picture says, does the trade pay —
and can a 0DTE or 1DTE option carry it?

WHAT THIS STUDY INHERITS. `chart-llm-gate` already pushed the other half of
the retail canon (orb, vwap-reclaim, ema-cross, sweep-fade, bull/bear flag)
through this exact harness on a year of 5m bars: all five died net of 2bp
costs, and only the flag beat its own random-entry null. The base rate for
"a taught intraday pattern pays" in this repo is now 1-of-7 gross, 0-of-7 net
(counting the two archived ICT setups). These three setups are the remaining
canon: the ones about LEVELS rather than indicators, with the meme's own stop
discipline — the stop at the pattern line itself, not at a swing.

THE THREE. Parameters are how the setups are taught, stated out loud, none
moved after seeing a result. All structure is same-session (an intraday level
crossing an overnight gap is a different object than the one in the picture).

  trendline  >=3 confirmed pivot lows (2-bar fractals) strictly rising, the
             middle one within 15bp of the line through the outer two. Price
             touches the projected line and closes back above it: long, stop
             AT THE LINE. Falling line through pivot highs mirrored short.
  breakout   The session high so far, tested >=2 times (highs within 10bp,
             tests >=3 bars apart, first test >=30 min before entry).
             First 5m close through it: long, stop AT THE LEVEL. Session low
             mirrored short. Not a rerun of orb: orb breaks a fixed 09:30-45
             range at any margin with the stop at the opposite extreme; this
             breaks a TESTED level wherever it formed, stop at the line.
  zone       Session range so far >=25bp high with >=2 tests of each edge
             (test = inside the 20%-of-height band). A wick into one band
             that closes back out fades it: stop just past the range extreme
             (10%-of-height of wick-through allowed), target the NEAR EDGE of
             the opposite band — zone to zone, the meme's own exit, so this
             one keeps its taught target rather than the 2R bracket.

SHARED WITH THE PRIOR FIVE: entry at the signal close, entries 09:45-15:00 ET
only, stop wins intrabar ties, 100-bar time stop, flat by the close, R >= 5bp
or it is not a trade, 2bp round-trip cost. trendline/breakout take the canon's
2R target. The null holds the same geometry per strategy (its own mean R and
target multiple) at random entries — a pattern must beat its own bracket on
its own tape, not just drift.

DATA, HONESTLY. This container has no Alpaca keys, so the fetch stage pulls
what a keyless machine can: Yahoo's chart API, which serves 5m bars for the
trailing 60 calendar days only (~40 sessions x 12 symbols). That is enough
bars to measure a per-trade edge in the hundreds-of-trades regime but it is
one-quarter of the chart-llm-gate year; `load_bars` therefore prefers that
study's 1m SIP cache whenever it exists, so re-running `build`/`report` on
the dev box repeats the study on the full year with zero code change. The
0/1DTE wrap is BSM-modeled (see `stage_wrap`) because option prints need the
keys; the honest upgrade is the 0dte-vrp fetch machinery.

Run:
    python scripts/research_sr_levels.py fetch     # bars -> cache/
    python scripts/research_sr_levels.py build     # detect + bracket -> cache/setups.parquet
    python scripts/research_sr_levels.py report    # the note: table + null + costs
    python scripts/research_sr_levels.py wrap      # the 0/1DTE overlay note (SPY/QQQ)
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
CHART_BARS = ROOT / "research" / "chart-llm-gate" / "cache" / "bars"
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")

# Same universe as chart-llm-gate, same reason: the names retail actually
# daytrades, with the two index ETFs as controls — and for this study SPY/QQQ
# carry the second question, because they are the only tickers with a daily
# expiration to put a 0DTE on.
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
            "TSLA", "AMD", "AVGO", "NFLX", "SPY", "QQQ"]

COST_BP = 2.0           # round trip, bp of notional; swept 0/2/5/10 in the note
TF = 5                  # minutes per bar
TP_R = 2.0              # trendline/breakout target; zone targets the far band
TIME_STOP = 100         # bars
ENTRY_LO = 15           # no entry in the first 15 min
ENTRY_HI = 330          # none after 15:00 ET
MIN_R_BP = 5.0          # a stop inside a tick is not a trade

PIV_W = 2               # pivot = 2-bar fractal, confirmed 2 bars later
LINE_TOL_BP = 15.0      # middle pivot must sit within this of the line
TOUCH_TOL_BP = 10.0     # a "test" of a horizontal level
BREAK_MIN_SEP = 3       # bars between the two tests
BREAK_AGE = 6           # first test >= 30 min before entry: a level is a
                        # memory, not a print. (The first cut required the
                        # LAST touch to be old instead, which is geometrically
                        # wrong — a breakout approaches its level on the way
                        # through, so that filtered real breaks and kept only
                        # gap-like jumps: 46 signals in 720 symbol-days, QQQ
                        # zero. Changed on fire-rate alone, before any P&L
                        # was computed.)
ZONE_MIN_BARS = 24      # 2h of session before a range is a range
ZONE_MIN_HT_BP = 25.0   # a zone worth trading zone-to-zone
ZONE_BAND = 0.20        # each zone = 20% of the range height
ZONE_WICK = 0.10        # wick-through allowance past the extreme
BRK_MIN_BARS = 18       # 90 min of session before a level is a level

STRATEGIES = ["trendline", "breakout", "zone"]

RTH_OPEN = 9 * 60 + 30
RTH_CLOSE = 16 * 60

N_NULL = 200
NULL_POOL = 3000


def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / np.sqrt(len(x)))) if sd > 0 else 0.0


# ------------------------------------------------------------------- fetch

YQ = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"User-Agent": "Mozilla/5.0 (research; planetaria sr-levels)"}


def _yahoo_chart(sym: str, interval: str, rng: str) -> dict:
    import requests
    for attempt in range(4):
        r = requests.get(YQ.format(sym=sym),
                         params={"interval": interval, "range": rng,
                                 "includePrePost": "false"},
                         headers=UA, timeout=30)
        if r.status_code == 429:            # Yahoo rate limit: back off, retry
            _time.sleep(3.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()["chart"]["result"][0]
    raise RuntimeError(f"{sym}: rate-limited 4 times")


def _bars_frame(res: dict) -> pd.DataFrame:
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"ts": res["timestamp"], "o": q["open"], "h": q["high"],
                       "l": q["low"], "c": q["close"], "v": q["volume"]})
    df = df.dropna(subset=["o", "h", "l", "c"])
    df["ts"] = (pd.to_datetime(df["ts"], unit="s", utc=True)
                .dt.tz_convert(ET))
    df = df.set_index("ts")
    return df[~df.index.duplicated(keep="first")].sort_index()


def yahoo_path(sym: str) -> Path:
    return CACHE / f"{sym}_5m_yahoo.parquet"


def stage_fetch(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for sym in (args.symbols.split(",") if args.symbols else UNIVERSE):
        df = _bars_frame(_yahoo_chart(sym, "5m", "60d"))
        df = df[rth_mask(df.index)]
        df.to_parquet(yahoo_path(sym))
        print(f"{sym}: {len(df):,} RTH 5m bars, "
              f"{df.index.normalize().nunique()} sessions "
              f"[{df.index.min():%Y-%m-%d}..{df.index.max():%Y-%m-%d}]")
        _time.sleep(0.6)
    # IV proxies for the wrap: the 9-day S&P vol index for SPY, the Nasdaq-100
    # vol index for QQQ. Daily closes; the wrap uses the PRIOR session's close
    # so the mark is causal at entry.
    iv = {}
    for name, tick in (("vix9d", "^VIX9D"), ("vxn", "^VXN")):
        res = _yahoo_chart(tick, "1d", "4mo")
        d = _bars_frame(res)
        iv[name] = pd.Series(d["c"].to_numpy(),
                             index=d.index.normalize().date)
        _time.sleep(0.6)
    ivf = pd.DataFrame(iv)
    ivf.index = ivf.index.astype(str)
    ivf.to_parquet(CACHE / "iv_proxy.parquet")
    print(f"iv proxies: {len(ivf)} sessions "
          f"[{ivf.index.min()}..{ivf.index.max()}]")


# -------------------------------------------------------------- frame prep

def rth_mask(index: pd.DatetimeIndex) -> np.ndarray:
    mins = index.hour * 60 + index.minute
    return np.asarray((index.weekday < 5) & (mins >= RTH_OPEN)
                      & (mins < RTH_CLOSE))


def resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 1:
        return df1m
    return (df1m.resample(f"{minutes}min", label="left", closed="left")
            .agg({"o": "first", "h": "max", "l": "min", "c": "last",
                  "v": "sum"}).dropna())


def load_bars(sym: str) -> pd.DataFrame | None:
    """chart-llm-gate's 1m SIP year when present (the dev box), else the
    keyless Yahoo 60d cache. Same frame either way: RTH 5m o/h/l/c/v."""
    hits = sorted(CHART_BARS.glob(f"{sym}_*_1m.parquet")) if CHART_BARS.exists() else []
    if hits:
        df = resample(pd.read_parquet(hits[-1]), TF)
        return df[rth_mask(df.index)].copy()
    p = yahoo_path(sym)
    if p.exists():
        return pd.read_parquet(p)
    return None


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["day"] = df.index.normalize()
    df["min"] = np.asarray(df.index.hour * 60 + df.index.minute - RTH_OPEN)
    return df.reset_index().rename(columns={df.index.name or "index": "ts",
                                            "ts": "ts"})


# ------------------------------------------------------------- exit engine
# Identical semantics to chart-llm-gate's: stop wins intrabar ties (the one
# place a bracket backtest can flatter itself by a strategy's worth of edge),
# never holds overnight.

def run_exit(h, l, c, day, i, side, entry, stop, tp,
             max_hold=TIME_STOP) -> tuple[float, int, str]:
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


# -------------------------------------------------------------- detectors
# Each returns (bar index, side, stop, tp_or_None). Entry is the CLOSE of the
# signal bar; decisions use bars 0..i only. A None tp means the standard 2R.

def _session_start(days: np.ndarray, i: int) -> int:
    j = i
    while j > 0 and days[j - 1] == days[i]:
        j -= 1
    return j


def _pivots(x: np.ndarray, s0: int, i: int, kind: str) -> list[int]:
    """Confirmed 2-bar fractals in [s0, i-PIV_W], same session by slicing."""
    out = []
    for j in range(s0 + PIV_W, i - PIV_W + 1):
        w = x[j - PIV_W: j + PIV_W + 1]
        if kind == "lo" and x[j] == w.min() and (w > x[j]).sum() == 2 * PIV_W:
            out.append(j)
        elif kind == "hi" and x[j] == w.max() and (w < x[j]).sum() == 2 * PIV_W:
            out.append(j)
    return out


def _sig_trendline(df: pd.DataFrame) -> list[tuple[int, int, float, float | None]]:
    out = []
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    last_fire = -10
    for i in range(len(df)):
        if not (ENTRY_LO <= mins[i] <= ENTRY_HI) or i - last_fire < 2:
            continue
        s0 = _session_start(days, i)
        for kind, side in (("lo", 1), ("hi", -1)):
            x = l if side > 0 else h
            piv = _pivots(x, s0, i, kind)
            if len(piv) < 3:
                continue
            p1, p2, p3 = piv[-3], piv[-2], piv[-1]
            slope = (x[p3] - x[p1]) / (p3 - p1)
            if side > 0 and (slope <= 0 or not x[p1] < x[p2] < x[p3]):
                continue
            if side < 0 and (slope >= 0 or not x[p1] > x[p2] > x[p3]):
                continue
            mid_line = x[p1] + slope * (p2 - p1)
            if abs(x[p2] - mid_line) / c[i] * 1e4 > LINE_TOL_BP:
                continue
            line = x[p1] + slope * (i - p1)
            tol = c[i] * TOUCH_TOL_BP / 1e4
            if side > 0 and l[i] <= line + tol and c[i] > line \
                    and c[i - 1] > line - slope:
                out.append((i, 1, float(line), None))
                last_fire = i
                break
            if side < 0 and h[i] >= line - tol and c[i] < line \
                    and c[i - 1] < line - slope:
                out.append((i, -1, float(line), None))
                last_fire = i
                break
    return out


def _sig_breakout(df: pd.DataFrame) -> list[tuple[int, int, float, float | None]]:
    out, fired = [], set()
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    for i in range(len(df)):
        if not (ENTRY_LO <= mins[i] <= ENTRY_HI):
            continue
        s0 = _session_start(days, i)
        if i - s0 < BRK_MIN_BARS:
            continue
        for side, key in ((1, "L"), (-1, "S")):
            if (days[i], key) in fired:
                continue
            seg = h[s0:i] if side > 0 else l[s0:i]
            level = seg.max() if side > 0 else seg.min()
            tol = level * TOUCH_TOL_BP / 1e4
            touches = np.flatnonzero(np.abs(seg - level) <= tol)
            # two distinct tests, apart in time, and the FIRST one old
            # enough that the level was on the chart before the break
            if (len(touches) < 2 or touches[-1] - touches[0] < BREAK_MIN_SEP
                    or (i - s0) - touches[0] < BREAK_AGE):
                continue
            if side > 0 and c[i] > level:
                out.append((i, 1, float(level), None))
                fired.add((days[i], key))
            elif side < 0 and c[i] < level:
                out.append((i, -1, float(level), None))
                fired.add((days[i], key))
    return out


def _sig_zone(df: pd.DataFrame) -> list[tuple[int, int, float, float | None]]:
    out = []
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    days, mins = df["day"].to_numpy(), df["min"].to_numpy()
    last_fire = -10
    for i in range(len(df)):
        if not (ENTRY_LO <= mins[i] <= ENTRY_HI) or i - last_fire < 2:
            continue
        s0 = _session_start(days, i)
        if i - s0 < ZONE_MIN_BARS:
            continue
        hi, lo = h[s0:i].max(), l[s0:i].min()
        ht = hi - lo
        if ht / c[i] * 1e4 < ZONE_MIN_HT_BP:
            continue
        band = ZONE_BAND * ht
        lo_tests = int((l[s0:i] <= lo + band).sum())
        hi_tests = int((h[s0:i] >= hi - band).sum())
        if lo_tests < 2 or hi_tests < 2:
            continue
        # long: wick into the support band (limited break of the extreme),
        # close back above it; target the near edge of the resistance band
        if (l[i] <= lo + band and l[i] >= lo - ZONE_WICK * ht
                and c[i] > lo + band):
            out.append((i, 1, float(lo - ZONE_WICK * ht), float(hi - band)))
            last_fire = i
        elif (h[i] >= hi - band and h[i] <= hi + ZONE_WICK * ht
                and c[i] < hi - band):
            out.append((i, -1, float(hi + ZONE_WICK * ht), float(lo + band)))
            last_fire = i
    return out


DETECTORS = {"trendline": _sig_trendline, "breakout": _sig_breakout,
             "zone": _sig_zone}


# ------------------------------------------------------------------- build

def build_symbol(sym: str) -> pd.DataFrame:
    raw = load_bars(sym)
    if raw is None:
        print(f"  {sym}: no bars — run fetch")
        return pd.DataFrame()
    df = prep(raw)
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    day = df["day"].to_numpy()
    rows = []
    for name, fn in DETECTORS.items():
        for i, side, stop, tp in fn(df):
            entry = float(c[i])
            r = abs(entry - stop)
            if r <= 0 or (r / entry) * 1e4 < MIN_R_BP:
                continue
            tgt = float(tp) if tp is not None else entry + side * TP_R * r
            if side * (tgt - entry) <= 0:        # zone entry past its target
                continue
            res, j, why = run_exit(h, l, c, day, i, side, entry, stop, tgt)
            rows.append({
                "symbol": sym, "strategy": name, "ts": df["ts"].iat[i],
                "date": str(pd.Timestamp(day[i]).date()), "i": i,
                "side": side, "entry": entry, "stop": stop, "target": tgt,
                "r_bp": (r / entry) * 1e4,
                "tp_r": side * (tgt - entry) / r,
                "gross_bp": (res / entry) * 1e4,
                "exit_i": j, "exit_ts": df["ts"].iat[j],
                "exit_px": entry + side * res,
                "exit_reason": why, "held": j - i,
            })
    return pd.DataFrame(rows)


def stage_build(args) -> None:
    frames = []
    for sym in (args.symbols.split(",") if args.symbols else UNIVERSE):
        d = build_symbol(sym)
        if len(d):
            print(f"{sym}: {len(d):,} setups  "
                  + " ".join(f"{k}={v}" for k, v in
                             d["strategy"].value_counts().sort_index().items()),
                  flush=True)
            frames.append(d)
    if not frames:
        raise SystemExit("no setups built")
    out = pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"])
    out["net_bp"] = out["gross_bp"] - COST_BP
    out = out.reset_index(drop=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_parquet(CACHE / "setups.parquet")
    print(f"\nwrote {len(out):,} setups -> setups.parquet "
          f"({out['date'].min()}..{out['date'].max()})")


# -------------------------------------------------------------------- null

_PREP_CACHE: dict[str, pd.DataFrame] = {}


def _prep_cached(sym: str) -> pd.DataFrame:
    if sym not in _PREP_CACHE:
        _PREP_CACHE[sym] = prep(load_bars(sym))
    return _PREP_CACHE[sym]


def random_null(sym: str, n_trades: int, r_bp: float, tp_r: float,
                rng: np.random.Generator, runs: int = N_NULL) -> np.ndarray:
    """Mean net bp for `runs` random-entry portfolios through the strategy's
    own bracket geometry (its mean R and target multiple). Draw one pool,
    bootstrap portfolios of size n from it — the statistic a strategy's mean
    has to clear is the sampling variance of a portfolio that size."""
    df = _prep_cached(sym)
    h, l, c = df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy()
    day = df["day"].to_numpy()
    mins = df["min"].to_numpy()
    ok = np.flatnonzero((mins >= ENTRY_LO) & (mins <= ENTRY_HI))
    idx = rng.choice(ok[(ok > 2) & (ok < len(c) - 2)], size=NULL_POOL)
    sides = rng.choice([1, -1], size=NULL_POOL)
    pool = np.empty(NULL_POOL)
    for k, (i, s) in enumerate(zip(idx, sides)):
        entry = c[i]
        r = entry * r_bp / 1e4
        res, _, _ = run_exit(h, l, c, day, int(i), int(s), entry,
                             entry - s * r, entry + s * tp_r * r)
        pool[k] = (res / entry) * 1e4 - COST_BP
    draw = rng.integers(0, NULL_POOL, size=(runs, min(n_trades, NULL_POOL)))
    return pool[draw].mean(axis=1)


# ------------------------------------------------------------------ report

def stage_report(args) -> None:
    s = pd.read_parquet(CACHE / "setups.parquet")
    rng = np.random.default_rng(20260814)
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    n_sess = s.groupby("symbol")["date"].nunique().median()
    emit(f"# The stop-loss meme, mechanised — {STAMP}")
    emit()
    emit(f"{len(s):,} setups, {s['symbol'].nunique()} symbols, "
         f"{s['date'].min()}..{s['date'].max()} (~{int(n_sess)} sessions/"
         f"symbol), {TF}m bars, {COST_BP:.0f}bp round-trip costs. Stops are "
         "the meme's: AT the trendline / broken level, just past the zone "
         "extreme. trendline/breakout run the canon 2R target; zone runs "
         "zone-to-zone. Data is the keyless 60-day Yahoo window — read "
         "the t-stats with that N in mind; the harness re-runs on the "
         "chart-llm-gate SIP year when its cache is present.")
    emit()
    emit("## By strategy (pooled across symbols)")
    emit()
    emit("| strategy | trades | win% | mean net bp | t | median R bp | "
         "target% | stop% | null pctile |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for name in STRATEGIES:
        g = s[s["strategy"] == name]
        if g.empty:
            continue
        net = g["net_bp"].to_numpy()
        nulls = []
        for sym, gs in g.groupby("symbol"):
            nulls.append(random_null(sym, len(gs), float(gs["r_bp"].mean()),
                                     float(gs["tp_r"].mean()), rng) * len(gs))
        pooled = np.sum(nulls, axis=0) / len(g)
        pct = float((pooled < net.mean()).mean() * 100)
        rc = g["exit_reason"].value_counts(normalize=True) * 100
        emit(f"| {name} | {len(g):,} | {(net > 0).mean() * 100:.1f} | "
             f"{net.mean():+.2f} | {tstat(net):+.2f} | "
             f"{g['r_bp'].median():.0f} | {rc.get('target', 0):.0f} | "
             f"{rc.get('stop', 0):.0f} | {pct:.1f} |")
    emit()
    emit("`null pctile` = share of 200 random-entry portfolios (same symbol "
         "mix, same bracket geometry) below the strategy's mean. 50 means "
         "the pattern adds nothing over entering at random.")
    emit()
    emit("## By symbol (mean net bp, trade count)")
    emit()
    emit("| symbol | " + " | ".join(STRATEGIES) + " |")
    emit("|---" * (len(STRATEGIES) + 1) + "|")
    piv = s.pivot_table(index="symbol", columns="strategy", values="net_bp",
                        aggfunc=["mean", "count"])
    for sym in sorted(s["symbol"].unique()):
        cells = []
        for st in STRATEGIES:
            try:
                m, n = piv[("mean", st)][sym], piv[("count", st)][sym]
                cells.append(f"{m:+.1f} ({int(n)})" if pd.notna(m) else "—")
            except KeyError:
                cells.append("—")
        emit(f"| {sym} | " + " | ".join(cells) + " |")
    emit()
    emit("## Cost sensitivity (mean net bp per trade)")
    emit()
    emit("| strategy | 0bp | 2bp | 5bp | 10bp |")
    emit("|---|---|---|---|---|")
    for name in STRATEGIES:
        g = s[s["strategy"] == name]["gross_bp"]
        if g.empty:
            continue
        emit(f"| {name} | " + " | ".join(f"{g.mean() - k:+.2f}"
                                         for k in (0, 2, 5, 10)) + " |")
    emit()
    emit("Read against the prior five (mechanical_20260807): orb -2.92, "
         "vwap -1.41, ema -1.89, sweep -1.46, flag +0.64 net bp on a year. "
         "The 2bp column is the headline; a pattern that only works at 0bp "
         "pays its edge to the market maker.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"levels_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# -------------------------------------------------------------------- wrap
# The second half of the prompt: can a 0DTE or 1DTE option CARRY these trades?
# No keys, no OPRA prints, so this is a Black-Scholes repricing of each
# SPY/QQQ signal — the same BSM core the app trades with — under loud
# assumptions: ATM strike at entry, IV = the prior close of the 9-day cash
# vol index (VIX9D for SPY, VXN for QQQ), IV FLAT over the hold, exit at the
# moment the underlying bracket resolves, 2c/contract round-trip spread.
# Flat-IV flatters the long: it charges theta but never the post-open vol
# crush. Real prints exist behind the Alpaca keys via the 0dte-vrp fetch
# machinery; that is the upgrade, not more modelling.

SPREAD_RT = 0.02        # $/share round trip; ATM 0-1DTE SPY/QQQ is 1-2c wide
IV_FLOOR = 0.08


def _iv_for(sym: str, dates: pd.Series) -> pd.Series:
    ivf = pd.read_parquet(CACHE / "iv_proxy.parquet")
    col = "vix9d" if sym == "SPY" else "vxn"
    ser = ivf[col].dropna()
    # prior session's close, so the mark exists at entry time
    prior = ser.shift(1)
    out = dates.map(prior).astype(float) / 100.0
    return out.fillna(ser.mean() / 100.0).clip(lower=IV_FLOOR)


def _tau_years(ts: pd.Timestamp, expiry_close: pd.Timestamp) -> float:
    """Trading-hours tau: whole 6.5h sessions between, plus the remainder of
    the entry day. Good to the granularity a 5m study needs."""
    hours = 0.0
    d, e = ts.normalize(), expiry_close.normalize()
    day_end = ts.normalize() + timedelta(hours=16)
    hours += max((min(day_end, expiry_close) - ts).total_seconds() / 3600, 0)
    d += timedelta(days=1)
    while d <= e:
        if d.weekday() < 5:
            hours += 6.5
        d += timedelta(days=1)
    return hours / (252 * 6.5)


def stage_wrap(args) -> None:
    from app.services.options_math import bs_price

    s = pd.read_parquet(CACHE / "setups.parquet")
    s = s[s["symbol"].isin(["SPY", "QQQ"])].copy()
    if s.empty:
        raise SystemExit("no SPY/QQQ setups — run build first")
    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The 0/1DTE wrap: BSM-modeled, not printed — {STAMP}")
    emit()
    emit(f"{len(s):,} SPY/QQQ signals from setups.parquet, repriced as long "
         "ATM options: strike = round(S at entry), right = signal direction, "
         "entry/exit at BSM with IV = prior close of VIX9D (SPY) / VXN "
         "(QQQ), IV flat over the hold, exit when the underlying bracket "
         f"resolves, {SPREAD_RT * 100:.0f}c/share round-trip spread. 0DTE "
         "expires at the entry day's close; 1DTE at the next session's. "
         "Flat IV flatters the long (theta is charged, the post-open vol "
         "crush is not), so read negative rows as at-least-this-bad. "
         "Real OPRA minute prints exist behind the Alpaca keys via the "
         "0dte-vrp fetch machinery — that is the upgrade path.")
    emit()
    for sym in ("SPY", "QQQ"):
        g = s[s["symbol"] == sym].copy()
        if g.empty:
            continue
        g["iv"] = _iv_for(sym, g["date"]).to_numpy()
        emit(f"## {sym} — {len(g)} signals, shares baseline "
             f"{g['net_bp'].mean():+.2f} net bp/trade "
             f"(t {tstat(g['net_bp'].to_numpy()):+.2f})")
        emit()
        emit("| dte | strategy | n | mean premium | mean P&L %prem | t | "
             "win% | worst %prem | bp of S |")
        emit("|---|---|---|---|---|---|---|---|---|")
        for dte in (0, 1):
            for name in STRATEGIES:
                gg = g[g["strategy"] == name]
                if len(gg) < 10:
                    continue
                prem_in = np.empty(len(gg))
                pnl = np.empty(len(gg))
                for k, r in enumerate(gg.itertuples()):
                    ts_in = pd.Timestamp(r.ts)
                    ts_out = pd.Timestamp(r.exit_ts)
                    exp_day = ts_in.normalize()
                    if dte == 1:
                        exp_day += timedelta(days=3 if ts_in.weekday() == 4 else 1)
                    exp = exp_day + timedelta(hours=16)
                    K = float(round(r.entry))
                    right = "C" if r.side > 0 else "P"
                    p_in = bs_price(r.entry, K, _tau_years(ts_in, exp),
                                    r.iv, right)
                    p_out = bs_price(r.exit_px, K, _tau_years(ts_out, exp),
                                     r.iv, right)
                    prem_in[k] = p_in
                    pnl[k] = p_out - p_in - SPREAD_RT
                pct = pnl / np.maximum(prem_in, 0.01) * 100
                bp_s = pnl / gg["entry"].to_numpy() * 1e4
                emit(f"| {dte} | {name} | {len(gg)} | ${prem_in.mean():.2f} "
                     f"| {pct.mean():+.1f} | {tstat(pct):+.2f} "
                     f"| {(pct > 0).mean() * 100:.0f} | {pct.min():+.0f} "
                     f"| {bp_s.mean():+.1f} |")
        emit()
    emit("The wrapper is pure amplification: the option's bp-of-S column can "
         "only beat the shares row by luck, because it is the same "
         "underlying path minus theta minus a spread that is ~40-100x the "
         "share spread as a fraction of capital at risk. A 0DTE makes sense "
         "on a signal with edge (it caps risk at the premium and levers a "
         "held-for-minutes conviction); it cannot manufacture edge the "
         "underlying signal does not have.")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"wrap_0dte_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "build", "report", "wrap"])
    ap.add_argument("--symbols", default=None, help="comma list; default all")
    args = ap.parse_args()
    {"fetch": stage_fetch, "build": stage_build,
     "report": stage_report, "wrap": stage_wrap}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
