"""What the fills actually cost: measured after-earnings AMC spreads.

WHY THIS EXISTS. Every P&L number in the study is net of a FLAT 13bp round
trip — 10bp for the after-hours entry, 3bp for the next-session exit. That
number was an assumption, never a measurement, and it is the single most
load-bearing assumption in the paper: the gated edge is ~139bp, so an
assumption that is wrong by 100bp changes the conclusion and an assumption
that is wrong by 300bp reverses it.

The after-hours book minutes after an earnings release is the worst
microstructure of the day. It is NOT, however, the same as ordinary
after-hours: a name that just reported is the most actively quoted stock on
the tape, so its spread is far tighter than the same name at the same clock
time on a night when nothing happened. Both of those claims are measurable,
and this module measures them instead of asserting either.

WHAT IS MEASURED, per event, from SIP NBBO quotes:

  entry     the quoted spread at the reaction print — the moment the study
            says it enters. This is the real cost of getting in.
  decay     the spread 15 minutes later, which says how fast the book heals
            and therefore how much of the entry cost is avoidable by waiting
  quiet     the SAME symbol at the SAME clock time on a nearby non-event
            session — ordinary after-hours, the control that makes
            "post-earnings AMC is tighter than ordinary AMC" a finding
            rather than an intuition
  exit      the next session's 15:55 regular-hours book — the other leg

Nothing here is modelled. `research_spread_sim` does the modelling, on top of
these numbers.

Run:
    python scripts/research_spread_model.py measure --limit 200   # smoke test
    python scripts/research_spread_model.py measure               # full panel
    python scripts/research_spread_model.py report
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_event_panel import PATHS_V2, _client  # noqa: E402
from research_llm_contamination import CACHE, ET  # noqa: E402

SPREADS = CACHE / "spreads_v2.parquet"

DECAY_MIN = 15        # how long after entry to re-read the book
QUIET_OFFSET_D = 7    # calendar days ahead for the ordinary-AMC control
EXIT_MIN = 15 * 60 + 55


def _mid_spread(quotes) -> tuple[float, float, float, float] | None:
    """(mid, spread_bp, bid_size, ask_size) from the first two-sided quote.

    One-sided books (a zero bid or a zero ask) are real in after-hours and are
    NOT a spread of infinity — they are an absence of a market. Returning None
    keeps them out of the mean rather than letting them define it.
    """
    for q in quotes:
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        # A crossed or absurd book is a data artefact, not a tradeable price.
        if (ask - bid) / mid > 0.5:
            continue
        return mid, (ask - bid) / mid * 1e4, float(q.bid_size or 0), float(q.ask_size or 0)
    return None


def _quote_at(api, symbol: str, when: datetime, window_s: int = 90):
    from alpaca.data.requests import StockQuotesRequest

    try:
        res = api.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=symbol, start=when,
            end=when + timedelta(seconds=window_s), feed="sip", limit=40))
        return res.data.get(symbol) or []
    except Exception:
        return []


def stage_measure(args) -> None:
    if not PATHS_V2.exists():
        raise SystemExit("no v2 path cache — run research_event_panel paths")
    paths = pd.read_parquet(PATHS_V2)
    prior, have = pd.DataFrame(), set()
    if SPREADS.exists() and not args.refresh:
        prior = pd.read_parquet(SPREADS)
        have = set(zip(prior["symbol"], prior["date"]))
    todo = [r for _, r in paths.iterrows()
            if (r["symbol"], r["date"]) not in have]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(paths)} events, {len(todo)} need quotes")

    api = _client()
    rows, failed = [], 0
    for n, row in enumerate(todo):
        d = pd.Timestamp(row["date"]).date()
        rm = int(row["react_min"])
        entry_at = datetime(d.year, d.month, d.day, rm // 60, rm % 60, tzinfo=ET)

        entry = _mid_spread(_quote_at(api, row["symbol"], entry_at))
        if entry is None:
            failed += 1
            if n % 100 == 0:
                print(f"  {n}/{len(todo)} ({len(rows)} ok, {failed} no book)")
            continue
        rec = {"symbol": row["symbol"], "date": row["date"], "acc_min": int(row["acc_min"]),
               "react_min": rm, "entry_mid": entry[0], "entry_bp": entry[1],
               "entry_bidsz": entry[2], "entry_asksz": entry[3]}

        decay = _mid_spread(_quote_at(
            api, row["symbol"], entry_at + timedelta(minutes=DECAY_MIN)))
        rec["decay_bp"] = decay[1] if decay else np.nan

        # Ordinary after-hours: same name, same minute, a week later. Skipped
        # unless the event's own book was readable, so the two are always a
        # matched pair rather than two different samples.
        if not args.no_quiet:
            q = _mid_spread(_quote_at(
                api, row["symbol"], entry_at + timedelta(days=QUIET_OFFSET_D)))
            rec["quiet_bp"] = q[1] if q else np.nan

        # The exit leg, in regular hours the next session.
        cm = row["close_minutes"]
        nxt = d + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        e = _mid_spread(_quote_at(api, row["symbol"], datetime(
            nxt.year, nxt.month, nxt.day, EXIT_MIN // 60, EXIT_MIN % 60, tzinfo=ET)))
        rec["exit_bp"] = e[1] if e else np.nan
        rec["_cm"] = int(cm[0]) if len(cm) else 0

        rows.append(rec)
        if n % 100 == 0:
            print(f"  {n}/{len(todo)} ({len(rows)} ok, {failed} no book)")
        _time.sleep(0.05)

    df = pd.concat([prior, pd.DataFrame(rows)], ignore_index=True) \
        if len(prior) else pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("no quotes came back at all — check SIP entitlement")
    df.to_parquet(SPREADS)
    print(f"\ncached {len(df)} measured books -> {SPREADS}")
    _describe(df)


def _describe(df: pd.DataFrame) -> None:
    def q(col):
        s = df[col].dropna()
        if s.empty:
            return "no data"
        return (f"n={len(s):5d}  median {s.median():7.1f}bp   mean {s.mean():7.1f}bp"
                f"   p75 {s.quantile(.75):7.1f}   p95 {s.quantile(.95):7.1f}")

    print("\n=== measured quoted spreads ===")
    for col, label in (("entry_bp", "post-earnings AMC, at the entry print"),
                       ("decay_bp", f"the same book {DECAY_MIN} min later"),
                       ("quiet_bp", f"ordinary AMC (same name, +{QUIET_OFFSET_D}d)"),
                       ("exit_bp", "regular hours, next session 15:55")):
        print(f"  {label:44s} {q(col)}")
    both = df.dropna(subset=["entry_bp", "quiet_bp"])
    if len(both):
        ratio = (both["entry_bp"] / both["quiet_bp"]).replace(
            [np.inf, -np.inf], np.nan).dropna()
        print(f"\n  matched pairs: {len(both)}. Post-earnings / ordinary AMC "
              f"spread ratio: median {ratio.median():.2f}x")
        print(f"  post-earnings is TIGHTER on {(ratio < 1).mean() * 100:.0f}% "
              f"of matched events.")


def stage_report(args) -> None:
    if not SPREADS.exists():
        raise SystemExit("run `measure` first")
    _describe(pd.read_parquet(SPREADS))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["measure", "report"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-quiet", action="store_true",
                    help="skip the ordinary-AMC control (one fewer request/event)")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    {"measure": stage_measure, "report": stage_report}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
