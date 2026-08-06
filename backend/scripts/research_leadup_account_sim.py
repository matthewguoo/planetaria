"""Account-level simulation: the lead-up premium under the ENGINE'S rules.

The per-event study (research_leadup_backtest.py) says the pre-print
run-up clears a matched null. This answers the next question: does an
ACCOUNT following planetaria's actual risk mechanics compound it —
- entries: each session, the biggest `--max-new` reporters (entry-day
  dollar volume) whose pre-print window opens that day; one open position
  per symbol; skip when gross-full;
- sizing: risk_pct (0.5%) of CURRENT equity at the stop -> notional =
  equity * risk / sl_eff, capped at 20%/name, 100% gross (the engine's
  dials as configured 2026-08-05);
- brackets: the strategy's _effective_bracket math — sl = clamp(2.5 x
  trailing avg |daily ret|, 5%..12%), tp = 2 x sl — walked on daily
  highs/lows, STOP-FIRST on same-bar touches (conservative);
- exits: bracket hit, else the pre-print deadline (AMC: announce-day
  close; BMO: prior close);
- costs 3bp/side. Entry variants: T-3 close, T-2 close, T-1 open.

Null: N_ACCOUNT_BOOTS random-entry accounts (same slot count per day,
random liquid symbol-days, same sizing/bracket machinery). Context: SPY
buy-and-hold over the same span. Daily-resolution honesty: intraday
bracket touches are approximated by daily H/L; gap-throughs fill at the
gap (worse than stop), same as reality.

Run: .venv/Scripts/python.exe scripts/research_leadup_account_sim.py
Reuses _leadup_cache (universe + EDGAR calendar); bars refetch adds H/L.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from alpaca.data.enums import Adjustment  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from app.config import get_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = Path(__file__).resolve().parent / "_leadup_cache"

COST_BP_SIDE = 3.0
RISK_PCT = 0.5           # % of current equity at the stop
PER_NAME_CAP = 0.20      # engine dial
GROSS_CAP = 1.00         # engine dial
SL_MULT, SL_FLOOR, SL_CAP = 2.5, 0.05, 0.12
START_EQUITY = 100_000.0
N_ACCOUNT_BOOTS = 50


SEC_UA = {"User-Agent": "planetaria/0.1 (contact: matthewguo.x86@gmail.com)"}
UNIVERSE_N = 1000


def build_universe_window(win_start: date, refresh: bool) -> dict[str, int]:
    """ticker -> CIK, top UNIVERSE_N by dollar volume in the window's FIRST
    sessions — no ranking on future liquidity. Residual survivorship: names
    fully delisted since then are absent from today's ticker map; bear-
    regime results stay slightly flattered and the doc says so."""
    import httpx

    CACHE.mkdir(exist_ok=True)
    cache = CACHE / f"universe_{win_start}.parquet"
    if cache.exists() and not refresh:
        df = pd.read_parquet(cache)
        return dict(zip(df["ticker"], df["cik"]))
    with httpx.Client(headers=SEC_UA, timeout=30) as http:
        rows = http.get("https://www.sec.gov/files/company_tickers.json").json()
    tick2cik: dict[str, int] = {}
    for row in rows.values():
        t = str(row["ticker"]).upper()
        if t and t.isalpha() and len(t) <= 5:
            tick2cik.setdefault(t, int(row["cik_str"]))
    symbols = sorted(tick2cik)
    settings = get_settings()
    client = StockHistoricalDataClient(settings.alpaca_api_key,
                                       settings.alpaca_secret_key)
    dv: dict[str, float] = {}
    chunks = [symbols[i:i + 75] for i in range(0, len(symbols), 75)]
    for n, chunk in enumerate(chunks):
        try:
            out = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=datetime.combine(win_start, datetime.min.time(), ET),
                end=datetime.combine(win_start + timedelta(days=10),
                                     datetime.min.time(), ET),
                feed="sip"))
        except Exception:
            continue
        for sym, bars in out.data.items():
            if bars:
                dv[sym] = float(bars[-1].close) * float(bars[-1].volume)
        if n % 25 == 0:
            print(f"  universe@{win_start}: chunk {n}/{len(chunks)}")
        _time.sleep(0.3)
    top = sorted(dv, key=dv.get, reverse=True)[:UNIVERSE_N]
    df = pd.DataFrame({"ticker": top, "cik": [tick2cik[t] for t in top]})
    df.to_parquet(cache)
    return dict(zip(df["ticker"], df["cik"]))


def fetch_calendar_window(win_start: date, win_end: date,
                          refresh: bool) -> pd.DataFrame:
    """EDGAR 2.02 events inside [win_start, win_end] for the start-ranked
    universe.

    WRONG AND NOT YET REPAIRED (found 2026-08-06 by verify_no_lookahead.py):
    the `hour` classification below slices acceptanceDateTime as text and
    compares to 16:00, on the 2026-08-04 belief that the value is ET wearing
    a spurious `Z`. It is not — it is real UTC. The median post-15:00
    acceptance shifts 58 minutes between Apr-Oct and Nov-Feb, which is a DST
    boundary and not something issuers do; and read as ET the histogram of
    68,394 cached 8-Ks puts filings at midnight and 02:00, when EDGAR is
    shut. So "amc" here means "accepted after 16:00 UTC" — after noon ET in
    summer, 11:00 ET in winter — and the bucket contains midday filers.

    Rebuilding the calendars would change the universe and force the whole
    LLM study to be re-scored, so the cached calendars are left as they are
    and the contamination is filtered downstream instead:
    research_llm_contamination.timing_ok() drops the 227 of 1,552 scored
    events accepted outside [16:00, 16:20] ET. That fixes the events; it does
    NOT fix selection, because the per-day top-5 was ranked over a candidate
    pool that included the mis-timed names. Any NEW window built from here
    should convert to ET first:
        datetime.fromisoformat(acc.replace("Z", "+00:00")).astimezone(ET)
    """
    import httpx

    cache = CACHE / f"edgar_cal_{win_start}_{win_end}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    universe = build_universe_window(win_start, refresh)
    lo, hi = win_start.isoformat(), win_end.isoformat()
    rows: list[dict] = []
    with httpx.Client(headers=SEC_UA, timeout=30) as http:
        for n, (ticker, cik) in enumerate(sorted(universe.items())):
            try:
                recent = http.get(
                    f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
                ).json()["filings"]["recent"]
            except Exception:
                continue
            for i in range(len(recent["form"])):
                if recent["form"][i] != "8-K":
                    continue
                if not (lo <= recent["filingDate"][i] <= hi):
                    continue
                if "2.02" not in (recent.get("items") or [""] * 99999)[i]:
                    continue
                acc = recent["acceptanceDateTime"][i][:19]
                hhmm = int(acc[11:13]) * 60 + int(acc[14:16])
                hour = "amc" if hhmm >= 16 * 60 else (
                    "bmo" if hhmm < 9 * 60 + 30 else "dmh")
                rows.append({"symbol": ticker, "date": acc[:10], "hour": hour})
            if n % 200 == 0:
                print(f"  edgar cal {win_start}: {n}/{len(universe)}")
            _time.sleep(0.12)
    df = pd.DataFrame(rows).drop_duplicates(subset=["symbol", "date"])
    df = df[df["hour"] != "dmh"]
    df.to_parquet(cache)
    print(f"  calendar {win_start}..{win_end}: {len(df)} events")
    return df


def fetch_ohlc(symbols: list[str], start: date, end: date,
               refresh: bool) -> pd.DataFrame:
    cache = CACHE / f"bars_ohlc_{start}_{end}_{len(symbols)}.parquet"
    if cache.exists() and not refresh:
        return pd.read_parquet(cache)
    settings = get_settings()
    client = StockHistoricalDataClient(settings.alpaca_api_key,
                                       settings.alpaca_secret_key)
    frames = []
    chunks = [symbols[i:i + 75] for i in range(0, len(symbols), 75)]
    for n, chunk in enumerate(chunks):
        try:
            out = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=datetime.combine(start, datetime.min.time(), ET),
                end=datetime.combine(end, datetime.min.time(), ET),
                feed="sip", adjustment=Adjustment.ALL, limit=None))
        except Exception as exc:
            print(f"  ohlc chunk {n}: {str(exc)[:80]}")
            continue
        for sym, bars in out.data.items():
            if bars:
                frames.append(pd.DataFrame({
                    "symbol": sym,
                    "date": [b.timestamp.astimezone(ET).date() for b in bars],
                    "o": [float(b.open) for b in bars],
                    "h": [float(b.high) for b in bars],
                    "l": [float(b.low) for b in bars],
                    "c": [float(b.close) for b in bars],
                    "v": [float(b.volume) for b in bars],
                }))
        _time.sleep(0.3)
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(cache)
    return df


class Panel:
    """Per-symbol OHLC arrays + session-date index."""

    def __init__(self, bars: pd.DataFrame):
        self.by_sym: dict[str, dict] = {}
        for sym, g in bars.sort_values(["symbol", "date"]).groupby("symbol"):
            g = g.reset_index(drop=True)
            closes = g["c"].to_numpy()
            rets = np.abs(np.diff(closes) / closes[:-1])
            self.by_sym[sym] = {
                "dates": g["date"].to_list(),
                "o": g["o"].to_numpy(), "h": g["h"].to_numpy(),
                "l": g["l"].to_numpy(), "c": closes,
                "dv": (g["c"] * g["v"]).to_numpy(),
                "avgret": np.concatenate([[np.nan] * 6, np.array([
                    rets[i - 5:i].mean() for i in range(5, len(rets) + 1)])])
                if len(rets) >= 5 else np.full(len(closes), np.nan),
            }
        self.sessions = sorted({d for g in self.by_sym.values()
                                for d in g["dates"]})

    def idx(self, sym: str, d: date) -> int | None:
        g = self.by_sym.get(sym)
        if g is None:
            return None
        i = np.searchsorted(g["dates"], d)
        return int(i) if i < len(g["dates"]) and g["dates"][i] == d else None


def bracket_for(g: dict, entry_idx: int) -> tuple[float, float]:
    avg = g["avgret"][entry_idx] if entry_idx < len(g["avgret"]) else np.nan
    sl = SL_MULT * avg if np.isfinite(avg) else SL_FLOOR
    sl = min(max(sl, SL_FLOOR), SL_CAP)
    return sl, 2 * sl


def simulate(events: pd.DataFrame, panel: Panel, entry_mode: str,
             max_new: int, start_equity: float = START_EQUITY,
             rng=None, allowed_dates: set | None = None) -> dict:
    """entry_mode: T3_close | T2_close | T1_open. rng -> random-null account
    (same slot counts, random symbol/date entries)."""
    lead = {"T3_close": 3, "T2_close": 2, "T1_open": 1}[entry_mode]
    at_open = entry_mode.endswith("open")

    # Map entry SESSION -> list of (symbol, exit_idx) candidates.
    per_day: dict[date, list[tuple[str, int, float]]] = {}
    for _, ev in events.iterrows():
        g = panel.by_sym.get(ev["symbol"])
        if g is None:
            continue
        i = np.searchsorted(g["dates"], ev["date"])
        if i >= len(g["dates"]):
            continue
        exit_idx = i if ev["hour"] == "amc" else i - 1
        entry_idx = exit_idx - lead
        if entry_idx < 7 or exit_idx >= len(g["dates"]):
            continue
        d = g["dates"][entry_idx]
        per_day.setdefault(d, []).append(
            (ev["symbol"], exit_idx, float(g["dv"][entry_idx - 1])))

    liquid = list(panel.by_sym)
    equity = start_equity
    curve = []
    open_pos: dict[str, dict] = {}
    stats = {"trades": 0, "tp": 0, "sl": 0, "deadline": 0, "wins": 0,
             "skipped_full": 0, "skipped_price": 0}

    for session in panel.sessions:
        # 1) exits first (bracket walk on today's H/L, else deadline close)
        for sym in list(open_pos):
            p = open_pos[sym]
            g = panel.by_sym[sym]
            i = panel.idx(sym, session)
            if i is None:
                continue
            px_exit = None
            if g["l"][i] <= p["sl_px"]:      # stop-first: conservative
                px_exit = min(p["sl_px"], g["o"][i])   # gap-through fills worse
            elif g["h"][i] >= p["tp_px"]:
                px_exit = p["tp_px"]
                stats["tp"] += 1
            if px_exit is None and i >= p["exit_idx"]:
                px_exit = g["c"][i]
                stats["deadline"] += 1
            elif px_exit is not None and px_exit <= p["sl_px"]:
                stats["sl"] += 1
            if px_exit is not None:
                pnl = p["qty"] * (px_exit - p["entry_px"])
                pnl -= (p["qty"] * (px_exit + p["entry_px"])) * COST_BP_SIDE / 1e4
                equity += pnl
                stats["trades"] += 1
                stats["wins"] += pnl > 0
                del open_pos[sym]

        # 2) entries
        cands = sorted(per_day.get(session, []), key=lambda x: -x[2])[:max_new]
        # Regime gate (risk-on only): no NEW entries on risk-off sessions;
        # open positions still walk to their exits. Applies to null too.
        if allowed_dates is not None and session not in allowed_dates:
            cands = []
        if rng is not None and cands:
            # Null: SAME day, SAME slot count, random liquid symbols with the
            # same holding horizon — entries must be session-aligned or the
            # walk is bookkeeping fiction.
            picks = []
            for _ in cands:
                for _try in range(20):
                    sym = liquid[rng.integers(len(liquid))]
                    i = panel.idx(sym, session)
                    gi = panel.by_sym[sym]
                    if i is not None and 7 <= i < len(gi["dates"]) - lead - 1:
                        picks.append((sym, i + lead, 0.0))
                        break
            cands = picks
        gross = sum(p["qty"] * p["entry_px"] for p in open_pos.values())
        for sym, exit_idx, _ in cands:
            if sym in open_pos:
                continue
            g = panel.by_sym[sym]
            i = panel.idx(sym, session)
            if i is None or i >= len(g["c"]):
                continue
            entry_px = g["o"][i] if at_open else g["c"][i]
            if not entry_px or entry_px <= 0:
                continue
            sl, tp = bracket_for(g, i)
            notional = min(equity * RISK_PCT / 100 / sl, equity * PER_NAME_CAP)
            if gross + notional > equity * GROSS_CAP:
                stats["skipped_full"] += 1
                continue
            qty = float(int(notional // entry_px))  # whole shares: limit-order reality
            if qty < 1:
                stats["skipped_price"] += 1  # can't afford one share at this size
                continue
            open_pos[sym] = {
                "qty": qty, "entry_px": entry_px, "exit_idx": exit_idx,
                "sl_px": entry_px * (1 - sl), "tp_px": entry_px * (1 + tp),
            }
            gross += notional
        curve.append(equity + sum(
            p["qty"] * (panel.by_sym[s]["c"][panel.idx(s, session)] - p["entry_px"])
            for s, p in open_pos.items() if panel.idx(s, session) is not None))

    curve_arr = np.array(curve)
    peak = np.maximum.accumulate(curve_arr)
    max_dd = float(((curve_arr - peak) / peak).min() * 100)
    total = (curve_arr[-1] / start_equity - 1) * 100
    return {"entry": entry_mode, "final": round(float(curve_arr[-1]), 0),
            "ret_pct": round(float(total), 1), "max_dd_pct": round(max_dd, 1),
            **{k: stats[k] for k in ("trades", "tp", "sl", "deadline",
                                     "skipped_full", "skipped_price")},
            "win_pct": round(100 * stats["wins"] / max(stats["trades"], 1), 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-new", type=int, default=10)
    ap.add_argument("--equity", type=float, default=START_EQUITY)
    ap.add_argument("--start", default="2025-02-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--spy-filter", action="store_true",
                    help="risk-on gate: new entries only when SPY > 200dma")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    win_start = date.fromisoformat(args.start)
    win_end = date.fromisoformat(args.end)
    cal = fetch_calendar_window(win_start, win_end, args.refresh)
    cal["date"] = pd.to_datetime(cal["date"]).dt.date
    start = min(cal["date"]) - timedelta(days=21)
    end = min(max(cal["date"]) + timedelta(days=5), date.today())
    symbols = sorted(set(cal["symbol"]) | {"SPY"})
    print(f"{len(cal)} events; ohlc {start}..{end} for {len(symbols)} symbols")
    bars = fetch_ohlc(symbols, start, end, args.refresh)
    panel = Panel(bars)

    spy = panel.by_sym.get("SPY")
    spy_ret = (spy["c"][-1] / spy["c"][0] - 1) * 100 if spy is not None else None

    allowed: set | None = None
    if args.spy_filter:
        settings = get_settings()
        client = StockHistoricalDataClient(settings.alpaca_api_key,
                                           settings.alpaca_secret_key)
        out = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
            start=datetime.combine(win_start - timedelta(days=320),
                                   datetime.min.time(), ET),
            end=datetime.combine(end, datetime.min.time(), ET),
            feed="sip", adjustment=Adjustment.ALL))
        sp = pd.DataFrame({
            "date": [b.timestamp.astimezone(ET).date() for b in out.data["SPY"]],
            "c": [float(b.close) for b in out.data["SPY"]]})
        sp["dma"] = sp["c"].rolling(200).mean()
        allowed = set(sp.loc[sp["c"] > sp["dma"], "date"])
        on = len([s for s in panel.sessions if s in allowed])
        print(f"  risk-on gate: {on}/{len(panel.sessions)} sessions allowed")

    rows = [simulate(cal, panel, mode, args.max_new, args.equity,
                     allowed_dates=allowed)
            for mode in ("T3_close", "T2_close", "T1_open")]
    null_finals = []
    for b in range(N_ACCOUNT_BOOTS):
        r = simulate(cal, panel, "T3_close", args.max_new, args.equity,
                     rng=np.random.default_rng(1000 + b),
                     allowed_dates=allowed)
        null_finals.append(r["ret_pct"])
    nf = np.array(null_finals)

    out = pd.DataFrame(rows)
    print(f"\n=== ACCOUNT SIM (${args.equity:,.0f}, {win_start}..{win_end}, "
          f"whole shares, engine sizing/caps, vol brackets, "
          f"max {args.max_new} entries/day) ===")
    print(out.to_string(index=False))
    print(f"\nrandom-entry account null (T3 slots, {N_ACCOUNT_BOOTS} boots): "
          f"mean {nf.mean():.1f}% | p5 {np.percentile(nf, 5):.1f}% | "
          f"p95 {np.percentile(nf, 95):.1f}%")
    print(f"SPY buy-hold same span: {spy_ret:.1f}%" if spy_ret is not None
          else "SPY unavailable")

    stamp = datetime.now(ET).strftime("%Y%m%d")
    doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
           / f"leadup_account_sim_{stamp}_{win_start}_{int(args.equity/1000)}k.md")
    doc.write_text(
        f"# Lead-up ACCOUNT sim — ${args.equity:,.0f}, {win_start}..{win_end}"
        f" — {datetime.now(ET):%Y-%m-%d}\n\n"
        "Universe ranked at WINDOW START (residual survivorship: fully "
        "delisted names absent from today's ticker map). Whole shares.\n\n"
        f"Engine rules: risk {RISK_PCT}%/name at vol-scaled stop "
        f"(x{SL_MULT}, {SL_FLOOR:.0%}..{SL_CAP:.0%}, TP=2xSL), caps "
        f"{PER_NAME_CAP:.0%}/name {GROSS_CAP:.0%} gross, max "
        f"{args.max_new} entries/day, costs {COST_BP_SIDE}bp/side.\n\n"
        f"```\n{out.to_string(index=False)}\n\n"
        f"null (random entries, same machinery): mean {nf.mean():.1f}% "
        f"p5 {np.percentile(nf, 5):.1f}% p95 {np.percentile(nf, 95):.1f}%\n"
        f"SPY buy-hold: {spy_ret:.1f}%\n```\n",
        encoding="utf-8")
    print(f"\nwrote {doc}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
