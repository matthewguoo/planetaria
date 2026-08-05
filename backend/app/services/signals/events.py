"""Typed in-process event bus for the strategy data plane.

Deliberately separate from Broadcaster: quotes are lossy-by-design (a stale
tick is worthless, drop-oldest is correct), but a dropped NEWS event is a
missed trade. Bus subscribers default to lossless queues that refuse to shed
— an overflow drops the NEWEST message, counts it, and logs loudly, so
backpressure shows up in /api/system/state instead of as silent no-trading.

The bus is in-memory and never replayed across restarts on purpose: the
signals table is a journal, not a queue. Restart re-fires nothing.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("app.events")

DEFAULT_QUEUE_SIZE = 512


@dataclass(frozen=True, slots=True)
class Event:
    type: str                  # news | estimate | manual | timer | signal
    ts: datetime               # upstream event time, UTC
    source: str                # adapter name, e.g. "alpaca-news"
    key: str | None            # upstream dedupe id; None for unkeyed types
    symbols: tuple[str, ...]
    payload: dict
    signal_id: int | None = None   # journal row id, set before publish


@dataclass
class _Subscription:
    queue: asyncio.Queue
    lossless: bool
    dropped: int = 0


@dataclass
class _Topic:
    subs: list[_Subscription] = field(default_factory=list)
    published: int = 0


class EventBus:
    def __init__(self):
        self._topics: dict[str, _Topic] = defaultdict(_Topic)

    def subscribe(
        self, event_type: str, *, maxsize: int = DEFAULT_QUEUE_SIZE, lossless: bool = True
    ) -> asyncio.Queue:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._topics[event_type].subs.append(_Subscription(queue, lossless))
        return queue

    def unsubscribe(self, event_type: str, queue: asyncio.Queue) -> None:
        topic = self._topics.get(event_type)
        if topic is None:
            return
        topic.subs = [s for s in topic.subs if s.queue is not queue]
        if not topic.subs and not topic.published:
            del self._topics[event_type]

    def publish(self, event: Event) -> None:
        topic = self._topics.get(event.type)
        if topic is None:
            return
        topic.published += 1
        for sub in topic.subs:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.dropped += 1
                if sub.lossless:
                    # Refuse to shed silently: the consumer is wedged or too
                    # slow, and that is an incident, not a shrug.
                    log.error(
                        "LOSSLESS QUEUE FULL: dropped %s event from %s "
                        "(subscriber has dropped %d total) - consumer wedged?",
                        event.type, event.source, sub.dropped,
                    )
                else:
                    try:  # lossy: drop oldest, keep newest (Broadcaster rules)
                        sub.queue.get_nowait()
                        sub.queue.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def status(self) -> dict:
        return {
            event_type: {
                "published": topic.published,
                "subscribers": [
                    {"size": s.queue.qsize(), "max": s.queue.maxsize,
                     "dropped": s.dropped, "lossless": s.lossless}
                    for s in topic.subs
                ],
            }
            for event_type, topic in self._topics.items()
        }
