"""SIP preflight: prove the $99 subscription is live BEFORE flipping the feed.

The engine reads after-hours prices through market.overnight_price ->
fetch_latest_stock_quote(feed=alpaca_stock_feed), and every AH entry is
gated on that quote being younger than QUOTE_MAX_AGE_S (120s). On the free
IEX feed the book is dead after 16:00 ET, so the gate refuses everything —
verified live 2026-08-05, and visible in the LAB deck as a wall of
"no fresh quote for X". That is the ONLY thing standing between the
journaled decisions and real fills.

This script answers, empirically, the one question worth asking before
touching a setting: does feed=sip return a FRESH two-sided quote right now,
in whichever session we are in? It checks the three surfaces the strategy
actually touches, side by side with IEX so the difference is visible rather
than asserted:

  1. latest quote          — the entry price + the freshness gate
  2. latest trade          — tape corroboration for the quote
  3. recent 1m bars        — the exit enforcer's mark and the twin's path

Entitlement failures are reported as what they are: a free-tier key gets a
clean 403/subscription error on real-time SIP, which is a PASS for "the
check works" and a FAIL for "you may flip the feed".

Run:  .venv/Scripts/python.exe scripts/verify_sip_entitlement.py
      [--symbols NVDA,AMD,SPY] [--max-age-s 120] [--write-doc]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.market_data import is_overnight_et  # noqa: E402
from app.strategies.earnings_reaction import QUOTE_MAX_AGE_S  # noqa: E402

ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets/v2/stocks"
DEFAULT_SYMBOLS = ("NVDA", "AMD", "SPY")


def session_label(now_utc: datetime) -> str:
    et = now_utc.astimezone(ET)
    minute = et.hour * 60 + et.minute
    if et.weekday() >= 5:
        return "weekend (closed)"
    if is_overnight_et(now_utc):
        return "overnight (Blue Ocean)"
    if minute < 4 * 60:
        return "closed"
    if minute < 9 * 60 + 30:
        return "premarket"
    if minute <= 16 * 60:
        return "regular hours"
    return "after-hours (16:00-20:00 ET)"


def age_s(ts_iso: str | None, now: datetime) -> float | None:
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds()


def get(http: httpx.Client, path: str, **params) -> tuple[dict | None, str | None]:
    """(json, error). An entitlement refusal is an error STRING, not an
    exception — the whole point is to report it, not to crash on it."""
    try:
        resp = http.get(f"{DATA}{path}", params=params, timeout=15)
    except Exception as exc:
        return None, f"transport: {str(exc)[:90]}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:120]}"
    return resp.json(), None


def check_symbol(http: httpx.Client, symbol: str, feed: str,
                 now: datetime, max_age: float) -> dict:
    row: dict = {"symbol": symbol, "feed": feed}

    quote, err = get(http, f"/{symbol}/quotes/latest", feed=feed)
    if err:
        row["quote"] = err
    else:
        q = (quote or {}).get("quote") or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        age = age_s(q.get("t"), now)
        row["bid"], row["ask"] = bid, ask
        row["quote_age_s"] = age
        row["spread_pct"] = (round((ask - bid) / ((ask + bid) / 2) * 100, 3)
                             if bid > 0 and ask > bid else None)
        row["fresh"] = age is not None and age <= max_age and bid > 0 and ask > 0

    trade, err = get(http, f"/{symbol}/trades/latest", feed=feed)
    if err:
        row["trade"] = err
    else:
        t = (trade or {}).get("trade") or {}
        row["last"] = t.get("p")
        row["trade_age_s"] = age_s(t.get("t"), now)

    bars, err = get(http, f"/{symbol}/bars", feed=feed, timeframe="1Min",
                    start=(now - timedelta(minutes=30)).isoformat(), limit=1000)
    if err:
        row["bars"] = err
    else:
        rows = (bars or {}).get("bars") or []
        row["bars_30m"] = len(rows)
        row["bar_age_s"] = age_s(rows[-1]["t"], now) if rows else None
    return row


def fmt(value, width=10, suffix="") -> str:
    if value is None:
        return "—".rjust(width)
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}".rjust(width)
    return f"{value}{suffix}".rjust(width)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--max-age-s", type=float, default=QUOTE_MAX_AGE_S)
    ap.add_argument("--write-doc", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        raise SystemExit("no Alpaca keys in .env — nothing to verify")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    now = datetime.now(timezone.utc)

    print(f"\n=== SIP PREFLIGHT — {now.astimezone(ET):%Y-%m-%d %H:%M:%S ET} ===")
    print(f"session: {session_label(now)}")
    print(f"configured stock_feed: {settings.alpaca_stock_feed}"
          f"   freshness gate: {args.max_age_s:.0f}s\n")

    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
               "APCA-API-SECRET-KEY": settings.alpaca_secret_key}
    results: dict[str, list[dict]] = {}
    with httpx.Client(headers=headers) as http:
        for feed in ("iex", "sip"):
            results[feed] = [check_symbol(http, s, feed, now, args.max_age_s)
                             for s in symbols]

    print(f"{'sym':<6}{'feed':<6}{'bid':>10}{'ask':>10}{'spread':>9}"
          f"{'quote age':>11}{'bar age':>10}{'bars/30m':>10}  verdict")
    print("-" * 88)
    for feed in ("iex", "sip"):
        for row in results[feed]:
            note = row.get("quote") if isinstance(row.get("quote"), str) else None
            verdict = ("ENTITLEMENT/ERROR" if note else
                       "FRESH" if row.get("fresh") else "STALE")
            print(f"{row['symbol']:<6}{feed:<6}{fmt(row.get('bid'))}"
                  f"{fmt(row.get('ask'))}"
                  f"{fmt(row.get('spread_pct'), 9, '%')}"
                  f"{fmt(row.get('quote_age_s'), 11, 's')}"
                  f"{fmt(row.get('bar_age_s'), 10, 's')}"
                  f"{fmt(row.get('bars_30m'), 10)}  {verdict}"
                  + (f"  {note}" if note else ""))

    sip_fresh = [r for r in results["sip"] if r.get("fresh")]
    sip_errors = [r for r in results["sip"] if isinstance(r.get("quote"), str)]
    iex_fresh = [r for r in results["iex"] if r.get("fresh")]

    print("\n--- verdict ---")
    if sip_errors:
        print("SIP REFUSED — real-time SIP is not entitled on this key.")
        print(f"  {sip_errors[0]['quote']}")
        print("  Subscribe (Algo Trader Plus, $99/mo) at app.alpaca.markets ->")
        print("  Market Data Subscriptions, then re-run this script.")
        ok = False
    elif not sip_fresh:
        print(f"SIP reachable but NO quote is younger than {args.max_age_s:.0f}s.")
        print("  Expected outside trading sessions — re-run inside 16:00-20:00 ET")
        print("  on a weekday before concluding anything.")
        ok = False
    else:
        print(f"SIP FRESH on {len(sip_fresh)}/{len(symbols)} symbols "
              f"(IEX fresh on {len(iex_fresh)}/{len(symbols)}).")
        print("  Safe to flip: SYSTEM menu -> stock_feed = sip, or")
        print("  PUT /api/settings/feed {\"stock_feed\": \"sip\"} — then RESTART the")
        print("  engine (streams are constructed once at boot) before 15:00 ET.")
        ok = True

    if args.write_doc:
        stamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
        doc = (Path(__file__).resolve().parent.parent.parent / "docs" / "notes"
               / f"sip_preflight_{stamp}.md")
        lines = [f"# SIP preflight — {stamp} ET", "",
                 f"session: {session_label(now)} · configured feed: "
                 f"{settings.alpaca_stock_feed} · gate {args.max_age_s:.0f}s",
                 f"verdict: {'ENTITLED + FRESH' if ok else 'NOT READY'}", "",
                 "| sym | feed | bid | ask | spread | quote age | bars/30m |",
                 "|---|---|---|---|---|---|---|"]
        for feed in ("iex", "sip"):
            for r in results[feed]:
                lines.append(
                    f"| {r['symbol']} | {feed} | {r.get('bid', '—')} | "
                    f"{r.get('ask', '—')} | {r.get('spread_pct', '—')} | "
                    f"{r.get('quote_age_s', '—')} | {r.get('bars_30m', '—')} |")
        doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwrote {doc}")

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
