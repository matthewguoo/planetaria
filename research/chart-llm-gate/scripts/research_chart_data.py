"""1-minute SIP bars for the megacap universe, cached one parquet per symbol.

WHY SIP AND NOT IEX. IEX prints a few percent of consolidated volume and goes
silent outside 08:00-17:00 ET. Every setup in this study keys off session
structure — the opening range, VWAP anchored at 09:30, the prior day's high —
and all three are wrong if the tape has holes in it. The paper account has SIP
entitlement for HISTORICAL bars (this is the same feed the PEAD study's
`_bars_for` uses); it does not have real-time SIP, which is a live-trading
constraint and not a backtest one.

WHY EXTENDED HOURS ARE FETCHED BUT NOT TRADED. The overnight and premarket
sessions are needed to compute two of the five setups — the liquidity sweep
references the overnight range, and the opening range's context includes the
premarket drift — but no setup ENTERS outside 09:30-16:00 ET. `rth_mask` is
the gate and it is applied at signal time, not at fetch time.

Alpaca paginates a year of 1m bars into many pages and the SDK walks them for
us; a single symbol-year is ~100k bars and takes 20-60s. The cache is keyed by
(symbol, start, end) so a widened window is a new file rather than a silent
partial read.

Run:
    python scripts/research_chart_data.py fetch
    python scripts/research_chart_data.py check
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from _paths import BACKEND, BARS

sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ET = ZoneInfo("America/New_York")

# The universe. Ten liquid tech megacaps plus the two index ETFs every retail
# daytrader actually trades. SPY and QQQ are here as CONTROLS, not as tech
# names: if a chart-reading edge exists it should not care which instrument
# the candles came from, and an effect that shows up only on single names
# (which have idiosyncratic news) but not on the indices is a different claim
# than the one this study is testing.
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META",
            "TSLA", "AMD", "AVGO", "NFLX", "SPY", "QQQ"]

START = "2025-08-01"
END = "2026-08-01"

# Round-trip cost in basis points of notional, applied to every trade.
#
# EXPRESSED IN BP, NOT $/SHARE, deliberately. The archived ICT study used
# $/share (SPY 0.02, TSLA 0.05), which is fine for four symbols of similar
# price but silently rescales the hurdle across a universe spanning ~$180
# (AMD) to ~$900 (META): two cents is 1.1bp on AMD and 0.2bp on META, so a
# $/share table would be testing a four-times-harsher cost on the cheap names.
#
# 2bp round trip is one tick-ish spread each way on a megacap plus slippage.
# It is an ASSUMPTION, and `research_chart_score.py` sweeps it (0/2/5/10bp)
# rather than asking anyone to trust this number.
COST_BP = 2.0

RTH_OPEN = 9 * 60 + 30      # 570
RTH_CLOSE = 16 * 60         # 960


def bars_path(symbol: str, start: str = START, end: str = END):
    return BARS / f"{symbol}_{start}_{end}_1m.parquet"


def fetch_bars(symbol: str, start: str = START, end: str = END,
               refresh: bool = False) -> pd.DataFrame:
    """1m SIP bars, ET-localised, cached. Columns o/h/l/c/v indexed by ts."""
    BARS.mkdir(parents=True, exist_ok=True)
    cache = bars_path(symbol, start, end)
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)

    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from app.config import get_settings

    s = get_settings()
    client = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    print(f"  fetching {symbol} 1m {start}..{end} (SIP, extended hours)…",
          flush=True)
    t0 = _time.monotonic()
    out = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
        start=datetime.fromisoformat(start).replace(tzinfo=ET),
        end=datetime.fromisoformat(end).replace(tzinfo=ET),
        feed="sip", limit=None))
    rows = [{"ts": b.timestamp, "o": float(b.open), "h": float(b.high),
             "l": float(b.low), "c": float(b.close), "v": float(b.volume)}
            for b in out.data.get(symbol, [])]
    if not rows:
        raise SystemExit(f"{symbol}: no bars returned for {start}..{end}")
    df = pd.DataFrame(rows).set_index("ts")
    df.index = pd.DatetimeIndex(df.index).tz_convert(ET)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df.to_parquet(cache)
    print(f"    {len(df):,} bars in {_time.monotonic() - t0:.0f}s", flush=True)
    return df


def resample(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1m -> Nm. label/closed='left' so a bar is stamped with its OPEN time,
    which is what every intraday convention assumes and what `rth_mask` and
    the minutes-since-open clock in the renderer both key on."""
    if minutes == 1:
        return df1m
    return (df1m.resample(f"{minutes}min", label="left", closed="left")
            .agg({"o": "first", "h": "max", "l": "min", "c": "last",
                  "v": "sum"})
            .dropna())


def rth_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """09:30 <= t < 16:00 ET on a weekday.

    Holidays need no special case: the tape simply has no bars on them, so
    they drop out of any frame this masks. Half-days DO survive with a
    truncated afternoon, which is correct — a 13:00 close is a real session.
    """
    mins = index.hour * 60 + index.minute
    return np.asarray((index.weekday < 5) & (mins >= RTH_OPEN)
                      & (mins < RTH_CLOSE))


def session_minutes(index: pd.DatetimeIndex) -> np.ndarray:
    """Minutes since 09:30 ET. Negative premarket, >390 after the close.

    This is the ONLY clock the LLM ever sees. It is a within-day quantity and
    carries no date, which is what lets the renderer show session structure
    (an opening-range setup is meaningless without knowing it is the open)
    while still giving the model nothing to look an outcome up by.
    """
    return np.asarray(index.hour * 60 + index.minute - RTH_OPEN)


# ------------------------------------------------------------------ stages

def stage_fetch(args) -> None:
    syms = args.symbols.split(",") if args.symbols else UNIVERSE
    for sym in syms:
        try:
            df = fetch_bars(sym, args.start, args.end, args.refresh)
        except Exception as exc:                             # noqa: BLE001
            print(f"  {sym}: FAILED {type(exc).__name__} {exc}")
            continue
        rth = df[rth_mask(df.index)]
        print(f"{sym}: {len(df):,} bars, {len(rth):,} RTH, "
              f"{rth.index.normalize().nunique()} sessions "
              f"[{df.index.min():%Y-%m-%d}..{df.index.max():%Y-%m-%d}]")
        _time.sleep(0.3)


def stage_check(args) -> None:
    """Coverage audit. A session with far fewer than 390 RTH 1m bars means a
    gappy tape, and gaps break VWAP and the opening range silently."""
    rows = []
    for sym in (args.symbols.split(",") if args.symbols else UNIVERSE):
        p = bars_path(sym, args.start, args.end)
        if not p.exists():
            rows.append({"symbol": sym, "status": "MISSING"})
            continue
        df = pd.read_parquet(p)
        rth = df[rth_mask(df.index)]
        per_day = rth.groupby(rth.index.date).size()
        # Half-days (~210 bars) are legitimate; anything under 180 is a hole.
        thin = int((per_day < 180).sum())
        rows.append({
            "symbol": sym, "status": "ok", "bars": len(df),
            "rth_bars": len(rth), "sessions": len(per_day),
            "median_bars_per_session": int(per_day.median()),
            "thin_sessions": thin,
            "first": str(df.index.min().date()),
            "last": str(df.index.max().date()),
        })
    out = pd.DataFrame(rows)
    print(out.to_string(index=False))
    bad = out[out.get("thin_sessions", pd.Series(dtype=int)).fillna(0) > 5]
    if len(bad):
        print(f"\nWARNING: {len(bad)} symbols have >5 thin sessions; "
              f"VWAP and opening-range setups on those days are unreliable.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "check"])
    ap.add_argument("--symbols", default=None, help="comma list; default all")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    {"fetch": stage_fetch, "check": stage_check}[args.stage](args)


if __name__ == "__main__":
    main()
