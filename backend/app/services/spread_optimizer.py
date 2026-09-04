"""Spread optimizer: work an order INSIDE the bid/ask instead of paying it.

Everything the engine prices is on the signed position-value axis (net
premium per share: debits positive, credits negative). On that axis the
"marketable" direction is the same for every structure:

    ENTRY  -> UP   (pay a higher debit / accept a smaller credit)
    EXIT   -> DOWN (accept a lower value for what is held)

so one pair of functions prices every shape. Offsets are expressed in
HALF-SPREADS of the whole position (sum over legs of |weight| x leg
half-spread, the same number `fair_value.position_quote_stats` returns):
0 = the mid, 1.0 = the touch (the ask for a long entry, the bid for a long
exit), <0 = better than mid (a bid inside the book). Rounding always goes
in the marketable direction so a rung never lands on the wrong side of a
tick boundary and quietly stops being marketable.

Entry: start at `start` half-spreads, step by `step` every `step_s`
seconds until `max` (default the touch), then rest there until the entry
TTL cancels it. Each step re-reads the live book, so the ladder tracks a
moving market instead of chasing a vanished price — bounded by the same
drift tolerance `place_trade` applies to a staged limit.

Exit: rungs at fractions of the half-spread (mid -> inside -> touch), then
market — the same shape as the legacy mid-2%/mid-6%/market ladder but
priced off the book that is actually there. On a penny-wide 0DTE SPY book
the legacy 2% rung was already through the bid; on a $0.30-wide wing it
never reached it. Half-spread units fix both.
"""

import math
from dataclasses import dataclass

TICK = 0.01

# Risk-settings keys (defaults live in risk.DEFAULT_RISK; bounds there too).
SETTING_KEYS = (
    "spread_optimizer",
    "spread_opt_step_s",
    "spread_opt_entry_start",
    "spread_opt_entry_step",
    "spread_opt_entry_max",
    "spread_opt_exit_max",
)

# Exit rungs as fractions of `spread_opt_exit_max` half-spreads; None =
# market. Waits come from `spread_opt_step_s` (the last rung waits a
# little longer: the touch is where most fills land).
EXIT_RUNG_FRACS: list[float | None] = [0.25, 0.5, 1.0, None]
EXIT_LAST_WAIT_MULT = 1.5


def _ceil_tick(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def _floor_tick(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


def _nonzero(x: float, toward_up: bool) -> float:
    """round_tick's invariant: a limit is never exactly 0.00."""
    if x == 0.0:
        return TICK if toward_up else -TICK
    return x


def entry_limit(mid: float, half_spread: float, frac: float) -> float:
    """Entry price `frac` half-spreads from the mid, rounded UP to a tick
    (the marketable direction for an entry)."""
    return _nonzero(_ceil_tick(mid + frac * max(half_spread, 0.0)), True)


def exit_limit(mid: float, half_spread: float, frac: float) -> float:
    """Exit price `frac` half-spreads below the mid, rounded DOWN to a tick
    (the marketable direction for a close)."""
    return _nonzero(_floor_tick(mid - frac * max(half_spread, 0.0)), False)


def exit_ladder(step_s: float, exit_max: float) -> list[tuple[float | None, float]]:
    """(half-spread fraction | None=market, wait_after_s) rungs for the
    enforcer's ladder loop — same shape as ESCALATION."""
    rungs: list[tuple[float | None, float]] = []
    for i, f in enumerate(EXIT_RUNG_FRACS):
        if f is None:
            rungs.append((None, 0.0))
            continue
        last_limit = i + 1 < len(EXIT_RUNG_FRACS) and EXIT_RUNG_FRACS[i + 1] is None
        rungs.append((f * exit_max, step_s * (EXIT_LAST_WAIT_MULT if last_limit else 1.0)))
    return rungs


@dataclass(frozen=True)
class EntryWork:
    """The chase plan stamped on a trade plan at placement (pricing JSON)."""

    staged: float      # the limit the trader saw and approved (drift anchor)
    start: float       # half-spreads from mid for rung 0
    step: float        # half-spreads added per rung
    max: float         # never worked past this many half-spreads
    step_s: float      # seconds a rung rests before the next
    frac: float        # the CURRENT rung's offset
    rung: int          # how many replacements have been made

    @classmethod
    def from_settings(cls, cfg: dict, staged: float) -> "EntryWork":
        start = float(cfg.get("spread_opt_entry_start", 0.0))
        return cls(
            staged=staged,
            start=start,
            step=float(cfg.get("spread_opt_entry_step", 0.25)),
            max=float(cfg.get("spread_opt_entry_max", 1.0)),
            step_s=float(cfg.get("spread_opt_step_s", 3.0)),
            frac=min(start, float(cfg.get("spread_opt_entry_max", 1.0))),
            rung=0,
        )

    @property
    def exhausted(self) -> bool:
        return self.frac >= self.max - 1e-9

    def next(self) -> "EntryWork":
        return EntryWork(
            staged=self.staged, start=self.start, step=self.step, max=self.max,
            step_s=self.step_s, frac=min(self.frac + self.step, self.max),
            rung=self.rung + 1,
        )

    def to_json(self) -> dict:
        return {
            "staged": self.staged, "start": self.start, "step": self.step,
            "max": self.max, "step_s": self.step_s, "frac": self.frac,
            "rung": self.rung,
        }

    @classmethod
    def from_json(cls, d: dict) -> "EntryWork":
        return cls(
            staged=float(d["staged"]), start=float(d["start"]), step=float(d["step"]),
            max=float(d["max"]), step_s=float(d["step_s"]), frac=float(d["frac"]),
            rung=int(d["rung"]),
        )


def work_spread_enabled(pricing: dict | None, cfg: dict) -> bool:
    """Per-plan override (stamped at placement) wins over the global toggle."""
    if pricing and pricing.get("work_spread") is not None:
        return bool(pricing["work_spread"])
    return bool(cfg.get("spread_optimizer", False))
