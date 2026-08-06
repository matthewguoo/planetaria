"""Rebuild the event panel with ACCEPTANCE-RELATIVE reaction windows.

WHAT WAS WRONG. Every panel before this one measured the reaction in a fixed
[16:00, 16:20] ET slice, and classified an event as after-close by slicing
EDGAR's acceptanceDateTime as text and comparing to 16:00 — on the belief that
the value was ET wearing a spurious `Z`. It is real UTC (see
research_llm_contamination.acceptance_et for the two facts that settle it), so
"amc" meant "after noon ET in summer" and the fixed window measured the tape
BEFORE the news for anything that crossed after 16:20. Phase 12 filtered those
227 events out. This module measures them properly instead:

    anchor  the last print at or before 16:05 ET — the live strategy's own
            16:04 re-anchor to the post-auction tape, so move_pct means the
            same thing here as it does in the engine
    react   the last print within REACT_MIN minutes AFTER that event's own
            acceptance timestamp
    exit    the next session's 15:55 close, re-derived per session

`anchor_pre` (the last print strictly before acceptance) is stored alongside so
the paper can report how much of the measured move is release reaction versus
drift between the close and a late filing.

ONE PASS, TWO PURPOSES. The reaction sweep is batched BY DATE — every AMC
reporter on a given day comes back in one request — which turns ~9,000 calls
into ~1,250. Only events that survive the gate, the liquidity floor and the
top-N rule then get the expensive per-event multi-session pass, which also
captures the excursion first-touch grid research_holding_period needs, so the
minute tape is fetched once rather than twice.

TEN YEARS. Alpaca's history starts 2016-01, so `--windows ten` builds
2016-2021 as well. Those years are SAMPLED (`--sample-per-year`): the selection
rule is identical, a uniform random subset of the qualifying events is kept,
and the sampling is reported rather than hidden. LLM verdicts cost money and a
sparse decade answers "does this survive a different market" better than a
dense five years answers it twice.

Run:
    python scripts/research_event_panel.py calendar --windows ten
    python scripts/research_event_panel.py react    --windows ten
    python scripts/research_event_panel.py paths    --windows ten
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_llm_contamination import (  # noqa: E402
    CACHE,
    ET,
    GATE,
    MIN_DV,
    SEC_UA,
    TOP_PER_DAY,
    acceptance_et,
    submissions,
)

REACT_MIN = 15            # measure the reaction this long after acceptance
ANCHOR_MIN = 16 * 60 + 5  # the live 16:04 re-anchor, one minute of slack
AMC_LO, AMC_HI = 16 * 60, 19 * 60 + 45   # a release needs AH tape after it
MAX_SESSIONS = 5
FETCH_DAYS = 11
LEVELS = np.round(np.arange(0.5, 40.01, 0.5), 2)

# The five windows the original study used, plus 2016-2021 for the decade.
WINDOWS_5Y = [(date(2021, 8, 1), date(2021, 12, 31)),
              (date(2022, 1, 1), date(2022, 12, 31)),
              (date(2023, 1, 1), date(2023, 12, 31)),
              (date(2024, 1, 1), date(2025, 1, 31)),
              (date(2025, 2, 1), date(2026, 8, 1))]
WINDOWS_10Y = [(date(2016, 1, 1), date(2016, 12, 31)),
               (date(2017, 1, 1), date(2017, 12, 31)),
               (date(2018, 1, 1), date(2018, 12, 31)),
               (date(2019, 1, 1), date(2019, 12, 31)),
               (date(2020, 1, 1), date(2020, 12, 31)),
               (date(2021, 1, 1), date(2021, 7, 31))] + WINDOWS_5Y


def windows(which: str) -> list[tuple[date, date]]:
    return WINDOWS_10Y if which == "ten" else WINDOWS_5Y


def cal_path(lo: date, hi: date) -> Path:
    return CACHE / f"edgar_cal_v2_{lo}_{hi}.parquet"


def react_path(lo: date, hi: date) -> Path:
    return CACHE / f"events_v2_{lo}_{hi}.parquet"


PATHS_V2 = CACHE / "event_paths_v2.parquet"


def _client():
    from alpaca.data.historical.stock import StockHistoricalDataClient

    from app.config import get_settings

    s = get_settings()
    return StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)


# ---------------------------------------------------------------- calendar

def stage_calendar(args) -> None:
    """EDGAR item-2.02 filings with acceptance converted to ET, per window.

    Also fetches the daily OHLC panel the run-up and dollar-volume features
    need, padded 21 calendar days before the window opens so the first event
    of a new year still has six prior sessions of history behind it.
    """
    from research_leadup_account_sim import build_universe_window, fetch_ohlc

    for lo, hi in windows(args.windows):
        out = cal_path(lo, hi)
        universe = build_universe_window(lo, args.refresh)
        fetch_ohlc(sorted(universe), lo - timedelta(days=21),
                   hi + timedelta(days=3), args.refresh)
        if out.exists() and not args.refresh:
            print(f"{out.name}: cached ({len(pd.read_parquet(out))} rows)")
            continue
        rows: list[dict] = []
        late = early = 0
        with httpx.Client(headers=SEC_UA, timeout=30,
                          follow_redirects=True) as http:
            for n, (ticker, cik) in enumerate(sorted(universe.items())):
                cached = (CACHE / "sec_submissions" / f"CIK{cik:010d}.json").exists()
                for f in submissions(cik, http):
                    if f["form"] != "8-K" or "2.02" not in f["items"]:
                        continue
                    et = acceptance_et(f["acceptance"])
                    if et is None or not (lo <= et.date() <= hi):
                        continue
                    minute = et.hour * 60 + et.minute
                    if minute < AMC_LO:
                        early += 1
                        continue
                    if minute > AMC_HI:
                        late += 1          # no after-hours tape left to react
                        continue
                    rows.append({"symbol": ticker, "date": str(et.date()),
                                 "acc_min": minute, "acc_iso": et.isoformat(),
                                 "acc": f["acc"]})
                if not cached:
                    _time.sleep(0.12)
                if n % 250 == 0:
                    print(f"  {lo} calendar: {n}/{len(universe)} "
                          f"({len(rows)} amc)")
        df = pd.DataFrame(rows).drop_duplicates(subset=["symbol", "date"])
        df.to_parquet(out)
        print(f"{out.name}: {len(df)} after-close events "
              f"(excluded {early} before 16:00 ET, {late} after "
              f"{AMC_HI // 60}:{AMC_HI % 60:02d})")


# ---------------------------------------------------------------- reactions

def stage_react(args) -> None:
    """Anchor and reaction for every AMC event, batched by announce date.

    One request per DATE rather than per event: the reaction only needs the
    announce day's own tape, and every reporter that night shares it. ~1,250
    requests instead of ~9,000.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    api = _client()
    for lo, hi in windows(args.windows):
        cal = cal_path(lo, hi)
        if not cal.exists():
            print(f"no calendar for {lo}..{hi} — run `calendar` first")
            continue
        out = react_path(lo, hi)
        if out.exists() and not args.refresh:
            print(f"{out.name}: cached ({len(pd.read_parquet(out))} rows)")
            continue
        cal = pd.read_parquet(cal)
        rows: list[dict] = []
        days = sorted(cal["date"].unique())
        for n, day in enumerate(days):
            g = cal[cal["date"] == day]
            d = date.fromisoformat(day)
            syms = sorted(g["symbol"].unique())
            bars_by_sym: dict[str, list] = {}
            for i in range(0, len(syms), 100):
                chunk = syms[i:i + 100]
                try:
                    res = api.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
                        start=datetime(d.year, d.month, d.day, 15, 30, tzinfo=ET),
                        end=datetime(d.year, d.month, d.day, 20, 5, tzinfo=ET),
                        feed="sip"))
                    for s, b in res.data.items():
                        bars_by_sym[s] = b
                except Exception as exc:
                    print(f"  {day} chunk {i}: {str(exc)[:70]}")
                _time.sleep(0.25)
            for _, e in g.iterrows():
                bars = bars_by_sym.get(e["symbol"]) or []
                if not bars:
                    continue
                acc = int(e["acc_min"])
                pre_close = pre_acc = react = None
                react_min = None
                for b in bars:
                    ts = b.timestamp.astimezone(ET)
                    m = ts.hour * 60 + ts.minute
                    px = float(b.close)
                    if m <= ANCHOR_MIN:
                        pre_close = px
                    if m < acc:
                        pre_acc = px
                    if acc <= m <= acc + REACT_MIN:
                        react, react_min = px, m
                if pre_close is None or react is None:
                    continue
                rows.append({"symbol": e["symbol"], "date": day,
                             "acc_min": acc, "anchor": pre_close,
                             "anchor_pre": pre_acc if pre_acc else pre_close,
                             "react": react, "react_min": react_min})
            if n % 25 == 0:
                print(f"  {lo} react: day {n}/{len(days)} ({len(rows)} events)")
        df = pd.DataFrame(rows)
        df.to_parquet(out)
        print(f"{out.name}: {len(df)} events with a measurable reaction")


# ----------------------------------------------------------------- universe

def load_universe_v2(which: str = "ten", sample_per_year: int | None = None,
                     seed: int = 20260806) -> pd.DataFrame:
    """Same selection rule as the original study — gate, liquidity floor,
    top-N per day — applied to acceptance-relative reactions.

    Pre-2021-08 years are optionally subsampled. The rule is unchanged and the
    subset is uniform at random within a year, so the sample is a sample and
    not a different strategy.
    """
    from research_llm_contamination import _bars_for

    frames = []
    for lo, hi in windows(which):
        path = react_path(lo, hi)
        if not path.exists():
            continue
        ev = pd.read_parquet(path)
        if ev.empty:
            continue
        ev["edate"] = pd.to_datetime(ev["date"]).dt.date
        ev["move_pct"] = (ev["react"] / ev["anchor"] - 1) * 100
        g = ev[np.abs(ev["move_pct"]) >= GATE].copy()
        if g.empty:
            continue
        bars = _bars_for(str(g["edate"].min()), str(g["edate"].max()))
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        bars = bars.sort_values(["symbol", "date"])
        panel = {s: (gg["date"].to_list(), gg["c"].to_numpy(),
                     (gg["c"] * gg["v"]).to_numpy())
                 for s, gg in bars.groupby("symbol")}

        def ctx(row):
            d, c, dv = panel.get(row["symbol"], (None, None, None))
            if d is None:
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            i = np.searchsorted(d, row["edate"])
            if not (6 <= i < len(c)):
                return pd.Series({"run5d": np.nan, "dv": np.nan})
            return pd.Series({"run5d": (c[i - 1] / c[i - 6] - 1) * 100,
                              "dv": dv[i - 1]})

        g = pd.concat([g, g.apply(ctx, axis=1)], axis=1)
        g = g[g["dv"] >= MIN_DV]
        g = g.sort_values("dv", ascending=False).groupby("date").head(TOP_PER_DAY)
        frames.append(g)
    if not frames:
        raise SystemExit("no v2 event panels — run `calendar` then `react`")
    out = pd.concat(frames, ignore_index=True)
    out["year"] = out["date"].str[:4]
    out["month"] = out["date"].str[:7]
    out = out.drop_duplicates(subset=["symbol", "date"]).sort_values("date")
    if sample_per_year:
        rng = np.random.default_rng(seed)
        keep = []
        for year, g in out.groupby("year"):
            # The five-year study's own years stay complete; only the decade
            # extension is sparse, and only where it exceeds the quota.
            if year >= "2021" or len(g) <= sample_per_year:
                keep.append(g)
            else:
                keep.append(g.iloc[rng.choice(len(g), sample_per_year,
                                              replace=False)])
        out = pd.concat(keep).sort_values("date")
    return out.reset_index(drop=True)


# -------------------------------------------------------------------- paths

def stage_paths(args) -> None:
    """Per-event multi-session closes plus the excursion first-touch grid,
    entered at the acceptance-relative reaction print."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    m = load_universe_v2(args.windows, args.sample_per_year)
    print(f"{len(m)} selected events {m['date'].min()}..{m['date'].max()}")
    prior, have = pd.DataFrame(), set()
    if PATHS_V2.exists():
        prior = pd.read_parquet(PATHS_V2)
        have = set(zip(prior["symbol"], prior["date"]))
    todo = [r for _, r in m.iterrows() if (r["symbol"], r["date"]) not in have]
    print(f"{len(todo)} need multi-session paths")

    api = _client()
    rows = []
    for n, row in enumerate(todo):
        d, entry = row["edate"], float(row["react"])
        # Start AT the entry minute — everything before it is not ours.
        start = datetime(d.year, d.month, d.day,
                         int(row["react_min"]) // 60,
                         int(row["react_min"]) % 60, tzinfo=ET)
        try:
            res = api.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=row["symbol"], timeframe=TimeFrame.Minute,
                start=start,
                end=start + timedelta(days=FETCH_DAYS), feed="sip"))
            bars = res.data.get(row["symbol"]) or []
        except Exception as exc:
            print(f"  {row['symbol']} {d}: {str(exc)[:60]}")
            bars = []
        up = np.full(len(LEVELS), -1.0)
        dn = np.full(len(LEVELS), -1.0)
        closes, close_minutes = [], []
        minute, sess_date, sess_close = 0, None, None
        for bar in bars:
            ts = bar.timestamp.astimezone(ET)
            hm = ts.hour * 60 + ts.minute
            if ts.date() == d and hm <= int(row["react_min"]):
                continue
            if ts.date() > d:
                if sess_date is not None and ts.date() != sess_date and sess_close:
                    closes.append(sess_close)
                    close_minutes.append(minute)
                    if len(closes) >= MAX_SESSIONS:
                        break
                sess_date = ts.date()
                if hm <= 15 * 60 + 55:
                    sess_close = float(bar.close)
            minute += 1
            np.putmask(up, (up < 0) & (LEVELS <= (float(bar.high) / entry - 1) * 100), minute)
            np.putmask(dn, (dn < 0) & (LEVELS <= -(float(bar.low) / entry - 1) * 100), minute)
        if sess_close and len(closes) < MAX_SESSIONS:
            closes.append(sess_close)
            close_minutes.append(minute)
        if not closes:
            continue
        rows.append({"symbol": row["symbol"], "date": row["date"],
                     "side": int(np.sign(row["move_pct"])), "entry": entry,
                     "acc_min": int(row["acc_min"]),
                     "react_min": int(row["react_min"]),
                     "closes": closes, "close_minutes": close_minutes,
                     "up": up.tolist(), "dn": dn.tolist()})
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}")
        _time.sleep(0.22)
    df = (pd.concat([prior, pd.DataFrame(rows)], ignore_index=True)
          if len(prior) else pd.DataFrame(rows))
    df.to_parquet(PATHS_V2)
    print(f"cached {len(df)} acceptance-relative paths -> {PATHS_V2}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["calendar", "react", "paths"])
    ap.add_argument("--windows", default="ten", choices=["five", "ten"])
    ap.add_argument("--sample-per-year", type=int, default=None,
                    help="cap qualifying events per pre-2021 year")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    {"calendar": stage_calendar, "react": stage_react,
     "paths": stage_paths}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
