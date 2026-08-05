"""Machine power-state defenses for a software-enforced bracket.

The exit enforcer only enforces while this process runs. A Windows box that
sleeps with a position open takes the SL and the time stop down with it
(incident 2026-07-30: the machine slept through a 14:25 ET time stop and the
position was still open at 22:48). Two defenses:

- KeepAwake: hold Windows' ES_SYSTEM_REQUIRED execution state while any
  plan is open OR any strategy instance is running (added 2026-08-05: a
  note-mode watcher holds no plans, but a box that idle-sleeps at 15:00
  misses the calendar fetch, the watchlist tick, and the whole night).
  Display sleep stays allowed. A lid close or an explicit user sleep still
  wins — this blocks the idle timer, not the user. No-op off Windows.
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
    def __init__(self, risk, interval_s: float = 30.0, runner=None):
        self.risk = risk
        # Strategy runner is constructed AFTER KeepAwake in bootstrap;
        # assigned post-construction (app.state.keep_awake.runner = runner).
        self.runner = runner
        self.interval_s = interval_s
        self.supported = sys.platform == "win32"
        self.active = False

    def _wanted_reason(self) -> str | None:
        """Why the machine must stay awake right now, or None."""
        if self.runner is not None and getattr(self.runner, "_running", None):
            return "strategy instances running"
        return None

    def _apply(self, want: bool, reason: str | None) -> None:
        if not _set_execution_state(want):
            log.warning("SetThreadExecutionState failed (keep-awake=%s)", want)
            return
        if want != self.active:
            log.warning(
                "keep-awake %s (%s)",
                "ON - system sleep inhibited" if want else "off - system may sleep",
                reason if want else "no open plans, no running strategies",
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
                    # cancel, positions want TP/SL/time-stop enforcement. Any
                    # running strategy needs feeds and timer ticks live.
                    reason = ("open plans present"
                              if await self.risk.open_plans()
                              else self._wanted_reason())
                    self._apply(reason is not None, reason)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("keep-awake pass failed")
                await asyncio.sleep(self.interval_s)
        finally:
            # Never leave a dead process's claim standing (best effort: the
            # OS drops per-thread state with the thread anyway).
            if self.active:
                self._apply(False, None)


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
