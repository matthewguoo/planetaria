"""Redis wrapper with graceful degradation.

The backend is a single process, so the in-process caches in BarStore are
authoritative during a run. Redis is the persistence layer that survives
restarts. If Redis is unreachable we keep running (loudly) rather than dying:
every operation becomes a no-op and hydration falls back to REST backfill.
"""

import logging

import redis.asyncio as aioredis

log = logging.getLogger("app.redis")


class RedisFacade:
    def __init__(self, url: str):
        self._url = url
        self._client: aioredis.Redis | None = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def connect(self) -> None:
        try:
            self._client = aioredis.from_url(
                self._url, decode_responses=True, socket_connect_timeout=1
            )
            await self._client.ping()
            self._healthy = True
            log.info("redis connected: %s", self._url)
        except Exception as exc:
            self._client = None
            self._healthy = False
            log.error("REDIS UNAVAILABLE (%s) - running without persistence", exc)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _guard(self, make_coro, default=None):
        """make_coro is a zero-arg callable so nothing touches self._client
        when it is None (a bare coroutine argument would be constructed —
        and explode — before this check could run)."""
        if not self._client:
            return default
        try:
            result = await make_coro()
            self._healthy = True
            return result
        except Exception as exc:
            if self._healthy:
                log.error("redis operation failed (%s) - degrading to memory-only", exc)
            self._healthy = False
            return default

    # --- bars: hash of ts->json per (symbol, timeframe) ---------------------

    def _bars_key(self, symbol: str, tf: str) -> str:
        return f"bars:{symbol}:{tf}"

    async def save_bars(self, symbol: str, tf: str, bars: dict[str, str]) -> None:
        if not bars:
            return
        key = self._bars_key(symbol, tf)
        await self._guard(lambda: self._client.hset(key, mapping=bars))

    async def load_bars(self, symbol: str, tf: str) -> dict[str, str]:
        key = self._bars_key(symbol, tf)
        return await self._guard(lambda: self._client.hgetall(key), {})

    async def trim_bars(self, symbol: str, tf: str, keep_ts: list[str]) -> None:
        """Delete fields not in keep_ts (bounded retention)."""
        key = self._bars_key(symbol, tf)
        existing = await self._guard(lambda: self._client.hkeys(key), [])
        stale = [ts for ts in existing if ts not in set(keep_ts)]
        if stale:
            await self._guard(lambda: self._client.hdel(key, *stale))

