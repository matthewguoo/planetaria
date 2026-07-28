"""In-process pub/sub hub for WebSocket fanout.

Single-process backend -> in-memory fanout is correct and fastest. Each WS
client owns a bounded queue; slow clients drop oldest messages rather than
back-pressuring the market data path.
"""

import asyncio
import logging
from collections import defaultdict

log = logging.getLogger("app.broadcast")

QUEUE_SIZE = 500


class Broadcaster:
    def __init__(self):
        self._topics: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, topic: str, queue: asyncio.Queue) -> None:
        self._topics[topic].add(queue)

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        self._topics[topic].discard(queue)
        if not self._topics[topic]:
            del self._topics[topic]

    def subscriber_count(self, topic: str) -> int:
        return len(self._topics.get(topic, ()))

    def publish(self, topic: str, message: dict) -> None:
        for queue in self._topics.get(topic, ()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop oldest, keep newest — stale ticks are worthless.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
