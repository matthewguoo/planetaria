"""Earnings calendar + consensus feed (Finnhub free tier).

Once daily at ~15:00 ET (and on boot) fetches today's + tomorrow's earnings
calendar and journals one `estimate` signal per reporter, keyed
cal:{symbol}:{date} so re-polls and restarts dedupe in the journal.

Delivery is AT-LEAST-ONCE on purpose, unlike the news feeds: a deduped
re-poll is REPUBLISHED to the bus (with its original signal_id) even though
it journals nothing. Estimates are watchlist data, not trade triggers — a
strategy that restarts mid-afternoon must still hear tonight's reporters on
the next fetch, and an idempotent watchlist add makes duplicates harmless.
News replays stay swallowed (AlpacaNewsFeed/EdgarFeed) because news drives
orders and replay there is a stale-trade hazard.

Response shape per Finnhub docs 2026-08-04 (free tier, 60 calls/min):
GET /api/v1/calendar/earnings?from=&to= with X-Finnhub-Token header ->
{"earningsCalendar": [{"date", "epsEstimate", "epsActual", "hour":
"bmo"|"amc"|"dmh", "quarter", "revenueEstimate", "revenueActual", "symbol",
"year"}]}. EMPIRICAL VERIFICATION PENDING a key in .env — run
scripts/verify_earnings_calendar.py once FINNHUB_API_KEY is set, then
update this comment with the date.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .events import Event, EventBus
from .store import SignalStore

log = logging.getLogger("app.feeds.earnings")

ET_TZ = ZoneInfo("America/New_York")
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"

FETCH_ET = (15, 0)  # daily fetch time: 15:00 ET, 30min before watchlist build
WHEN_LABELS = {"bmo": "bmo", "amc": "amc", "dmh": "dmh"}


def seconds_until_next_fetch(now: datetime | None = None) -> float:
    """Next 15:00 ET strictly in the future. Weekends fetch too: one call a
    day is free and keeps Monday's calendar visible over the weekend."""
    now = (now or datetime.now(ET_TZ)).astimezone(ET_TZ)
    target = now.replace(hour=FETCH_ET[0], minute=FETCH_ET[1],
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


@dataclass
class EarningsCalendarFeed:
    bus: EventBus
    store: SignalStore
    settings: object

    name: str = field(default="earnings-cal", init=False)
    fetches: int = field(default=0, init=False)
    reporters_last_fetch: int = field(default=0, init=False)
    journaled: int = field(default=0, init=False)
    published: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)
    auth_failed: bool = field(default=False, init=False)
    last_fetch_mono: float | None = field(default=None, init=False)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"X-Finnhub-Token":
                     getattr(self.settings, "finnhub_api_key", "")},
            timeout=20.0,
        )

    async def run(self) -> None:  # long-lived; raising -> supervise() backoff
        async with self._client() as http:
            while True:
                try:
                    await self._fetch_once(http)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in (401, 403):
                        # A bad key never self-heals; hammering Finnhub with
                        # supervise() retries all day would be pointless.
                        self.errors += 1
                        self.auth_failed = True
                        log.error("finnhub auth failed (%d) - calendar feed "
                                  "idle until next daily fetch", code)
                    else:
                        raise  # 429/5xx: supervise() backoff is the retry
                await asyncio.sleep(seconds_until_next_fetch())

    async def _fetch_once(self, http: httpx.AsyncClient) -> None:
        today = datetime.now(ET_TZ).date()
        resp = await http.get(FINNHUB_URL, params={
            "from": today.isoformat(),
            "to": (today + timedelta(days=1)).isoformat(),
        })
        resp.raise_for_status()
        self.auth_failed = False
        self.fetches += 1
        self.last_fetch_mono = time.monotonic()
        rows = (resp.json() or {}).get("earningsCalendar") or []
        self.reporters_last_fetch = 0
        for row in rows:
            # One malformed reporter must not hide the rest.
            try:
                await self._journal_reporter(row)
                self.reporters_last_fetch += 1
            except Exception:
                self.errors += 1
                log.exception("earnings calendar row unusable: %r", row)
        log.info("earnings calendar: %d reporters for %s..+1d",
                 self.reporters_last_fetch, today)

    async def _journal_reporter(self, row: dict) -> None:
        symbol = str(row["symbol"]).strip().upper()
        date = str(row["date"]).strip()
        if not symbol or not date:
            raise ValueError("reporter missing symbol/date")
        quarter, year = row.get("quarter"), row.get("year")
        event = Event(
            type="estimate",
            ts=datetime.now(timezone.utc),
            source=self.name,
            key=f"cal:{symbol}:{date}",
            symbols=(symbol,),
            payload={
                "symbol": symbol,
                "date": date,
                "when": WHEN_LABELS.get(str(row.get("hour") or "").lower(),
                                        "unknown"),
                "eps_consensus": row.get("epsEstimate"),
                "revenue_consensus": row.get("revenueEstimate"),
                "fiscal_period": (f"Q{quarter} {year}"
                                  if quarter and year else None),
            },
        )
        event, fresh = await self.store.record(event)
        if fresh:
            self.journaled += 1
        self.published += 1
        self.bus.publish(event)  # at-least-once: see module docstring

    def status(self) -> dict:
        return {
            "configured": bool(getattr(self.settings, "finnhub_api_key", "")),
            "auth_failed": self.auth_failed,
            "fetches": self.fetches,
            "reporters_last_fetch": self.reporters_last_fetch,
            "journaled": self.journaled,
            "published": self.published,
            "errors": self.errors,
            "last_fetch_age_s": (
                round(time.monotonic() - self.last_fetch_mono, 1)
                if self.last_fetch_mono else None
            ),
        }
