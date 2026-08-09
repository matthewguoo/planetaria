"""The no-SIP after-hours thesis, proven empirically in one table.

THE COUNTERINTUITION THIS SETTLES. Alpaca lets this paper account trade
effectively 24/5 — extended-hours DAY limits fill in premarket, postmarket
and the overnight session (verified 2026-08-04) — yet after 17:00 ET the
same account cannot see a live quote. That is not a bug and not one
product: EXECUTION is brokerage (venues route and fill regardless of what
you subscribe to), while real-time consolidated DATA is a licensed
redistribution product (CTA/UTP fees), sold separately as the $99 SIP tier.
The free tier gets IEX — a venue that runs 08:00-17:00 ET and carries ~2-3%
of volume — plus the consolidated tape 15 minutes late. You may trade at
prices you cannot yet see; fifteen minutes later you may see exactly what
they were.

This script makes each clause of that sentence measurable, per symbol:

  iex latest     the entitled feed: fresh until 17:00 ET, then a dead book
                 whose age grows all evening — the wall every flagship entry
                 died at.
  sip latest     the paywall itself: a clean subscription error on the free
                 tier, reported as the finding it is.
  sip 16m ago    the free tier's delayed-SIP window: the consolidated NBBO
                 from 16 minutes ago, returned WITHOUT a subscription — the
                 ground truth pead_nosip verifies its fills against.
  sip 5m ago     the boundary: the same query inside the 15-minute window is
                 refused, which is what makes the delayed channel useless
                 for entries and perfect for audits.
  yahoo/finnhub  the public prints pead_nosip prices entries from, with
                 their honest ages.

With --order it also proves the trading side: an extended-hours DAY limit
parked far below the market is ACCEPTED by the paper endpoint while the
data side is dark, then cancelled. Order placement is the one thing this
script does that leaves (brief) state on the account; it never places
anything marketable.

Run:  .venv/Scripts/python.exe scripts/verify_nosip_ah_quotes.py
      [--symbols NVDA,AMD,SPY] [--order] [--write-doc]

Most informative 16:00-20:00 ET on a weekday (the AH session itself);
outside it the table still shows the structure, with every source stale
together on a weekend.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.ah_quotes import AhQuoteFeed  # noqa: E402
from app.services.market_clock import equity_session  # noqa: E402

ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets/v2/stocks"
PAPER = "https://paper-api.alpaca.markets/v2"
DEFAULT_SYMBOLS = ("NVDA", "AMD", "SPY")


def _age(ts_iso: str | None, now: datetime) -> float | None:
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds()


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _last_ah_instant(now: datetime) -> datetime:
    """The most recent COMPLETED weekday 17:30 ET — an instant when IEX had
    been closed for half an hour while the consolidated tape kept printing.
    Querying that one minute against both feeds is the whole thesis in two
    rows: same symbol, same minute, 0 IEX updates vs N SIP updates."""
    et = now.astimezone(ET)
    day = et.date()
    while True:
        candidate = datetime(day.year, day.month, day.day, 17, 30, tzinfo=ET)
        if candidate.weekday() < 5 and candidate < et - timedelta(minutes=45):
            return candidate.astimezone(timezone.utc)
        day -= timedelta(days=1)


def _get(http: httpx.Client, path: str, **params) -> tuple[dict | None, str | None]:
    """(json, error). An entitlement refusal is a FINDING, not a crash."""
    try:
        resp = http.get(f"{DATA}{path}", params=params, timeout=15)
    except Exception as exc:                               # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        try:
            msg = resp.json().get("message") or resp.text
        except ValueError:
            msg = resp.text
        return None, f"HTTP {resp.status_code}: {str(msg)[:90]}"
    return resp.json(), None


def probe_symbol(http: httpx.Client, feed: AhQuoteFeed, symbol: str,
                 now: datetime) -> list[str]:
    rows: list[str] = []

    def row(source: str, px, age, note="") -> None:
        px_s = f"{px:,.2f}" if isinstance(px, (int, float)) and px else "—"
        rows.append(f"| {symbol} | {source} | {px_s} | {_fmt_age(age)} "
                    f"| {note} |")

    doc, err = _get(http, f"/{symbol}/quotes/latest", feed="iex")
    quote = (doc or {}).get("quote") or {}
    row("iex latest", ((quote.get("bp") or 0) + (quote.get("ap") or 0)) / 2
        if quote else None, _age(quote.get("t"), now), err or "the entitled feed")

    doc, err = _get(http, f"/{symbol}/quotes/latest", feed="sip")
    quote = (doc or {}).get("quote") or {}
    row("sip latest", ((quote.get("bp") or 0) + (quote.get("ap") or 0)) / 2
        if quote else None, _age(quote.get("t"), now),
        err or "ENTITLED?! consider flipping the feed instead")

    for label, minutes, expect in (("sip 16m ago", 16, "the audit channel"),
                                   ("sip 5m ago", 5, "should refuse")):
        end = now - timedelta(minutes=minutes)
        doc, err = _get(http, f"/{symbol}/quotes", feed="sip",
                        start=(end - timedelta(seconds=60)).isoformat(),
                        end=end.isoformat(), limit=1000)
        quotes = (doc or {}).get("quotes") or []
        last = quotes[-1] if quotes else {}
        row(label, ((last.get("bp") or 0) + (last.get("ap") or 0)) / 2
            if last else None,
            _age(last.get("t"), now) if last else None,
            err or (f"{len(quotes)} NBBO updates in 60s — {expect}"
                    if quotes else "no quotes in window"))

    # The same past minute against both feeds: 17:30 ET, when IEX had been
    # closed half an hour while the consolidated tape printed on.
    instant = _last_ah_instant(now)
    stamp = instant.astimezone(ET).strftime("%a %H:%M ET")
    for feed_name in ("iex", "sip"):
        doc, err = _get(http, f"/{symbol}/quotes", feed=feed_name,
                        start=instant.isoformat(),
                        end=(instant + timedelta(seconds=60)).isoformat(),
                        limit=1000)
        quotes = (doc or {}).get("quotes") or []
        last = quotes[-1] if quotes else {}
        row(f"{feed_name} @ {stamp}",
            ((last.get("bp") or 0) + (last.get("ap") or 0)) / 2
            if last else None,
            _age(last.get("t"), now) if last else None,
            err or f"{len(quotes)} NBBO updates in that minute")

    async def _external() -> list[dict | None]:
        try:
            ext = await feed.latest(symbol)
        finally:
            pass
        return [ext]

    (ext,) = asyncio.run(_external())
    if ext:
        row(ext["src"], ext["mid"], (now.timestamp() * 1000 - ext["ts"]) / 1000,
            "public print — the entry-pricing source")
    else:
        row("yahoo/finnhub", None, None, "no external print")
    return rows


def prove_order_path(settings, symbol: str, lines: list[str]) -> None:
    """An extended-hours DAY limit far below the market: ACCEPTED while the
    data side is dark, then cancelled. Paper endpoint only, by construction."""
    if not settings.alpaca_paper:
        lines.append("--order refused: ALPACA_PAPER is not true")
        return
    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
               "APCA-API-SECRET-KEY": settings.alpaca_secret_key}
    with httpx.Client(headers=headers, timeout=15) as http:
        try:
            start = (datetime.now(timezone.utc)
                     - timedelta(days=7)).isoformat()
            bars = http.get(f"{DATA}/{symbol}/bars",
                            params={"timeframe": "1Day", "limit": 5,
                                    "start": start, "feed": "iex"}).json()
            last_close = float((bars.get("bars") or [{}])[-1].get("c") or 0)
        except Exception as exc:                           # noqa: BLE001
            lines.append(f"--order skipped: no reference close ({exc})")
            return
        if last_close <= 0:
            lines.append("--order skipped: no reference close")
            return
        limit_px = round(last_close * 0.5, 2)
        order = {"symbol": symbol, "qty": "1", "side": "buy", "type": "limit",
                 "limit_price": str(limit_px), "time_in_force": "day",
                 "extended_hours": True,
                 "client_order_id": f"nosip-verify-{int(datetime.now().timestamp())}"}
        resp = http.post(f"{PAPER}/orders", json=order)
        if resp.status_code not in (200, 201):
            lines.append(f"order REJECTED ({resp.status_code}): "
                         f"{resp.text[:120]} — the execution-side claim FAILS")
            return
        placed = resp.json()
        lines.append(
            f"order path: 1 {symbol} extended-hours DAY limit @ {limit_px} "
            f"(50% under last close) -> status **{placed.get('status')}** "
            f"(id {placed.get('id', '')[:13]}…) — accepted while the entitled "
            f"quote is stale")
        http.delete(f"{PAPER}/orders/{placed['id']}")
        after = http.get(f"{PAPER}/orders/{placed['id']}",
                         params={"nested": "false"})
        state = after.json().get("status") if after.status_code == 200 else "?"
        lines.append(f"cancelled -> status {state}. No position, no fill, "
                     f"no residue.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--order", action="store_true",
                    help="also prove the paper order path (place+cancel one "
                         "far-from-market extended-hours limit)")
    ap.add_argument("--write-doc", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        raise SystemExit("no Alpaca keys in .env")
    now = datetime.now(timezone.utc)
    et = now.astimezone(ET)
    lines = [
        "# no-SIP AH data/trading asymmetry — verified "
        f"{et.strftime('%Y-%m-%d %H:%M ET')}",
        "",
        f"equity session: **{equity_session() or 'closed (weekend gap)'}**; "
        f"configured feed: **{settings.alpaca_stock_feed}**",
        "",
        "| symbol | source | price | age | note |",
        "|---|---|---|---|---|",
    ]
    feed = AhQuoteFeed(settings)
    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
               "APCA-API-SECRET-KEY": settings.alpaca_secret_key}
    with httpx.Client(headers=headers, timeout=15) as http:
        for symbol in [s.strip().upper() for s in args.symbols.split(",") if s]:
            lines.extend(probe_symbol(http, feed, symbol, now))
    asyncio.run(feed.close())
    lines.append("")
    if args.order:
        prove_order_path(settings, "SPY", lines)
        lines.append("")
    lines.append(
        "Reading: `iex latest` fresh only 08:00-17:00 ET; `sip latest` "
        "refused (the paywall); `sip 16m ago` returned (the free audit "
        "channel pead_nosip verifies fills against); `sip 5m ago` refused "
        "(the 15-minute boundary); the public print is what prices the "
        "entry. Execution and data are separate products — the order path "
        "stays open through all of it.")
    print("\n".join(lines))
    if args.write_doc:
        out = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
               / f"nosip_ah_quotes_{et.strftime('%Y%m%d_%H%M')}.md")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
