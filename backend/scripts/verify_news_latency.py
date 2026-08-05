"""Earnings-night latency proof: is the free stack fast enough?

The earnings reaction strategy needs release TEXT in hand well under a
minute after the release. This script measures, per after-close reporter,
when each source first had it:

  symbol | benzinga first/nums | edgar accepted | edgar seen | first move

Two modes:

--retro (default) — reconstruct the MOST RECENT after-close window from
  recorded timestamps: EDGAR getcurrent still lists the evening's filings
  (works same night and before the next 06:00 premarket wave), Alpaca news
  REST carries Benzinga created_at, SIP 1-minute bars give the tape. This
  measures SOURCE availability, not our poll loop — run --live for that.

--live — run during 15:55-17:30 ET on an earnings day. Discovers reporters
  from EDGAR as their 8-Ks land, records acceptance->seen poll lag (the
  number --retro cannot know), captures Benzinga websocket arrivals, then
  backfills SIP bars for first-move at the end of the window.

--write-doc — append the table to docs/notes/earnings_latency_<date>.md
  (the committed evidence the phase brief requires).

Timezone note (verified 2026-08-04): EDGAR Atom <updated> carries a real
ET offset; the submissions-API acceptanceDateTime says "Z" but is ET.
This script only uses the Atom feed and is unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from alpaca.data.historical.news import NewsClient  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import NewsRequest, StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.signals.edgar import (  # noqa: E402
    GETCURRENT_URL,
    TICKER_MAP_URL,
    EdgarFeed,
    Filing,
)

ET = ZoneInfo("America/New_York")

WINDOW_START = (15, 55)   # ET; brief: run 15:55-17:30
WINDOW_END = (17, 30)
NUMS_RE = re.compile(r"(EPS|earnings|revenue|sales|Q[1-4]|quarter)", re.I)
DOLLAR_RE = re.compile(r"[$€£]|\d+(\.\d+)?[BM]\b|\d+\.\d{2}")


@dataclass
class Row:
    symbol: str
    company: str
    accession: str
    edgar_accepted: datetime
    edgar_seen: datetime | None = None      # live mode only: our poll saw it
    text_chars: int | None = None           # live mode: exhibit text fetched
    bz_first: datetime | None = None        # first Benzinga mention
    bz_nums: datetime | None = None         # first Benzinga NUMBERS headline
    first_move: datetime | None = None      # first 1m bar >=move_pct off close
    move_pct: float | None = None
    close: float | None = None
    dollar_vol_m: float | None = None       # day dollar volume, $M (rank key)


def parser_feed() -> EdgarFeed:
    """Parse-only EdgarFeed: reuses the battle-tested Atom/exhibit parsing
    without a bus or journal (they are never touched by the parse path)."""
    return EdgarFeed(bus=None, store=None, settings=get_settings())


def fmt(ts: datetime | None) -> str:
    return ts.astimezone(ET).strftime("%H:%M:%S") if ts else "-"


def delta_s(a: datetime | None, b: datetime | None) -> str:
    if not a or not b:
        return "-"
    return f"{(a - b).total_seconds():+.0f}s"


# --------------------------------------------------------------- discovery

async def discover_retro(http: httpx.AsyncClient, day: datetime,
                         max_pages: int = 8) -> list[Filing]:
    """Page getcurrent back through the evening's 8-Ks. Stops when entries
    predate the window (the feed is newest-first) or pages repeat."""
    feed = parser_feed()
    start_dt = day.replace(hour=WINDOW_START[0], minute=WINDOW_START[1])
    end_dt = day.replace(hour=WINDOW_END[0], minute=WINDOW_END[1])
    out: dict[str, Filing] = {}
    prev_first: str | None = None
    for page in range(max_pages):
        resp = await http.get(GETCURRENT_URL, params={
            "action": "getcurrent", "type": "8-K", "company": "", "dateb": "",
            "owner": "include", "count": "100", "start": str(page * 100),
            "output": "atom",
        })
        resp.raise_for_status()
        filings = feed._parse_atom(resp.text)
        if not filings or filings[0].accession == prev_first:
            break
        prev_first = filings[0].accession
        for f in filings:
            accepted_et = f.accepted_utc.astimezone(ET)
            if f.form == "8-K" and "2.02" in f.items and \
                    start_dt <= accepted_et <= end_dt:
                out[f.accession] = f
        if filings[-1].accepted_utc.astimezone(ET) < start_dt:
            break
        await asyncio.sleep(0.4)  # politeness between pages
    return sorted(out.values(), key=lambda f: f.accepted_utc)


async def load_tickers(http: httpx.AsyncClient) -> dict[int, str]:
    resp = await http.get(TICKER_MAP_URL)
    resp.raise_for_status()
    tickers: dict[int, str] = {}
    for row in resp.json().values():
        tickers.setdefault(int(row["cik_str"]), str(row["ticker"]).upper())
    return tickers


# ------------------------------------------------------------- enrichment

async def rank_by_liquidity(stocks: StockHistoricalDataClient, rows: list[Row],
                            day: datetime, min_price: float, top: int) -> list[Row]:
    """Official close + day dollar volume per symbol; keep price>=min_price,
    top-N by dollar volume. One batched daily-bars request."""
    if not rows:
        return rows
    # end 20:00 ET: the official close is final long before, and the free
    # tier rejects SIP windows reaching too close to now (empirical
    # 2026-08-05 00:28 ET: end=23:59 refused, end=22:00 fine - the recency
    # cut is coarser than the documented 15 minutes).
    req = StockBarsRequest(
        symbol_or_symbols=[r.symbol for r in rows], timeframe=TimeFrame.Day,
        start=day.replace(hour=0, minute=0), end=day.replace(hour=20, minute=0),
        feed="sip",
    )
    bars = await asyncio.to_thread(stocks.get_stock_bars, req)
    for row in rows:
        blist = bars.data.get(row.symbol) or []
        if blist:
            row.close = float(blist[-1].close)
            row.dollar_vol_m = round(
                float(blist[-1].volume) * row.close / 1e6, 1)
    kept = [r for r in rows if r.close and r.close >= min_price]
    kept.sort(key=lambda r: r.dollar_vol_m or 0, reverse=True)
    dropped = len(rows) - len(kept)
    if dropped:
        print(f"  ({dropped} below ${min_price:.0f} or bar-less dropped)")
    return kept[:top]


async def first_benzinga(news: NewsClient, row: Row, day: datetime) -> None:
    req = NewsRequest(symbols=row.symbol,
                      start=day.replace(hour=15, minute=30),
                      end=day.replace(hour=19, minute=0),
                      limit=50, include_content=False, sort="asc")
    try:
        out = await asyncio.to_thread(news.get_news, req)
    except Exception as exc:
        print(f"  news REST failed for {row.symbol}: {exc}")
        return
    for item in out.data.get("news", []):
        created = item.created_at
        headline = item.headline or ""
        if row.bz_first is None:
            row.bz_first = created
        if NUMS_RE.search(headline) and DOLLAR_RE.search(headline):
            row.bz_nums = created
            break


async def first_move(stocks: StockHistoricalDataClient, row: Row,
                     day: datetime, move_pct: float) -> None:
    """First 1m bar at/after the earliest RESULTS-BEARING source (numbers
    headline or EDGAR acceptance) whose close is >=move_pct off the official
    close. Anchoring on bz_first would leak pre-release drift: AMD
    2026-08-04 had a 16:13:54 sector story and +1%% wander before its
    16:15:45 numbers crossed."""
    if not row.close:
        return
    anchor = min((t for t in (row.bz_nums, row.edgar_accepted) if t),
                 default=row.edgar_accepted)
    anchor = anchor.replace(second=0, microsecond=0)
    # Cap the window away from now (SIP recency rule, see rank_by_liquidity);
    # matters when --live finishes at 17:30 and queries immediately.
    bar_end = min(day.replace(hour=19, minute=0),
                  datetime.now(timezone.utc) - timedelta(minutes=31))
    req = StockBarsRequest(symbol_or_symbols=row.symbol,
                           timeframe=TimeFrame.Minute,
                           start=day.replace(hour=16, minute=0),
                           end=bar_end, feed="sip")
    try:
        bars = await asyncio.to_thread(stocks.get_stock_bars, req)
    except Exception as exc:
        print(f"  bars failed for {row.symbol}: {exc}")
        return
    for bar in bars.data.get(row.symbol, []):
        if bar.timestamp < anchor:
            continue
        pct = (float(bar.close) / row.close - 1) * 100
        if abs(pct) >= move_pct:
            row.first_move = bar.timestamp
            row.move_pct = round(pct, 1)
            return


# ------------------------------------------------------------------ live

async def live_capture(args, tickers: dict[int, str]) -> list[Row]:
    """Poll EDGAR every poll_s inside the window, recording acceptance->seen
    lag (+ text fetch); capture Benzinga websocket arrivals in parallel."""
    settings = get_settings()
    feed = parser_feed()
    rows: dict[str, Row] = {}
    bz_seen: dict[str, datetime] = {}

    async def on_news(item) -> None:
        for sym in getattr(item, "symbols", None) or ():
            bz_seen.setdefault(sym, datetime.now(timezone.utc))

    stream_task = None
    if settings.alpaca_api_key:
        from alpaca.data.live.news import NewsDataStream

        stream = NewsDataStream(settings.alpaca_api_key,
                                settings.alpaca_secret_key)
        stream.subscribe_news(on_news, "*")
        stream_task = asyncio.create_task(stream._run_forever())

    end_et = datetime.now(ET).replace(hour=WINDOW_END[0],
                                      minute=WINDOW_END[1], second=0)
    print(f"live until {end_et:%H:%M ET}; polling every {args.poll_s}s")
    async with feed._client() as http:
        try:
            while datetime.now(ET) < end_et:
                t0 = time.monotonic()
                try:
                    resp = await http.get(GETCURRENT_URL, params={
                        "action": "getcurrent", "type": "8-K", "company": "",
                        "dateb": "", "owner": "include", "count": "100",
                        "output": "atom"})
                    resp.raise_for_status()
                    for f in feed._parse_atom(resp.text):
                        if f.form != "8-K" or "2.02" not in f.items:
                            continue
                        if f.accession in rows:
                            continue
                        sym = tickers.get(f.cik)
                        if sym is None:
                            continue
                        row = Row(symbol=sym, company=f.company,
                                  accession=f.accession,
                                  edgar_accepted=f.accepted_utc,
                                  edgar_seen=datetime.now(timezone.utc))
                        text, _, err = await feed._fetch_release_text(http, f)
                        row.text_chars = len(text) if not err else None
                        rows[f.accession] = row
                        lag = (row.edgar_seen - row.edgar_accepted).total_seconds()
                        print(f"  {fmt(row.edgar_seen)} {sym}: accepted "
                              f"{fmt(row.edgar_accepted)}, seen +{lag:.0f}s, "
                              f"text={row.text_chars or 'FAILED'}")
                except Exception as exc:
                    print(f"  poll error: {exc}")
                await asyncio.sleep(max(0.5, args.poll_s -
                                        (time.monotonic() - t0)))
        except KeyboardInterrupt:
            print("interrupted - finishing with what we have")
        finally:
            if stream_task:
                stream_task.cancel()
    out = sorted(rows.values(), key=lambda r: r.edgar_accepted)
    for row in out:  # websocket arrivals (wall-clock of OUR receipt)
        row.bz_first = bz_seen.get(row.symbol)
    return out


# ---------------------------------------------------------------- report

def print_table(rows: list[Row], day: datetime, live: bool) -> list[str]:
    lines = []
    mode = "LIVE (edgar_seen = our poll)" if live else "RETRO (source timestamps)"
    lines.append(f"earnings-night latency - {day:%Y-%m-%d} ET - {mode}")
    lines.append("")
    header = (f"{'symbol':7s} {'$vol(M)':>8s} {'bz_first':>9s} {'bz_nums':>9s} "
              f"{'edgar_acc':>9s} {'seen+':>6s} {'bz-edgar':>9s} "
              f"{'move@':>9s} {'move':>6s}")
    lines.append(header)
    lines.append("-" * len(header))
    for r in rows:
        seen = (f"{(r.edgar_seen - r.edgar_accepted).total_seconds():+.0f}s"
                if r.edgar_seen else "-")
        lines.append(
            f"{r.symbol:7s} {r.dollar_vol_m if r.dollar_vol_m is not None else '-':>8} "
            f"{fmt(r.bz_first):>9s} {fmt(r.bz_nums):>9s} "
            f"{fmt(r.edgar_accepted):>9s} {seen:>6s} "
            f"{delta_s(r.bz_nums or r.bz_first, r.edgar_accepted):>9s} "
            f"{fmt(r.first_move):>9s} "
            f"{f'{r.move_pct:+.1f}%' if r.move_pct is not None else '-':>6s}")
    lines.append("")
    both = [r for r in rows if (r.bz_nums or r.bz_first)]
    if both:
        deltas = sorted(((r.bz_nums or r.bz_first) - r.edgar_accepted)
                        .total_seconds() for r in both)
        med = deltas[len(deltas) // 2]
        lines.append(f"benzinga(nums|first) vs edgar acceptance: median "
                     f"{med:+.0f}s over {len(both)} names "
                     f"(negative = benzinga earlier)")
    moved = [r for r in rows if r.first_move]
    lines.append(f"moved >= threshold after release: {len(moved)}/{len(rows)}")
    for line in lines:
        print(line)
    return lines


def write_doc(lines: list[Row] | list[str], day: datetime, args) -> Path:
    doc = (Path(__file__).resolve().parent.parent.parent
           / "docs" / "notes" / f"earnings_latency_{day:%Y%m%d}.md")
    doc.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    block = "\n".join(lines)
    text = (f"\n## run {stamp} ({'live' if args.live else 'retro'})\n\n"
            f"```\n{block}\n```\n")
    if doc.exists():
        doc.write_text(doc.read_text(encoding="utf-8") + text, encoding="utf-8")
    else:
        doc.write_text(
            f"# Earnings-night news latency - {day:%Y-%m-%d}\n\n"
            "Produced by `backend/scripts/verify_news_latency.py`. Columns:\n"
            "- `bz_first/bz_nums`: first Benzinga mention / first headline "
            "with numbers (created_at from the news REST API; in live mode "
            "bz_first is OUR websocket arrival wall-clock).\n"
            "- `edgar_acc`: EDGAR acceptance (Atom `<updated>`).\n"
            "- `seen+`: acceptance -> our poll saw it (live mode only).\n"
            "- `move@/move`: first SIP 1m bar >= threshold off the official "
            "close, at/after the earliest text source (pre-release drift "
            "excluded by construction).\n" + text,
            encoding="utf-8")
    print(f"\nwrote {doc}")
    return doc


# ------------------------------------------------------------------ main

def target_day(now_et: datetime) -> datetime:
    """Most recent after-close window: today once it's past 17:30, else the
    previous weekday."""
    day = now_et
    if now_et.hour * 60 + now_et.minute < WINDOW_END[0] * 60 + WINDOW_END[1]:
        day = day - timedelta(days=1)
    while day.weekday() >= 5:
        day = day - timedelta(days=1)
    return day.replace(second=0, microsecond=0)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="capture the 15:55-17:30 ET window live")
    ap.add_argument("--date", help="retro target date YYYY-MM-DD "
                                   "(default: most recent evening)")
    ap.add_argument("--top", type=int, default=10,
                    help="keep the N most liquid reporters (default 10)")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--move-pct", type=float, default=1.0,
                    help="first-move threshold vs official close (default 1%%)")
    ap.add_argument("--poll-s", type=float, default=2.5)
    ap.add_argument("--write-doc", action="store_true",
                    help="append results to docs/notes/")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.alpaca_api_key:
        raise SystemExit("Alpaca keys required (news REST + SIP bars)")
    news = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
    stocks = StockHistoricalDataClient(settings.alpaca_api_key,
                                       settings.alpaca_secret_key)

    if args.date:
        day = datetime.fromisoformat(args.date).replace(tzinfo=ET)
    elif args.live:
        day = datetime.now(ET)  # live always measures TODAY's window
    else:
        day = target_day(datetime.now(ET))
    print(f"verify_news_latency @ {datetime.now(ET):%Y-%m-%d %H:%M ET} "
          f"targeting {day:%Y-%m-%d}")

    ua_client = parser_feed()._client()
    async with ua_client as http:
        tickers = await load_tickers(http)
        if args.live:
            rows = await live_capture(args, tickers)
        else:
            filings = await discover_retro(http, day)
            print(f"discovered {len(filings)} Item-2.02 8-Ks in the "
                  f"{WINDOW_START[0]}:{WINDOW_START[1]:02d}-"
                  f"{WINDOW_END[0]}:{WINDOW_END[1]:02d} ET window")
            rows = [Row(symbol=tickers[f.cik], company=f.company,
                        accession=f.accession, edgar_accepted=f.accepted_utc)
                    for f in filings if f.cik in tickers]
            unmapped = len(filings) - len(rows)
            if unmapped:
                print(f"  ({unmapped} untickered filers dropped)")

    rows = await rank_by_liquidity(stocks, rows, day, args.min_price, args.top)
    for row in rows:
        if row.bz_first is None:  # live mode may already have arrivals
            await first_benzinga(news, row, day)
        await first_move(stocks, row, day, args.move_pct)

    lines = print_table(rows, day, args.live)
    if args.write_doc and rows:
        write_doc(lines, day, args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
