"""Reference strategy: the end-to-end integration proof, not alpha.

Buys a tiny share position at a marketable limit with an immediate bracket,
through the exact pipeline every real strategy will use — signal journal ->
decision journal -> budget -> global risk -> place_trade -> FSM -> exit
enforcer -> exec-quality ledger. One trigger produces one plan whose whole
life is reconstructible from persisted rows.

Runbook (paper):
  1. POST /api/strategies            {"kind": "ref_tick", "name": "ref-spy",
                                      "params": {"extended_hours": true}}
  2. POST /api/strategies/{id}/enable
  3. POST /api/strategies/{id}/trigger   {"payload": {}}
  4. Watch: GET /api/positions (plan appears, enforcer arms),
     GET /api/strategies/{id}/decisions (intent -> placed),
     GET /api/signals?type=manual (the trigger),
     GET /api/positions/{plan_id}/events (FSM journal).
  5. The bracket manages the exit (tp/sl/time stop ~30min).
Optional: params.schedule_et="HH:MM" fires once a day on the timer feed.
The dedupe key caps it at ONE placement per symbol per ET day regardless
of how many times the trigger is mashed.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent

ET = ZoneInfo("America/New_York")


@register
class RefTick(Strategy):
    """Tiny bracketed share buy on manual/scheduled trigger (integration proof)."""

    kind = "ref_tick"
    subscriptions = ("manual", "timer")
    default_params = {
        "symbol": "SPY",
        "qty": 1,
        "tp_pct": 0.003,        # +0.3%
        "sl_pct": 0.005,        # -0.5%
        "hold_min": 30,
        "extended_hours": True,
        "schedule_et": None,    # "HH:MM" to also fire on the timer feed
    }

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if clean["schedule_et"] is not None:
            from datetime import time as _time

            _time.fromisoformat(str(clean["schedule_et"]))
        if not (1 <= int(clean["qty"]) <= 100):
            raise ValueError("qty must be 1-100 shares (this is a proof, not a position)")
        for key in ("tp_pct", "sl_pct"):
            if not (0.0005 <= float(clean[key]) <= 0.05):
                raise ValueError(f"{key} must be between 0.05% and 5%")
        return clean

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        if event.type == "timer" and not self._schedule_hit(event, ctx.params):
            return

        symbol = str(ctx.params["symbol"]).upper()
        # Overnight-aware: inside the 20:00-04:00 ET session this subscribes
        # the symbol and polls the Blue Ocean tape (the REST book is the dead
        # 17:00 close there — observed 18 points off the live tape in the
        # 2026-08-04 e2e run); every other session it is the plain
        # staleness-aware REST quote.
        quote = await ctx.market.overnight_price(symbol)
        if not quote or not quote.get("ask"):
            await ctx.note({"skip": f"no usable quote for {symbol}"},
                           signal_ids=_ids(event))
            return
        # Freshness backstop: whatever the source, never price an entry off
        # a dead quote (e.g. a failed overnight poll, or the post-17:00 REST
        # book going stale between sessions).
        import time as _time

        quote_ts = float(quote.get("ts") or 0)
        age_s = (_time.time() * 1000 - quote_ts) / 1000 if quote_ts else None
        if age_s is None or age_s > 120:
            await ctx.note({"skip": f"quote for {symbol} is "
                                    f"{'unknown-age' if age_s is None else f'{age_s:.0f}s stale'}"
                                    " - refusing to price an entry off it"},
                           signal_ids=_ids(event))
            return
        # Marketable: cross to the ask — the exec-quality ledger then measures
        # exactly what that immediacy cost against the submit-time fair value.
        entry = round(float(quote["ask"]), 2)
        intent = TradeIntent(
            asset_class="equity",
            underlying=symbol,
            legs=[{"symbol": symbol, "side": 1, "ratio": 1, "entry": entry}],
            qty=int(ctx.params["qty"]),
            entry_limit=entry,
            tp=round(entry * (1 + float(ctx.params["tp_pct"])), 2),
            sl=round(entry * (1 - float(ctx.params["sl_pct"])), 2),
            time_stop_utc=datetime.now(timezone.utc)
            + timedelta(minutes=float(ctx.params["hold_min"])),
            extended_hours=bool(ctx.params["extended_hours"]),
            reason=f"ref_tick {event.type} trigger",
            signal_ids=tuple(_ids(event)),
            dedupe_key=f"ref:{symbol}:{datetime.now(ET).date().isoformat()}",
        )
        result = await ctx.submit(intent)
        ctx.log.info("ref_tick placed plan %s (%s @ %.2f)",
                     result.get("id"), symbol, entry)

    @staticmethod
    def _schedule_hit(event: Event, params: dict) -> bool:
        """Fire on the timer tick whose ET minute matches schedule_et (the
        daily dedupe key makes double-fires within the minute harmless)."""
        schedule = params.get("schedule_et")
        if not schedule or event.payload.get("kind") != "tick":
            return False
        et_iso = event.payload.get("et")
        if not et_iso:
            return False
        now_et = datetime.fromisoformat(et_iso)
        return now_et.strftime("%H:%M") == str(schedule)


def _ids(event: Event) -> list[int]:
    return [event.signal_id] if event.signal_id is not None else []
