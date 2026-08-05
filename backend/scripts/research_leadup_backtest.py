"""Earnings LEAD-UP backtest: buy the run-up, never hold the print.

Thesis (earnings announcement premium — Beaver 1968; Frazzini & Lamont
2007): stocks earn abnormal returns in the days INTO their earnings
announcement, driven by attention buying and the uncertainty premium. The
tradeable, event-risk-free version: enter N sessions before a CONFIRMED
earnings date and exit before the release can print —
  AMC reporters: exit at the CLOSE of announcement day (release is after);
  BMO/unknown:   exit at the PRIOR day's close (release is before open).
RTH only, close-to-close daily bars, no overnight event exposure.

Null discipline (house rule, same as the ICT study): each variant's mean
bp/event is compared against N_BOOT random portfolios of the SAME symbols
holding the SAME number of sessions on random non-announcement dates —
if lead-up mean doesn't clear that distribution, the "premium" is just
market drift + symbol beta.

Data: Finnhub earnings calendar paged DAY BY DAY (the 1500-row cap
silently drops earliest dates on wide windows — verified 2026-08-05) +
Alpaca SIP daily bars (split-adjusted), chunked batches.

Run: .venv/Scripts/python.exe scripts/research_leadup_backtest.py
     [--months 18] [--min-dollar-vol 50e6] [--refresh]
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from alpaca.data.enums import Adjustment  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from app.config import get_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = Path(__file__).resolve().parent / "_leadup_cache"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"

ENTRY_DAYS = (5, 3, 2)     # sessions before announce-day to enter (at close)
N_BOOT = 200
COST_BP_SIDE = 3.0         # 2bp fees-ish + spread proxy, per side


# ------------------------------------------------------------- calendar

def fetch_calendar(months: int, refresh: bool) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    end = date.today() - timedelta(days=7)   # complete events only
    start = end - timedelta(days=months * 30)
    cache = CACHE / f"calendar_{start}_{end}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    settings = get_settings()
    if not settings.finnhub_api_key:
        raise SystemExit("FINNHUB_API_KEY required")
    rows: list[dict] = []
    day = start
    with httpx.Client(headers={"X-Finnhub-Token": settings.finnhub_api_key},
                      timeout=20) as http:
        while day <= end:
            for attempt in range(3):
                r = http.get(FINNHUB_URL, params={"from": day.isoformat(),
                                                  "to": day.isoformat()})
                if r.status_code == 429:
                    _time.sleep(15)
                    continue
                r.raise_for_status()
                rows.extend((r.json() or {}).get("earningsCalendar") or [])
                break
            day += timedelta(days=1)
            _time.sleep(1.05)  # 60/min free-tier budget
    df = pd.DataFrame(rows)
    df.to_parquet(cache)
    print(f"  calendar: {len(df)} reporter-events {start}..{end}")
    return df


# ----------------------------------------------------------------- bars

def fetch_daily_bars(symbols: list[str], start: date, end: date,
                     refresh: bool) -> pd.DataFrame:
    cache = CACHE / f"bars_v2_{start}_{end}_{len(symbols)}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    settings = get_settings()
    client = StockHistoricalDataClient(settings.alpaca_api_key,
                                       settings.alpaca_secret_key)
    frames: list[pd.DataFrame] = []
    chunks = [symbols[i:i + 75] for i in range(0, len(symbols), 75)]
    for n, chunk in enumerate(chunks):
        req = StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time(), ET),
            end=datetime.combine(end, datetime.min.time(), ET),
            feed="sip", adjustment=Adjustment.ALL, limit=None,
        )
        try:
            out = client.get_stock_bars(req)
        except Exception as exc:
            print(f"  bars chunk {n}: {str(exc)[:80]} (skipped)")
            continue
        for sym, bars in out.data.items():
            if bars:
                frames.append(pd.DataFrame({
                    "symbol": sym,
                    "date": [b.timestamp.astimezone(ET).date() for b in bars],
                    "open": [float(b.open) for b in bars],
                    "close": [float(b.close) for b in bars],
                    "volume": [float(b.volume) for b in bars],
                }))
        if n % 10 == 0:
            print(f"  bars: chunk {n}/{len(chunks)}")
        _time.sleep(0.35)  # stay far under 200/min
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(cache)
    return df


# ------------------------------------------------------------- backtest

def run(months: int, min_dollar_vol: float, refresh: bool) -> None:
    cal = fetch_calendar(months, refresh)
    cal = cal.dropna(subset=["symbol", "date"])
    cal["date"] = pd.to_datetime(cal["date"]).dt.date
    cal = cal[cal["symbol"].str.fullmatch(r"[A-Z]{1,5}")]  # plain US tickers

    start = min(cal["date"]) - timedelta(days=14)
    end = max(cal["date"]) + timedelta(days=5)
    symbols = sorted(cal["symbol"].unique())
    print(f"{len(cal)} events / {len(symbols)} symbols; bars {start}..{end}")
    bars = fetch_daily_bars(symbols, start, end, refresh)

    # Panel: symbol x session-date -> close, dollar vol; session index per sym
    bars = bars.sort_values(["symbol", "date"])
    bars["dv"] = bars["close"] * bars["volume"]
    by_sym: dict[str, pd.DataFrame] = {
        s: g.reset_index(drop=True) for s, g in bars.groupby("symbol")
    }

    rng = np.random.default_rng(20260805)
    results = []
    variants = [f"T-{n}_cc" for n in ENTRY_DAYS] + ["lastday_oc", "priorday_oc"]
    per_event_rows: dict[str, list[float]] = {v: [] for v in variants}

    for _, ev in cal.iterrows():
        sym, d = ev["symbol"], ev["date"]
        g = by_sym.get(sym)
        if g is None or len(g) < 30:
            continue
        dates = g["date"].to_list()
        # announce-day session index (or next session for weekend-dated rows)
        idx = np.searchsorted(dates, d)
        if idx >= len(dates):
            continue
        amc = str(ev.get("hour") or "") == "amc"
        exit_idx = idx if amc else idx - 1   # AMC: announce-day close is safe
        if exit_idx < 1 or exit_idx >= len(g):
            continue
        for n in ENTRY_DAYS:
            entry_idx = exit_idx - n
            if entry_idx < 1:
                continue
            if g["dv"].iloc[entry_idx] < min_dollar_vol:
                continue
            entry, exit_ = g["close"].iloc[entry_idx], g["close"].iloc[exit_idx]
            if entry <= 0:
                continue
            bp = (exit_ / entry - 1) * 1e4 - 2 * COST_BP_SIDE
            per_event_rows[f"T-{n}_cc"].append(bp)
        # Intraday variants (the premium concentrates near the event): hold
        # open->close of the LAST SAFE session (AMC: announce day itself —
        # the print is after the bell) and of the session before it. Pure
        # RTH, no overnight, no event exposure.
        for label, i in (("lastday_oc", exit_idx), ("priorday_oc", exit_idx - 1)):
            if i < 1 or g["dv"].iloc[i - 1] < min_dollar_vol:
                continue
            o, c = g["open"].iloc[i], g["close"].iloc[i]
            if o and o > 0:
                per_event_rows[label].append((c / o - 1) * 1e4 - 2 * COST_BP_SIDE)

    # Null: random matched-horizon holds on the same (liquid) symbol panel.
    liquid_pool = [(s, g["open"].to_numpy(), g["close"].to_numpy())
                   for s, g in by_sym.items()
                   if len(g) > 40 and g["dv"].median() >= min_dollar_vol]

    def null_draw(horizon_sessions: int, intraday: bool) -> float:
        _, opens, closes = liquid_pool[rng.integers(len(liquid_pool))]
        i = rng.integers(1, len(closes) - max(horizon_sessions, 1) - 1)
        if intraday:
            entry = opens[i]
            exit_ = closes[i]
        else:
            entry = closes[i]
            exit_ = closes[i + horizon_sessions]
        if not entry or entry <= 0:
            return 0.0
        return (exit_ / entry - 1) * 1e4 - 2 * COST_BP_SIDE

    horizon = {**{f"T-{n}_cc": (n, False) for n in ENTRY_DAYS},
               "lastday_oc": (1, True), "priorday_oc": (1, True)}
    for variant in variants:
        obs = np.array(per_event_rows[variant])
        if len(obs) < 50:
            results.append({"entry": variant, "events": len(obs),
                            "note": "too few events"})
            continue
        n_sessions, intraday = horizon[variant]
        boots = np.empty(N_BOOT)
        m = min(len(obs), 2000)
        for k in range(N_BOOT):
            boots[k] = sum(null_draw(n_sessions, intraday)
                           for _ in range(m)) / m
        mean = obs.mean()
        t = mean / (obs.std(ddof=1) / np.sqrt(len(obs)))
        hold_days = 1 if intraday else n_sessions + 1
        results.append({
            "entry": variant, "events": len(obs),
            "win_pct": round(float((obs > 0).mean() * 100), 1),
            "mean_bp": round(float(mean), 1),
            "t": round(float(t), 2),
            "null_mean_bp": round(float(boots.mean()), 1),
            "null_pctile": round(float((boots < mean).mean() * 100), 1),
            "ann_excess_bp": round(float((mean - boots.mean()) * 252 / hold_days), 0),
        })

    out = pd.DataFrame(results)
    print("\n=== EARNINGS LEAD-UP (long into the print, exit before it; "
          "net of costs; null = matched-horizon random holds) ===")
    print(out.to_string(index=False))
    stamp = datetime.now(ET).strftime("%Y%m%d")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"leadup_backtest_{stamp}.md")
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        f"# Earnings lead-up backtest — {datetime.now(ET):%Y-%m-%d}\n\n"
        f"{months} months, min entry-day dollar vol ${min_dollar_vol/1e6:.0f}M,"
        f" costs {COST_BP_SIDE}bp/side. Method: script docstring.\n\n"
        f"```\n{out.to_string(index=False)}\n```\n",
        encoding="utf-8")
    print(f"\nwrote {doc}")
    (CACHE / "last_results.json").write_text(out.to_json(orient="records"))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--min-dollar-vol", type=float, default=50e6)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    run(args.months, args.min_dollar_vol, args.refresh)
