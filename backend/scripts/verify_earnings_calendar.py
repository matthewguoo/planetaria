"""Empirical verification of the Finnhub earnings-calendar shape.

The EarningsCalendarFeed was built against the documented response shape
(2026-08-04) but the house rule is empirical proof before dependence. Run
this once FINNHUB_API_KEY is in the root .env, then update the dated
comment in app/services/signals/earnings_calendar.py with what you saw.

Prints: reporter counts for today..+7d, field coverage rates (how often
epsEstimate / revenueEstimate / hour are actually populated on the free
tier — the revenue-consensus quality question that decides whether to
propose paid FMP), and a few sample rows.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.signals.earnings_calendar import FINNHUB_URL  # noqa: E402

ET = ZoneInfo("America/New_York")


async def main() -> None:
    settings = get_settings()
    if not settings.finnhub_api_key:
        raise SystemExit(
            "FINNHUB_API_KEY is not set. Register (free) at "
            "https://finnhub.io/register, then add FINNHUB_API_KEY=... to "
            "the root .env and re-run."
        )
    today = datetime.now(ET).date()
    rows: list[dict] = []
    async with httpx.AsyncClient(
        headers={"X-Finnhub-Token": settings.finnhub_api_key}, timeout=20
    ) as http:
        # Day-by-day paging: wide windows silently drop their EARLIEST
        # dates once the response hits the 1500-row cap (verified
        # 2026-08-05 - a 7d peak-season query returned zero rows for its
        # own start date).
        for offset in range(0, 8):
            day = (today + timedelta(days=offset)).isoformat()
            resp = await http.get(FINNHUB_URL, params={"from": day, "to": day})
            if offset == 0:
                print(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            rows.extend((resp.json() or {}).get("earningsCalendar") or [])

    print(f"{len(rows)} reporters {today}..+7d")
    by_date = Counter(r.get("date") for r in rows)
    for date, count in sorted(by_date.items()):
        print(f"  {date}: {count}")
    hours = Counter(str(r.get("hour")) for r in rows)
    print(f"hour values: {dict(hours)}")

    def coverage(key: str) -> str:
        have = sum(1 for r in rows if r.get(key) is not None)
        return f"{have}/{len(rows)} ({have / max(1, len(rows)):.0%})"

    print(f"epsEstimate coverage:     {coverage('epsEstimate')}")
    print(f"revenueEstimate coverage: {coverage('revenueEstimate')}")
    print(f"quarter/year coverage:    {coverage('quarter')} / {coverage('year')}")
    print("\nsample rows:")
    for row in rows[:5]:
        print(f"  {row}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
