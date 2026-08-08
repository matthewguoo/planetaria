"""What else was going on: the day, the tape, the index, the options.

WHY THIS IS A SEPARATE ARM AND NOT A BETTER PROMPT. The `outcome` arm shows
the model 48 candles and nothing else. That is a deliberately impoverished
view — a real discretionary trader glances at the index, knows whether the
name gapped, and has some sense of whether the options market is leaning. If
the bare-chart arm comes back null, "an LLM cannot read a chart" and "an LLM
cannot read a chart WITH NO CONTEXT" are different claims and the first does
not follow.

So the context is added as a PAIRED ARM on the identical setups rather than
folded into the existing prompt. That makes it an ablation with a clean
difference — same events, same bracket, same model, same decoding, one block
of text added — instead of an untestable prompt improvement. If context helps,
the difference says so and says by how much.

FOUR BLOCKS, all causal at the decision bar:

  day       Gap from the prior close, the session's range so far, where price
            sits in it, and volume so far against the same-time-of-day norm.
            Everything a trader reads off the top of the screen.
  daily     Ten prior daily candles as percentages of the current price. The
            higher-timeframe bias that every chart-reading school insists on
            and that a 4-hour intraday window cannot show.
  index     SPY's own last twelve 5m candles, aligned to the SAME timestamp
            and anonymised the same way. Is the whole tape moving, or just
            this name?
  options   The PRIOR session's put/call volume ratio and total option volume
            against its 20-day median.

WHAT THE OPTIONS BLOCK IS AND IS NOT, measured against the paper API on
2026-08-07 rather than taken from documentation:

  - OPEN INTEREST IS NOT AVAILABLE ON THIS TIER AT ALL. `open_interest` comes
    back None on live, currently-tradable contracts (checked against 892 live
    NVDA contracts), so this is not a historical-data gap that a wider window
    would fix. There is no OI in this study and there cannot be.
  - VOLUME is fully available historically. Expired contracts cannot be
    ENUMERATED (the trading API lists only live ones and returns 0 for a past
    expiry), but their bars can be FETCHED by constructing the OCC symbol
    directly, and that batches: 24 contracts x 10 sessions in 0.4s.
  - It is the PRIOR session's flow, not the current one. Today's full-day
    option volume includes everything that traded after the decision bar and
    would be straightforward lookahead. Intraday option bars would fix that
    and cost ~100x the data; if the ablation shows the options block earns
    anything, that is the upgrade to buy.
  - It covers the FRONT TWO WEEKLY EXPIRIES within +-18% of spot, which is
    where megacap volume concentrates but is not the whole chain. Read it as
    a near-dated flow proxy, not as total options volume.

Run:
    python scripts/research_chart_context.py options    # ~10 min of API time
    python scripts/research_chart_context.py check
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from collections import defaultdict
from datetime import date, datetime, timedelta

from _paths import BACKEND, CACHE

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_chart_data import END, START, UNIVERSE, bars_path  # noqa: E402
from research_chart_render import WINDOW, _pct  # noqa: E402

OPTIONS = CACHE / "options_flow.parquet"

DAILY_BARS = 10          # prior daily candles shown
INDEX_BARS = 12          # index 5m candles shown (one hour)
INDEX_SYMBOL = "SPY"
STRIKE_SPAN = 0.18       # +-18% of spot
FRONT_EXPIRIES = 2       # weeklies carried per decision date


def _increment(px: float) -> float:
    """Strike ladder spacing. Over-asking is free — a constructed symbol that
    never existed simply returns no bars — so these lean fine rather than
    trying to be exactly right at every price level."""
    if px < 100:
        return 0.5
    if px < 250:
        return 1.0
    if px < 500:
        return 2.5
    return 5.0


def _fridays(lo: date, hi: date) -> list[date]:
    d = lo + timedelta(days=(4 - lo.weekday()) % 7)
    out = []
    while d <= hi:
        out.append(d)
        d += timedelta(days=7)
    return out


def stage_options(args) -> None:
    """Per (symbol, session) call and put volume, from constructed OCC symbols.

    One request per (symbol, expiry): all strikes in the band, daily bars over
    the two weeks the contract is actually liquid. Misses are free.
    """
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    s = get_settings()
    cl = OptionHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    lo, hi = date.fromisoformat(args.start), date.fromisoformat(args.end)
    rows: list[dict] = []
    syms = args.symbols.split(",") if args.symbols else UNIVERSE

    for sym in syms:
        px = pd.read_parquet(bars_path(sym))["c"]
        got = 0
        for exp in _fridays(lo, hi):
            # The window a weekly is actually traded in. Two weeks back is
            # generous for a weekly and cheap, since unlisted strikes cost
            # nothing.
            w0, w1 = exp - timedelta(days=14), exp
            seg = px[(px.index.date >= w0) & (px.index.date <= w1)]
            if seg.empty:
                continue
            mid = float(seg.median())
            inc = _increment(mid)
            k0 = np.floor(mid * (1 - STRIKE_SPAN) / inc) * inc
            k1 = np.ceil(mid * (1 + STRIKE_SPAN) / inc) * inc
            strikes = np.arange(k0, k1 + inc, inc)
            occ = [f"{sym}{exp:%y%m%d}{cp}{int(round(k * 1000)):08d}"
                   for k in strikes for cp in "CP"]
            try:
                res = cl.get_option_bars(OptionBarsRequest(
                    symbol_or_symbols=occ, timeframe=TimeFrame.Day,
                    start=datetime.combine(w0, datetime.min.time()),
                    end=datetime.combine(w1, datetime.max.time())))
            except Exception as exc:                         # noqa: BLE001
                print(f"  {sym} {exp}: {type(exc).__name__} {str(exc)[:60]}")
                continue
            agg: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
            for o, bars in res.data.items():
                # OCC: root + yymmdd + C/P + strike*1000. The type char sits
                # immediately after the 6-digit date, counted from the END so
                # a 1-5 character root does not shift it.
                j = 0 if o[-9] == "C" else 1
                for b in bars:
                    agg[b.timestamp.date()][j] += float(b.volume)
            for d, (c, p) in agg.items():
                rows.append({"symbol": sym, "date": str(d), "expiry": str(exp),
                             "call_vol": c, "put_vol": p})
            got += len(res.data)
            _time.sleep(0.05)
        print(f"{sym}: {got:,} contract-series across "
              f"{len(_fridays(lo, hi))} expiries", flush=True)

    if not rows:
        raise SystemExit("no option volume returned")
    df = pd.DataFrame(rows)
    # A session sees several live expiries; the study wants the front two, so
    # rank by expiry within each session and keep the nearest.
    df["expiry_rank"] = (df.sort_values("expiry")
                         .groupby(["symbol", "date"])["expiry"]
                         .rank(method="dense").astype(int))
    df = df[df["expiry_rank"] <= FRONT_EXPIRIES]
    out = (df.groupby(["symbol", "date"])[["call_vol", "put_vol"]].sum()
           .reset_index())
    out["opt_vol"] = out["call_vol"] + out["put_vol"]
    out["pc_ratio"] = out["put_vol"] / out["call_vol"].replace(0, np.nan)
    out = out.sort_values(["symbol", "date"])
    out["opt_vol_med20"] = (out.groupby("symbol")["opt_vol"]
                            .transform(lambda s: s.rolling(20, min_periods=5)
                                       .median()))
    out["opt_vol_rel"] = out["opt_vol"] / out["opt_vol_med20"]
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OPTIONS)
    print(f"\nwrote {len(out):,} symbol-sessions -> {OPTIONS.name}")
    print(out.groupby("symbol")["pc_ratio"].describe()[["count", "50%"]]
          .to_string())


# ------------------------------------------------------------------ blocks

def _daily_frame(df1m: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLC from the 1m tape, RTH only."""
    from research_chart_data import rth_mask

    d = df1m[rth_mask(df1m.index)]
    return (d.resample("1D").agg({"o": "first", "h": "max", "l": "min",
                                  "c": "last", "v": "sum"}).dropna())


def premarket(df1m: pd.DataFrame) -> pd.DataFrame:
    """Per-session premarket high/low, 04:00-09:30 ET.

    Available only because `research_chart_data` fetches extended hours and
    `prep` discards them at RESAMPLE time rather than at fetch time. Two of
    the five theses want this and neither can be served from the RTH frame:
    the opening range is read against where the premarket left price, and a
    sweep hunts the liquidity resting above the premarket high.
    """
    m = df1m.index.hour * 60 + df1m.index.minute
    pre = df1m[(m >= 240) & (m < 570) & (df1m.index.weekday < 5)]
    g = pre.groupby(pre.index.normalize())
    return pd.DataFrame({"pm_h": g["h"].max(), "pm_l": g["l"].min(),
                         "pm_v": g["v"].sum()})


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Fields the canon needs that `prep` does not compute.

    DELIBERATELY NOT ADDED TO `prep`. The setups panel and the `outcome` arm
    are built off that function, and a study should not have to re-derive
    20,550 setups — or invalidate verdicts already on disk — to add a line of
    context to a prompt. Everything here is a pure function of the prep frame
    and is computed at read time.

    VWAP bands are the volume-weighted standard deviation of typical price
    about the anchored VWAP, accumulated within the session:
        var = E_v[tp^2] - vwap^2
    which is the textbook one-pass form. They are here because a VWAP trade
    without bands is only half the setup as it is taught — the reclaim means
    something different at -0.2σ than at -2.5σ.
    """
    d = df.copy()
    tp = (d["h"] + d["l"] + d["c"]) / 3
    day = d["day"]
    cv = d.groupby(day)["v"].cumsum()
    cvt2 = (d["v"] * tp ** 2).groupby(day).cumsum()
    var = (cvt2 / cv.replace(0, np.nan)) - d["vwap"] ** 2
    d["vwap_sd"] = np.sqrt(var.clip(lower=0))

    # Wilder ATR(14) on the study's own timeframe, reset nowhere — a gap is
    # part of true range and pretending otherwise understates overnight risk.
    pc = d["c"].shift(1)
    tr = pd.concat([d["h"] - d["l"], (d["h"] - pc).abs(),
                    (d["l"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return d


def thesis_block(row: pd.Series, df: pd.DataFrame, pm: pd.DataFrame,
                 daily: pd.DataFrame) -> str:
    """The inputs THIS setup's own thesis is stated in.

    A generic context block is the wrong shape for this study. Each of the
    five setups is an argument, and each argument has its own premises: an
    opening-range break is a claim about where the day's initial balance sat
    relative to the premarket and to yesterday; a VWAP reclaim is a claim
    about distance from the institutional average, which is meaningless
    without the bands; a sweep is a claim about which resting liquidity just
    got taken. Handing all five the same four paragraphs would test whether
    the model can read a generic dashboard, not whether it can evaluate the
    trade in front of it.

    So each strategy gets the premises its own canon states out loud, and
    nothing is invented that a chart-reader would not have on screen.
    """
    i = int(row["i"])
    b = df.iloc[i]
    anchor = float(b["c"])
    day_i = df["day"].iat[i]
    st = row["strategy"]
    out: list[str] = []

    pmr = pm.loc[day_i] if day_i in pm.index else None
    prior = daily[daily.index < day_i]
    pd_row = prior.iloc[-1] if len(prior) else None

    def p(x):
        return _pct(x, anchor)

    if st == "orb":
        bits = [f"opening range {p(b['orl']):+.2f}% to {p(b['orh']):+.2f}% "
                f"(width {(b['orh'] / b['orl'] - 1) * 100:.2f}%)"]
        if pmr is not None:
            bits.append(f"premarket range {p(pmr['pm_l']):+.2f}% to "
                        f"{p(pmr['pm_h']):+.2f}%")
        if pd_row is not None:
            inside = (b["orh"] <= pd_row["h"]) and (b["orl"] >= pd_row["l"])
            bits.append(f"prior session range {p(pd_row['l']):+.2f}% to "
                        f"{p(pd_row['h']):+.2f}% — the opening range sits "
                        f"{'INSIDE' if inside else 'OUTSIDE'} it")
        out.append("OPENING RANGE: " + "; ".join(bits))

    elif st == "vwap":
        sd = float(b["vwap_sd"]) or np.nan
        z = (anchor - b["vwap"]) / sd if sd and not np.isnan(sd) else np.nan
        bands = "; ".join(
            f"{k:+g}sd {p(b['vwap'] + k * sd):+.2f}%" for k in (-2, -1, 1, 2)
        ) if sd and not np.isnan(sd) else "bands unavailable"
        out.append(f"VWAP: {p(b['vwap']):+.2f}%, price is "
                   f"{z:+.2f} standard deviations from it; {bands}")

    elif st == "ema":
        sep = (b["ema9"] / b["ema21"] - 1) * 100
        prev = df.iloc[max(0, i - 6)]
        slope = (b["ema21"] / prev["ema21"] - 1) * 100
        out.append(f"MOVING AVERAGES: 9-EMA {p(b['ema9']):+.2f}%, 21-EMA "
                   f"{p(b['ema21']):+.2f}%, separation {sep:+.2f}%; the "
                   f"21-EMA has moved {slope:+.2f}% over the last 6 bars")

    elif st == "sweep":
        bits = [f"prior session high {p(b['pdh']):+.2f}%, low "
                f"{p(b['pdl']):+.2f}%"]
        if pmr is not None:
            bits.append(f"premarket high {p(pmr['pm_h']):+.2f}%, low "
                        f"{p(pmr['pm_l']):+.2f}%")
        bits.append(f"the flagged bar's own range was {p(b['l']):+.2f}% to "
                    f"{p(b['h']):+.2f}% on {b['v'] / df['v'].iloc[max(0, i - 20):i].median():.1f}x "
                    f"the recent median volume")
        out.append("LIQUIDITY LEVELS: " + "; ".join(bits))

    elif st == "flag":
        lo = max(0, i - 14)
        seg = df.iloc[lo:i + 1]
        imp = (seg["c"].iat[-1] / seg["c"].iat[0] - 1) * 100
        rng = (seg["h"].max() / seg["l"].min() - 1) * 100
        recent = df.iloc[max(0, i - 5):i]
        out.append(f"IMPULSE AND CONSOLIDATION: price moved {imp:+.2f}% over "
                   f"the last {len(seg) - 1} bars within a total range of "
                   f"{rng:.2f}%; the 5 bars before the breakout held "
                   f"{p(recent['l'].min()):+.2f}% to {p(recent['h'].max()):+.2f}%")

    # ATR is universal: it is what says whether the proposed stop is wide or
    # tight for THIS tape, and every school teaches sizing against it.
    atr_pct = float(b["atr"]) / anchor * 100
    stop_atr = abs(anchor - float(row["stop"])) / float(b["atr"]) \
        if float(b["atr"]) > 0 else np.nan
    out.append(f"VOLATILITY: ATR(14) on this timeframe is {atr_pct:.2f}%; "
               f"the proposed stop is {stop_atr:.1f} ATR away")
    return "\n\n".join(out)


def context_block(row: pd.Series, df: pd.DataFrame, daily: pd.DataFrame,
                  index_df: pd.DataFrame, opts: pd.DataFrame | None) -> str:
    """The four blocks, all anonymised against the decision bar's close.

    NO-LOOKAHEAD, and each block earns that differently:
      - `day` is computed from `df.iloc[:i+1]` restricted to the current
        session, so it can only see bars already shown.
      - `daily` is strictly PRIOR sessions — the current one is excluded
        because its high, low and close are not known at the decision bar.
      - `index` is sliced by TIMESTAMP, not by position, so a symbol with a
        missing bar cannot pull in an index bar from the future.
      - `options` is the prior session's row.
    """
    i = int(row["i"])
    anchor = float(df["c"].iat[i])
    ts = df["ts"].iat[i]
    day_i = df["day"].iat[i]
    out: list[str] = []

    # --- day -----------------------------------------------------------
    today = df.iloc[:i + 1]
    today = today[today["day"] == day_i]
    # Both sides stay tz-aware ET. `_daily_frame` resamples an ET-localised
    # 1m index, so its index carries the zone; normalising `day_i` away would
    # silently shift the boundary by the UTC offset and let a prior session
    # leak into or out of the comparison.
    prior = daily[daily.index < day_i]
    bits = [f"session so far: high {_pct(today['h'].max(), anchor):+.2f}%, "
            f"low {_pct(today['l'].min(), anchor):+.2f}%, "
            f"open {_pct(today['o'].iat[0], anchor):+.2f}%"]
    if len(prior):
        pc = float(prior["c"].iat[-1])
        bits.append(f"prior close {_pct(pc, anchor):+.2f}% "
                    f"(gap at the open "
                    f"{(today['o'].iat[0] / pc - 1) * 100:+.2f}%)")
        # Volume so far against the same number of bars on prior days, so an
        # 09:45 comparison is against other 09:45s and not against full days.
        n = len(today)
        norm = [g["v"].iloc[:n].sum() for _, g in
                df[df["day"].isin(prior.index[-10:])].groupby("day")]
        if norm:
            bits.append(f"volume so far {today['v'].sum() / np.median(norm):.1f}x "
                        f"the 10-day norm for this time of day")
    out.append("SESSION: " + "; ".join(bits))

    # --- daily ---------------------------------------------------------
    if len(prior) >= 2:
        rows = ["DAILY (prior sessions, most recent last):",
                "  DAY    OPEN    HIGH     LOW   CLOSE"]
        for k, (_, b) in enumerate(prior.tail(DAILY_BARS).iterrows()):
            rows.append(f"  {k - min(len(prior), DAILY_BARS):>3}  "
                        f"{_pct(b['o'], anchor):>+6.2f}  "
                        f"{_pct(b['h'], anchor):>+6.2f}  "
                        f"{_pct(b['l'], anchor):>+6.2f}  "
                        f"{_pct(b['c'], anchor):>+6.2f}")
        out.append("\n".join(rows))

    # --- index ---------------------------------------------------------
    if row["symbol"] != INDEX_SYMBOL and index_df is not None:
        ix = index_df[index_df["ts"] <= ts].tail(INDEX_BARS)
        if len(ix) >= 4:
            ia = float(ix["c"].iat[-1])
            rows = [f"INDEX (broad market, same {INDEX_BARS * 5} minutes, "
                    f"relative to its own last close):",
                    "  BAR   CLOSE"]
            for k, (_, b) in enumerate(ix.iterrows()):
                rows.append(f"  {k - len(ix) + 1:>3}  "
                            f"{_pct(b['c'], ia):>+6.2f}")
            out.append("\n".join(rows))

    # --- options -------------------------------------------------------
    if opts is not None and len(opts):
        p = opts[opts["date"] < row["date"]]
        if len(p):
            r = p.iloc[-1]
            rel = (f", total near-dated option volume "
                   f"{r['opt_vol_rel']:.1f}x its 20-day median"
                   if pd.notna(r.get("opt_vol_rel")) else "")
            out.append(f"OPTIONS (prior session): put/call volume ratio "
                       f"{r['pc_ratio']:.2f}{rel}")
    return "\n\n".join(out)


def load_context(symbols: list[str]) -> dict:
    """Everything both blocks need, loaded once per run.

    `frames` here are ENRICHED prep frames (VWAP bands, ATR). Callers that
    render context must use these rather than a bare `prep` frame, or the
    thesis block raises on the missing columns.
    """
    from research_chart_setups import TF, prep

    opts = pd.read_parquet(OPTIONS) if OPTIONS.exists() else None
    daily, pm, frames = {}, {}, {}
    for s in set(symbols) | {INDEX_SYMBOL}:
        raw = pd.read_parquet(bars_path(s))
        daily[s] = _daily_frame(raw)
        pm[s] = premarket(raw)
        frames[s] = enrich(prep(raw, TF))
    return {
        "daily": daily, "premarket": pm, "frames": frames,
        "index": frames[INDEX_SYMBOL],
        "options": ({s: g.sort_values("date")
                     for s, g in opts.groupby("symbol")} if opts is not None
                    else {}),
    }


def full_context(row: pd.Series, ctx: dict) -> str:
    """Thesis premises first, then the surrounding conditions.

    Order is deliberate: the setup-specific block is what the trade is
    actually arguing, and the session / daily / index / options blocks are
    the conditions it is arguing under. A model that reads only the first
    paragraph still gets the thing that matters most.
    """
    sym = row["symbol"]
    df = ctx["frames"][sym]
    return "\n\n".join([
        thesis_block(row, df, ctx["premarket"][sym], ctx["daily"][sym]),
        context_block(row, df, ctx["daily"][sym], ctx["index"],
                      ctx["options"].get(sym)),
    ])


def stage_check(args) -> None:
    """One rendered context per strategy, plus the token budget.

    The budget check is not decoration. Each llama-server slot gets
    ctx_size/parallel tokens and must hold the prompt AND the generation; the
    bare `outcome` prompt already measured a 1,951-token median against a
    3,072 budget, so a context block that doubles it silently truncates the
    tail of the panel rather than failing loudly.
    """
    from research_chart_setups import SETUPS

    s = pd.read_parquet(SETUPS)
    s = s[s["i"] >= WINDOW]
    ctx = load_context(list(s["symbol"].unique()))
    if args.strategy:
        s = s[s["strategy"] == args.strategy]
    sizes = []
    for st, g in s.groupby("strategy"):
        row = g.iloc[len(g) // 2]
        block = full_context(row, ctx)
        sizes.append((st, len(block)))
        if args.strategy or st == (args.show or "orb"):
            print(f"=== {st}: {row['symbol']} {row['ts']} "
                  f"(both hidden from the model) ===\n")
            print(block)
            print()
    print("--- context block size by strategy (chars / ~tokens) ---")
    for st, n in sizes:
        print(f"  {st:<6} {n:>5} chars  ~{n // 4:>4} tokens")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["options", "check"])
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--strategy", default=None, help="render just this one")
    ap.add_argument("--show", default=None, help="which strategy to print")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    args = ap.parse_args()
    {"options": stage_options, "check": stage_check}[args.stage](args)


if __name__ == "__main__":
    main()
