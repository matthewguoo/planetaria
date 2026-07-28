"""Shared supervision for long-running upstream connections: run forever,
reconnect with capped exponential backoff + jitter, optional post-reconnect
hook (e.g. bar gap-fill)."""

import asyncio
import logging
import random
from typing import Awaitable, Callable

log = logging.getLogger("app.supervise")


async def supervise(
    name: str,
    run: Callable[[], Awaitable[None]],
    on_reconnect: Callable[[], Awaitable[None]] | None = None,
    max_delay: float = 60.0,
) -> None:
    attempt = 0
    while True:
        try:
            log.info("%s connecting", name)
            await run()
            log.warning("%s exited cleanly", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("%s error: %s", name, exc)
        attempt += 1
        delay = min(max_delay, (2**attempt) + random.uniform(0, 1))
        log.info("%s reconnecting in %.1fs", name, delay)
        await asyncio.sleep(delay)
        if on_reconnect is not None:
            try:
                await on_reconnect()
            except Exception:
                log.exception("%s on_reconnect hook failed", name)
