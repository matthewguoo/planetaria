"""Machine power-state defenses for a software-enforced bracket.

The exit enforcer only enforces while this process runs. A Windows box that
sleeps with a position open takes the SL and the time stop down with it
(incident 2026-07-30: the machine slept through a 14:25 ET time stop and the
position was still open at 22:48). Two defenses:

- KeepAwake: hold Windows' ES_SYSTEM_REQUIRED execution state while any
  plan is open, so the OS does not idle-sleep on an open position. Display
  sleep stays allowed. A lid close or an explicit user sleep still wins —
  this blocks the idle timer, not the user. No-op off Windows (Docker/CI).
- wake_watchdog: detect that the event loop STOPPED anyway (sleep,
  hibernate, suspended VM) by comparing actual vs expected wake time, then
  reconcile immediately — the time-stop backstop and the parked-exit logic
  run on the first pass after wake instead of whenever the next interval
  happens to land.
"""

import asyncio
import logging
import sys
import time

log = logging.getLogger("app.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(keep_awake: bool) -> bool:
    """ES_CONTINUOUS is per-thread state: every call MUST come from the same
    thread (all callers run on the asyncio loop thread)."""
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if keep_awake else 0)
    return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0


class KeepAwake:
    def __init__(self, risk, interval_s: float = 30.0):
        self.risk = risk
        self.interval_s = interval_s
        self.supported = sys.platform == "win32"
        self.active = False

    def _apply(self, want: bool) -> None:
        if not _set_execution_state(want):
            log.warning("SetThreadExecutionState failed (keep-awake=%s)", want)
            return
        if want != self.active:
            log.warning(
                "keep-awake %s (%s)",
                "ON - system sleep inhibited" if want else "off - system may sleep",
                "open plans present" if want else "no open plans",
            )
        self.active = want

    async def run(self) -> None:
        if not self.supported:
            log.info(
                "keep-awake unavailable on %s - the machine can sleep on open "
                "positions; exits are then broker-resting orders only",
                sys.platform,
            )
            return
        try:
            while True:
                try:
                    # Any open plan needs the engine: entries want their TTL
                    # cancel, positions want TP/SL/time-stop enforcement.
                    self._apply(bool(await self.risk.open_plans()))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("keep-awake pass failed")
                await asyncio.sleep(self.interval_s)
        finally:
            # Never leave a dead process's claim standing (best effort: the
            # OS drops per-thread state with the thread anyway).
            if self.active:
                self._apply(False)


async def wake_watchdog(enforcer, interval_s: float = 5.0, threshold_s: float = 30.0) -> None:
    """Detect event-loop suspension (system sleep) and reconcile right away.

    Wall-clock based on purpose: whether time.monotonic() ticks through a
    given machine's sleep state is hardware-dependent, but a sleep always
    shows up as the wall clock jumping past the expected wake time."""
    while True:
        wall = time.time()
        await asyncio.sleep(interval_s)
        lost = time.time() - wall - interval_s
        if lost <= threshold_s:
            continue
        log.error(
            "event loop was suspended ~%.0fs (system sleep/hibernate?) - "
            "reconciling all plans now", lost,
        )
        try:
            await enforcer.reconcile_once()
        except Exception:
            log.exception("post-wake reconcile failed")
