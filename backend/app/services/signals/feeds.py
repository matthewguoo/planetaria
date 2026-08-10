"""DataFeed adapters: each is one supervised long-lived connection/loop that
normalizes an external source into Events on the bus. Strategies never know
which adapter produced an event — that is what makes data sources swappable
(FMP estimates, Kalshi quotes, ... all land here later with zero downstream
changes).

Push where the source supports it (Alpaca news WebSocket), polling only as
an adapter-internal detail. Never one feed per strategy.
"""

import asyncio
import logging
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .events import Event, EventBus
from .store import SignalStore

log = logging.getLogger("app.feeds")

ET = ZoneInfo("America/New_York")


class DataFeed(Protocol):
    name: str

    async def run(self) -> None:  # long-lived; raising -> supervise() backoff
        ...

    def status(self) -> dict:
        ...


def et_session(now: datetime | None = None) -> str:
    """Coarse ET session label carried on timer events — market_clock's
    verified 24/5 table, with the closed weekend gap labelled "weekend".

    This used to be a third hand-rolled clock, and it disagreed with the
    verified one at both week boundaries: Friday 20:00+ came back
    "overnight" while the market was closed, and Sunday 20:00+ came back
    "weekend" while the Blue Ocean session was open."""
    from app.services.market_clock import equity_session

    return equity_session(now) or "weekend"


class AlpacaNewsFeed:
    """Benzinga real-time news via the Alpaca WebSocket (entitlement verified
    on the paper keys 2026-08-04). key includes updated_at so a REVISED
    headline journals as a fresh event instead of deduping away."""

    name = "alpaca-news"

    def __init__(self, alpaca, bus: EventBus, store: SignalStore):
        self.alpaca = alpaca
        self.bus = bus
        self.store = store
        self._stream = None
        self.received = 0
        self.journaled = 0
        self.errors = 0
        self.last_event_mono: float | None = None
        self.connected_mono: float | None = None

    async def run(self) -> None:
        stream = self.alpaca.make_news_stream()
        stream.subscribe_news(self._on_news, "*")
        self._stream = stream
        self.connected_mono = time.monotonic()
        try:
            await stream._run_forever()
        finally:
            self.connected_mono = None
            with suppress(Exception):
                await stream.close()

    async def _on_news(self, news) -> None:
        # One malformed payload must not kill the socket: normalize, journal,
        # publish inside a blanket guard; failures count and log.
        try:
            self.received += 1
            self.last_event_mono = time.monotonic()
            created_at = getattr(news, "created_at", None) or datetime.now(timezone.utc)
            updated_at = getattr(news, "updated_at", None) or created_at
            event = Event(
                type="news",
                ts=created_at,
                source=self.name,
                key=f"{news.id}:{updated_at.isoformat()}",
                symbols=tuple(getattr(news, "symbols", None) or ()),
                payload={
                    "headline": getattr(news, "headline", "") or "",
                    "summary": getattr(news, "summary", "") or "",
                    "url": getattr(news, "url", None),
                    "author": getattr(news, "author", None),
                    "created_at": created_at.isoformat(),
                    "updated_at": updated_at.isoformat(),
                },
            )
            event, fresh = await self.store.record(event)
            if fresh:
                self.journaled += 1
                self.bus.publish(event)
        except Exception:
            self.errors += 1
            log.exception("news normalization failed (item %d)", self.received)

    def status(self) -> dict:
        return {
            "connected": self.connected_mono is not None,
            "received": self.received,
            "journaled": self.journaled,
            "errors": self.errors,
            "last_event_age_s": (
                round(time.monotonic() - self.last_event_mono, 1)
                if self.last_event_mono
                else None
            ),
        }


class TimerFeed:
    """Minute ticks + session-boundary events. Deliberately plain
    asyncio.sleep — this codebase runs zero schedulers and that stays true.
    Ticks are NOT journaled (see store.JOURNALED_TYPES): they carry no
    outside information."""

    name = "timer"

    def __init__(self, bus: EventBus, tick_s: float = 60.0):
        self.bus = bus
        self.tick_s = tick_s
        self.ticks = 0
        self._last_session: str | None = None

    async def run(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            session = et_session(now)
            if session != self._last_session and self._last_session is not None:
                self.bus.publish(Event(
                    type="timer", ts=now, source=self.name, key=None, symbols=(),
                    payload={"kind": "session_change",
                             "session": session, "from": self._last_session},
                ))
            self._last_session = session
            self.bus.publish(Event(
                type="timer", ts=now, source=self.name, key=None, symbols=(),
                payload={"kind": "tick", "session": session,
                         "et": now.astimezone(ET).isoformat()},
            ))
            self.ticks += 1
            await asyncio.sleep(self.tick_s)

    def status(self) -> dict:
        return {"ticks": self.ticks, "session": self._last_session,
                "tick_s": self.tick_s}
