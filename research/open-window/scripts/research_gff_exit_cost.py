"""The gff exit book, measured — NBBO half-spread at the 09:31:30 stop.

The decade note's cost table is the whole verdict for gap_fail_fade
(Sharpe 1.25 @6bp, 0.99 @10bp, dead @20bp) and the exit cost is its one
guessed number: the MOO entry pays no spread, but the 09:31:30 time-stop
crosses a gapped, one-minute-old book. The pre-registration's stopping
rule fires at measured RT cost > 20bp. This study replaces the guess with
the historical NBBO itself: for every trade in the registered decade
selection, fetch SIP quotes 09:31:15 -> 09:35:05 and read the book as-of
09:31:30 (the hard stop) and 09:35:00 (the window's far edge).

Cost model being tested, stated: entry at the auction print (free), exit
as a marketable limit crossing half the spread — so measured half-spread
~= the round trip. Crossed/locked/absent books are counted, not averaged
away; they are the halt-adjacency tail the queue's item 6 tracks.

Run:  python scripts/research_gff_exit_cost.py fetch    (resumable, ~3.4k requests)
      python scripts/research_gff_exit_cost.py score
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(STUDY / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_gff_decade import (  # noqa: E402
    MIN_PRICE,
    MIN_TURN_BP,
    SLOTS_PER_LEG,
    load_union,
    tstat,
)

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
QUOTES_F = CACHE / "gff_exit_quotes.parquet"

ASOF = ("09:31:30", "09:35:00")
COST_ROWS = (6.0, 10.0, 15.0, 20.0)


def registered_selection() -> pd.DataFrame:
    """The decade note's trade set, verbatim conventions."""
    df = load_union().dropna(subset=["pm0915", "pm0929", "open", "c1"])
    df = df[df["open"] >= MIN_PRICE].copy()
    pm_trend = (df["pm0929"] / df["pm0915"] - 1) * 1e4
    gap = df["gap"]
    fading = (pm_trend.abs() >= MIN_TURN_BP) & \
             (np.sign(pm_trend) != np.sign(gap * 1e4))
    df["bp"] = -np.sign(gap) * (df["c1"] / df["open"] - 1) * 1e4
    ev = df[fading].dropna(subset=["bp"]).copy()
    ev["long"] = ev["gap"] < 0
    sel = (ev.sort_values(["date", "pmvol"], ascending=[True, False])
           .groupby(["date", "long"]).head(SLOTS_PER_LEG).copy())
    return sel


def stage_fetch(args) -> None:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockQuotesRequest

    from app.config import get_settings

    s = get_settings()
    api = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    sel = registered_selection()
    prior, have = pd.DataFrame(), set()
    if QUOTES_F.exists():
        prior = pd.read_parquet(QUOTES_F)
        have = set(zip(prior["symbol"], prior["date"].astype(str)))
    todo = [r for r in sel.itertuples()
            if (r.symbol, str(r.date)) not in have]
    print(f"{len(todo)} trades to quote (of {len(sel)} in the selection)")
    rows = []

    def flush():
        nonlocal rows, prior
        if not rows:
            return
        df = pd.DataFrame(rows)
        allf = pd.concat([prior, df], ignore_index=True) if len(prior) else df
        allf = allf.drop_duplicates(subset=["symbol", "date"])
        allf.to_parquet(QUOTES_F)
        prior, rows = allf, []

    for n, r in enumerate(todo):
        d = datetime.fromisoformat(str(r.date))
        try:
            res = api.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=r.symbol,
                start=d.replace(hour=9, minute=31, second=15, tzinfo=ET),
                end=d.replace(hour=9, minute=31, second=45, tzinfo=ET),
                feed="sip"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  {r.symbol} {r.date}: {str(exc)[:70]}")
            _time.sleep(1.0)
            continue
        quotes = res.data.get(r.symbol) or []
        rec = {"symbol": r.symbol, "date": str(r.date),
               "long": bool(r.long), "gap": float(r.gap),
               "n_quotes": len(quotes)}
        for asof in ASOF:
            hh, mm, ss = map(int, asof.split(":"))
            cut = d.replace(hour=hh, minute=mm, second=ss, tzinfo=ET)
            at_or_before = [q for q in quotes
                            if q.timestamp.astimezone(ET) <= cut]
            q = at_or_before[-1] if at_or_before else (
                quotes[0] if quotes else None)
            tag = asof.replace(":", "")
            if q is None:
                rec[f"bid_{tag}"] = rec[f"ask_{tag}"] = None
                rec[f"age_{tag}"] = None
            else:
                rec[f"bid_{tag}"] = float(q.bid_price)
                rec[f"ask_{tag}"] = float(q.ask_price)
                rec[f"age_{tag}"] = (cut - q.timestamp.astimezone(ET)
                                     ).total_seconds()
        rows.append(rec)
        if n % 200 == 0:
            print(f"  {n}/{len(todo)} ({r.date} {r.symbol})", flush=True)
            flush()
        _time.sleep(0.3)
    flush()
    print(f"cached {len(prior)} quoted trades -> {QUOTES_F}")


def stage_score(args) -> None:
    q = pd.read_parquet(QUOTES_F)
    sel = registered_selection()
    j = sel.merge(q.drop(columns=["long", "gap"]),
                  on=["symbol", "date"], how="inner")
    j["year"] = j["date"].str[:4]

    lines: list[str] = []

    def emit(t=""):
        print(t)
        lines.append(t)

    emit(f"# The gff exit book, measured — {STAMP}")
    emit()
    emit(f"{len(j):,} of {len(sel):,} registered trades quoted (SIP NBBO "
         "as-of the timestamps; MOO entry pays no spread, the exit crosses "
         "half the book). half_bp = (ask-bid)/2/mid in bp.")
    emit()

    # The first ~200 rows were fetched with a wide window reaching 09:35;
    # the rest use a 30s window whose "09:35" mark would just be a stale
    # 09:31 quote — the age filter keeps only genuine 09:35 books.
    for tag, label, max_age in (("093130", "09:31:30 (the hard stop)", None),
                                ("093500", "09:35:00 (window edge, "
                                           "wide-window subset)", 60.0)):
        bid, ask = j[f"bid_{tag}"], j[f"ask_{tag}"]
        valid = bid.notna() & ask.notna() & (bid > 0) & (ask > 0)
        if max_age is not None:
            valid &= j[f"age_{tag}"].notna() & (j[f"age_{tag}"] <= max_age)
        crossed = valid & (ask <= bid)
        ok = valid & (ask > bid)
        mid = (ask + bid) / 2
        half = ((ask - bid) / 2 / mid * 1e4)[ok]
        age = j.loc[ok, f"age_{tag}"]
        emit(f"## {label}")
        emit()
        emit(f"- book present {valid.sum():,} ({valid.mean() * 100:.1f}%), "
             f"crossed/locked {int(crossed.sum())}, missing "
             f"{int((~valid).sum())} (the halt-adjacency tail)")
        emit(f"- half-spread bp: mean {half.mean():.1f}, median "
             f"{half.median():.1f}, p75 {half.quantile(0.75):.1f}, p90 "
             f"{half.quantile(0.90):.1f}, p99 {half.quantile(0.99):.1f}")
        emit(f"- quote age at the cut: median {age.median():.1f}s, p90 "
             f"{age.quantile(0.90):.1f}s")
        emit()
        emit("| slice | n | mean half bp | median | p90 | share > 10bp % | share > 20bp % |")
        emit("|---|---|---|---|---|---|---|")

        def srow(name, mask):
            h = ((j[f"ask_{tag}"] - j[f"bid_{tag}"]) / 2
                 / ((j[f"ask_{tag}"] + j[f"bid_{tag}"]) / 2) * 1e4)[mask & ok]
            if len(h) < 30:
                return
            emit(f"| {name} | {len(h):,} | {h.mean():.1f} | {h.median():.1f} "
                 f"| {h.quantile(0.90):.1f} | {(h > 10).mean() * 100:.0f} "
                 f"| {(h > 20).mean() * 100:.0f} |")

        srow("ALL", pd.Series(True, index=j.index))
        srow("LONG leg (gap-down books)", j["long"])
        srow("SHORT leg (gap-up books)", ~j["long"])
        for y in sorted(j["year"].unique()):
            srow(y, j["year"] == y)
        srow("|gap| >= 3%", j["gap"].abs() >= 0.03)
        srow("|gap| < 3%", j["gap"].abs() < 0.03)
        emit()

    # ---- net edge under the measured cost, per trade ----------------------
    tag = "093130"
    bid, ask = j[f"bid_{tag}"], j[f"ask_{tag}"]
    ok = bid.notna() & ask.notna() & (ask > bid) & (bid > 0)
    mid = (ask + bid) / 2
    j.loc[ok, "half_bp"] = ((ask - bid) / 2 / mid * 1e4)[ok]
    emit("## The decade edge under the MEASURED exit cost")
    emit()
    emit("Each trade charged its own book's half-spread (missing/crossed "
         "books charged 25bp, the conservative stand-in) instead of a flat "
         "assumption.")
    emit()
    emit("| legs | n | net bp/tr | t | vs flat 10bp |")
    emit("|---|---|---|---|---|")
    cost = j["half_bp"].fillna(25.0)
    for legs, m in (("LONG", j["long"]), ("BOTH", pd.Series(True, index=j.index))):
        net = (j["bp"] - cost)[m]
        flat = (j["bp"] - 10.0)[m]
        emit(f"| {legs} | {len(net):,} | {net.mean():+.1f} "
             f"| {tstat(net.to_numpy()):+.2f} | {net.mean() - flat.mean():+.1f} |")
    emit()
    emit("| year | BOTH net (measured) bp/tr | t |")
    emit("|---|---|---|")
    for y in sorted(j["year"].unique()):
        m = j["year"] == y
        net = (j["bp"] - cost)[m]
        emit(f"| {y} | {net.mean():+.1f} | {tstat(net.to_numpy()):+.2f} |")
    emit()
    flat_rows = ", ".join(f"{c:.0f}bp -> {(j['bp'] - c).mean():+.1f}"
                          for c in COST_ROWS)
    emit(f"Flat-cost rows on the same quoted subset, for the bracket: "
         f"{flat_rows} (bp/tr, BOTH).")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_gff_exit_cost.py score` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gff_exit_cost_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["fetch", "score"])
    args = ap.parse_args()
    {"fetch": stage_fetch, "score": stage_score}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
