"""Mechanical overnight earnings carry — the flagship minus the model.

The overnight decomposition (research/pead-llm-gate/notes/
overnight_decomp_mech_*.md) measured what the tape alone is worth: on
after-hours reporters whose reaction is UP, the carry from the reaction
print to the next session's open is positive with no verdict anywhere in
the loop — the same clientele mechanism as everything else in the scan
(speculative repricing happens overnight; the open is the exit, never the
entry). The down side is not traded: its carry needs a short and the
engine gates overnight short entries for unbounded-gap reasons.

This class is PeadFlagship with the LLM deleted rather than a rewrite:
identical watchlist (top-N AMC reporters by prior-session dollar volume),
identical re-anchor and confirmation delay, identical slot allocator —
the decision is `move_pct >= gate and move_pct > 0`, and the exit is the
NEXT OPEN (09:31 time stop), not the close. requires = {"sip"}: the entry
is an after-hours fill, which is the same billing gate as the flagship.

Run in note-mode until the clean-anchor full-panel study
(research_overnight_decomp mech2) has confirmed the restricted-sample
number, and pre-register before any live decision.
"""

from app.services.signals.events import Event  # noqa: F401  (subscriptions doc)
from app.strategies import register
from app.strategies.base import StrategyContext
from app.strategies.pead_flagship import (
    QUOTE_MAX_AGE_S,
    PeadFlagship,
    _exit_open_time,
    _intent_dict,
    _spread_pct,
    _usable_price,
)


@register
class MechCarry(PeadFlagship):
    """Long the after-hours earnings pop, sell the next open. No model, no verdict — the tape decides and the open pays."""

    kind = "mech_carry"
    requires = frozenset({"sip"})     # no llm, no shorts — long carry only

    default_params = {
        **PeadFlagship.default_params,
        # The mechanical gate is lower than the flagship's 5%: the carry was
        # measured at the 2% gate (t 3.4 on the clean-anchor subset), and a
        # verdictless strategy has no second filter to pay for a higher one.
        "min_move_pct": 2.0,
        "min_dv_musd": 100.0,
    }

    async def _decide_text(self, sym: str, text: str, event_ids, source: str,
                           ctx: StrategyContext) -> None:
        """The whole decision: fresh price, signed move vs the anchor, done.
        Overrides the flagship's LLM call site; everything upstream
        (watchlist, re-anchor, eligibility, confirmation delay) and
        downstream (slots, sizing, note-mode) is inherited."""
        key = f"{sym}:{self._watch_date}"
        self._decided.add(key)
        watch = self._watch[sym]

        quote = await self._quote(sym, ctx)
        px = _usable_price(quote, max_age_s=QUOTE_MAX_AGE_S)
        if px is None:
            self._decided.discard(key)
            await ctx.note({"skip": f"no fresh quote for {sym} — this is the "
                                    "SIP requirement, not a bug"},
                           signal_ids=event_ids)
            return
        move_pct = (px / watch["px0"] - 1) * 100
        min_move = float(ctx.params["min_move_pct"])

        detail = {
            "symbol": sym, "source": source, "px0": watch["px0"], "px": px,
            "move_pct": round(move_pct, 2),
            "dv_musd": round(watch.get("dv", 0.0) / 1e6, 1),
            "spread_pct": _spread_pct(quote),
            "rule": "mechanical: long the up-tape, exit next open",
        }
        if move_pct < min_move:
            why = (f"reaction {move_pct:+.2f}% below the +{min_move:.1f}% gate"
                   if move_pct >= 0 else
                   f"down reaction {move_pct:+.2f}% — the down-side carry "
                   f"needs an overnight short, which the engine gates")
            await ctx.note({"no_trade": detail, "why": why},
                           signal_ids=event_ids)
            return

        spread = detail.get("spread_pct")
        cap = float(ctx.params["max_spread_pct"])
        if spread is not None and spread > cap:
            await ctx.note({"veto": detail,
                            "why": f"AH book too wide: {spread:.2f}% > "
                                   f"{cap:.2f}% cap"},
                           signal_ids=event_ids)
            return

        try:
            book = await ctx.account()
        except Exception as exc:                              # noqa: BLE001
            await ctx.note({"skip": f"cannot size {sym}: {exc}"},
                           signal_ids=event_ids)
            return
        slots = int(ctx.params["slots"])
        allocated = float(book.get("equity") or 0.0)
        available = float(book.get("available") or 0.0)
        per_slot = allocated / slots if slots else 0.0
        detail["capital"] = {"allocated": round(allocated, 2),
                             "available": round(available, 2),
                             "per_slot": round(per_slot, 2), "slots": slots}
        if per_slot <= 0 or available < per_slot:
            await ctx.note({"no_slot": detail,
                            "why": f"all {slots} slots committed"},
                           signal_ids=event_ids)
            return

        intent = self._intent(sym, +1, px, quote, per_slot, 1, ctx.params,
                              tuple(event_ids))
        # The carry's exit is the OPEN, whatever the params say — that is
        # the measurement this strategy trades.
        intent.time_stop_utc = _exit_open_time()
        intent.reason = f"mech carry long {sym}, exit next open"
        intent.dedupe_key = f"mcarry:{sym}:{self._watch_date}"
        detail["intent"] = _intent_dict(intent)
        await ctx.note(
            {("decision" if bool(ctx.params["live"]) else "would_trade"):
             {**detail, "side": 1}},
            signal_ids=event_ids)
        await ctx.submit(intent)
        ctx.log.info("mech_carry long %s @ %.2f (%+.2f%%)", sym, px, move_pct)
