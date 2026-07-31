"""Market-hours truth from the broker clock, cached and extrapolated.

Why this exists (incident 2026-07-30): the machine slept through a time
stop; on wake at ~22:48 ET the exit ladder fired into a CLOSED market and
churned cancel/replace limit orders all night — limits can't fill when the
market is closed, and market orders are rejected. Every exit path now asks
this clock first and PARKS a single resting order instead (see
ExitEnforcer).

Policy: FAIL OPEN. If the clock cannot be fetched (API error, keyless), the
market is reported open, because the failure mode of wrongly assuming
"closed" is an exit that never enforces — strictly worse than some
after-hours order churn.

One REST fetch per session regime: the snapshot carries next_open/next_close
wall-clock boundaries, so between fetches openness is derived locally and a
fetch is only needed after a boundary is crossed. Wall clock (not monotonic)
on purpose — after a system sleep the wall clock is exactly the truth we
want to compare against.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("app.clock")


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class ClockSnapshot:
    is_open: bool
    next_open: datetime | None
    next_close: datetime | None
    fetched_at: datetime

    def current_state(self, now: datetime) -> bool | None:
        """Openness at `now`, or None when `now` has crossed the snapshot's
        boundary and the snapshot can no longer answer."""
        if self.is_open:
            if self.next_close is not None and now < self.next_close:
                return True
            return None
        if self.next_open is not None and now < self.next_open:
            return False
        return None


class MarketClock:
    # After a failed fetch, don't re-hit the broker for this long — exits
    # consult the clock on their hot path, and a dead API must fail open
    # FAST, not stack a 15s+ fetch timeout in front of every ladder.
    FAIL_CACHE_S = 30.0

    def __init__(self, alpaca):
        self.alpaca = alpaca
        self.snapshot: ClockSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_fail_log = 0.0
        self._fail_until = 0.0

    async def is_open(self) -> bool:
        """True when the market is open for trading. Fail-open: keyless or
        broker-unreachable reports open so exits keep enforcing."""
        if not getattr(self.alpaca, "configured", False):
            return True
        now = datetime.now(timezone.utc)
        snap = self.snapshot
        if snap is not None:
            state = snap.current_state(now)
            if state is not None:
                return state
        snap = await self._refresh()
        if snap is None:
            return True  # fail open
        state = snap.current_state(now)
        return True if state is None else state

    async def next_open(self) -> datetime | None:
        """Next session open (None when unknown). Meaningful while closed."""
        if not getattr(self.alpaca, "configured", False):
            return None
        snap = self.snapshot
        now = datetime.now(timezone.utc)
        if snap is None or snap.current_state(now) is None:
            snap = await self._refresh()
        if snap is None or snap.is_open:
            return None
        return snap.next_open

    async def _refresh(self) -> ClockSnapshot | None:
        import time as _time

        if _time.monotonic() < self._fail_until:
            return None  # recent failure — fail open without a broker hit
        async with self._lock:
            # A concurrent caller may have refreshed while we waited.
            snap = self.snapshot
            now = datetime.now(timezone.utc)
            if snap is not None and snap.current_state(now) is not None:
                return snap
            if _time.monotonic() < self._fail_until:
                return None
            try:
                clock = await self.alpaca.call(
                    self.alpaca.trading.get_clock, retries=1
                )
            except Exception as exc:
                self._fail_until = _time.monotonic() + self.FAIL_CACHE_S
                if _time.monotonic() - self._last_fail_log > 60:
                    self._last_fail_log = _time.monotonic()
                    log.warning("market clock fetch failed (failing OPEN): %s", exc)
                return None
            self._fail_until = 0.0
            snap = ClockSnapshot(
                is_open=bool(clock.is_open),
                next_open=_as_utc(getattr(clock, "next_open", None)),
                next_close=_as_utc(getattr(clock, "next_close", None)),
                fetched_at=now,
            )
            self.snapshot = snap
            log.info(
                "market clock: %s (next_open=%s next_close=%s)",
                "OPEN" if snap.is_open else "CLOSED", snap.next_open, snap.next_close,
            )
            return snap

    def status(self) -> dict:
        """Introspection for /api/system/state."""
        snap = self.snapshot
        if snap is None:
            return {"known": False}
        now = datetime.now(timezone.utc)
        state = snap.current_state(now)
        return {
            "known": state is not None,
            "is_open": state,
            "next_open": snap.next_open.isoformat() if snap.next_open else None,
            "next_close": snap.next_close.isoformat() if snap.next_close else None,
        }
