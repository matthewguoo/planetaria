"""Shared supervision for long-running upstream connections: run forever,
reconnect with capped exponential backoff + jitter, optional post-reconnect
hook (e.g. bar gap-fill)."""

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

log = logging.getLogger("app.supervise")

HEALTHY_RUN_S = 120.0  # a run this long resets the backoff ladder


async def supervise(
    name: str,
    run: Callable[[], Awaitable[None]],
    on_reconnect: Callable[[], Awaitable[None]] | None = None,
    max_delay: float = 60.0,
) -> None:
    attempt = 0
    while True:
        started = time.monotonic()
        try:
            log.info("%s connecting", name)
            await run()
            log.warning("%s exited cleanly", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("%s error: %s", name, exc)
        # A connection that lived a while was healthy: reconnect fast instead
        # of inheriting stale backoff from startup hiccups.
        if time.monotonic() - started > HEALTHY_RUN_S:
            attempt = 0
        attempt += 1
        delay = min(max_delay, (2**attempt) + random.uniform(0, 1))
        log.info("%s reconnecting in %.1fs", name, delay)
        await asyncio.sleep(delay)
        if on_reconnect is not None:
            try:
                await on_reconnect()
            except Exception:
                log.exception("%s on_reconnect hook failed", name)
