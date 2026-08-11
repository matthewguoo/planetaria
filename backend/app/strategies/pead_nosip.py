"""PEAD flagship, sighted through public prints instead of the SIP.

THE EXPERIMENT. The flagship's one unproven link is the fill: no after-hours
entry has ever filled on this account, because the free tier goes blind at
17:00 ET while the venues stay open (phase 15, §2.1). The paid fix is a SIP
subscription. This class is the unpaid one: same policy line for line — same
watchlist, gate, verdict, fade, slots, conditional exit — with exactly one
seam overridden, the pricing quote. Where the flagship reads the broker feed
and correctly refuses to act blind, this variant prices from the freshest of
the broker feed and public last-trade prints (Yahoo's chart tail, Finnhub's
quote; see services/ah_quotes.py), so a release at 17:40 gets a decision
instead of a `skip: no fresh quote`.

WHAT A PRINT CANNOT TELL YOU, AND WHAT IS DONE ABOUT IT. These sources carry
no NBBO — bid == ask == last, so the spread veto has nothing to veto and the
entry limit sits AT the last print (which is the paper's own entry
definition: the reaction print). Whether that limit would actually have
filled, and at what true cost, is measured rather than assumed: `verify_min`
minutes after every decision — long enough for the free tier's 15-minute SIP
delay to lapse — the strategy fetches the consolidated NBBO as of the entry
instant (market.sip_quote_asof) and journals a `fill_check`: the real
bid/ask, the spread, how far the acted-on print sat from the true mid, and
whether a limit there was marketable. That journal is the AMC fill
simulation the paper could not run, produced by the live pipeline itself,
in note-mode and order-mode alike.

Run it in note-mode first (live: false, the default) exactly like the
flagship; the twin and the fill_check rows are the evidence that decides
whether order-mode is justified. `requires` drops "sip" — that is the whole
point — and keeps "shorts": never standing down fades roughly a third of
events, and a long-only account turns those into journaled refusals.
"""

import time as _time
from datetime import datetime, timezone

from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import StrategyContext, TradeIntent
from app.strategies.pead_flagship import PeadFlagship

# The free tier refuses SIP queries younger than 15 minutes; 16 is the floor
# with one minute of clock-skew margin.
SIP_DELAY_FLOOR_MIN = 16.0


@register
class PeadNoSip(PeadFlagship):
    """PEAD flagship priced from public AH prints (no SIP entitlement), with every fill assumption verified against delayed SIP after the fact."""

    kind = "pead_nosip"
    requires = frozenset({"shorts", "llm"})

    # Frozen at registration (8cfb36c). The metric is DRIFT HIT RATE on
    # non-neutral verdicts — deliberately not P&L (a single reaction is
    # hundreds of bp of noise) and not tape agreement (healthy regardless).
    # The engine does not compute it yet (needs verdict-vs-next-session
    # joins); the ladder card says "not yet measured" rather than falling
    # back to a yardstick the pre-reg did not name. ~200 non-neutral
    # verdicts (2-3 earnings seasons) separate 55% from chance at z~2.
    registered = {
        "doc": "docs/pre-registration-nosip.md",
        "registered_commit": "8cfb36c",
        "source_note": "docs/report.html (the PEAD paper); Fable panel 2026-08-09",
        "window": "in-corpus reference: 57.6% (Opus 5, n=1,787)",
        "metric": "drift_hit_rate_nonneutral",
        "metric_label": "% drift hit rate (non-neutral verdicts)",
        "band": [55.0, 100.0],
        "verdict_target": 200,
        "backtest": {"value": 57.6, "basis": "in-corpus; forward events are the control"},
        "costs_assumed": "23.2bp round trip; fill_check journals measure it live",
        "ladder": [{"stage": "note-mode", "sessions": None},
                   {"stage": "live forward test", "sessions": None}],
    }

    default_params = {
        **PeadFlagship.default_params,
        # How old a print may be and still count as a price. The flagship's
        # own gates use 120s; this is the same number made a param so the
        # shadow season can measure how binding it is.
        "quote_max_age_s": 120.0,
        # Minutes after a decision to fetch the delayed-SIP truth. Floor is
        # the entitlement's own delay; later is fine, earlier is impossible.
        "verify_min": 16.0,
    }

    def __init__(self):
        super().__init__()
        # key -> {due_mono, symbol, side, ref_px, src, entry_utc, ids, live}
        self._fill_checks: dict[str, dict] = {}

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not (10.0 <= float(clean["quote_max_age_s"]) <= 600.0):
            raise ValueError("quote_max_age_s must be 10-600 seconds")
        if not (SIP_DELAY_FLOOR_MIN <= float(clean["verify_min"]) <= 120.0):
            raise ValueError(
                f"verify_min must be {SIP_DELAY_FLOOR_MIN:.0f}-120 minutes — "
                f"the free tier's SIP window opens 15 minutes behind the tape")
        return clean

    # ------------------------------------------------------------- the seam

    async def _quote(self, sym: str, ctx: StrategyContext) -> dict | None:
        """Broker feed while it is fresh, public prints where it is dark.
        Everything downstream — the 120s age gates, the spread veto, the
        risk stack's per-symbol tape age — reads the result identically to
        a broker quote, with `src` carrying the provenance into the
        decision journal."""
        return await ctx.market.ah_quote(
            sym, max_age_s=float(ctx.params["quote_max_age_s"]))

    # ----------------------------------------------------- fill verification

    def _intent(self, sym: str, side: int, px: float, quote: dict,
                notional: float, hold_days: int, params: dict,
                signal_ids: tuple[int, ...]) -> TradeIntent:
        intent = super()._intent(sym, side, px, quote, notional, hold_days,
                                 params, signal_ids)
        # Every decision that reached sizing gets a truth check, note-mode
        # included: the question "what was the real book at the moment we
        # acted on a print" is the same whether an order followed or not.
        self._fill_checks[f"{sym}:{self._watch_date}"] = {
            "symbol": sym,
            "side": side,
            "limit": float(intent.entry_limit),
            "src": str(quote.get("src") or "unknown"),
            "quote_ts": quote.get("ts"),
            "entry_utc": datetime.now(timezone.utc),
            "due_mono": _time.monotonic() + float(params["verify_min"]) * 60.0,
            "ids": tuple(signal_ids),
            "live": bool(params["live"]),
        }
        return intent

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        await super().on_event(event, ctx)
        if event.type == "timer" and event.payload.get("kind") == "tick":
            await self._verify_due(ctx)

    async def _verify_due(self, ctx: StrategyContext) -> None:
        now = _time.monotonic()
        due = [k for k, v in self._fill_checks.items() if v["due_mono"] <= now]
        for key in due:
            check = self._fill_checks.pop(key)
            sym, side = check["symbol"], check["side"]
            ref = abs(float(check["limit"]))
            sip = None
            try:
                sip = await ctx.market.sip_quote_asof(sym, check["entry_utc"])
            except Exception as exc:                       # noqa: BLE001
                ctx.log.warning("fill_check SIP lookup failed for %s: %s",
                                sym, exc)
            base = {
                "symbol": sym, "side": side, "limit": round(ref, 4),
                "quote_src": check["src"],
                "entry_utc": check["entry_utc"].isoformat(),
                "mode": "live" if check["live"] else "note",
            }
            if not sip or not sip.get("bid") or not sip.get("ask"):
                await ctx.note(
                    {"fill_check": base,
                     "why": "delayed-SIP NBBO unavailable for the entry "
                            "minute — verification skipped, not assumed"},
                    signal_ids=check["ids"])
                continue
            bid, ask = float(sip["bid"]), float(sip["ask"])
            mid = (bid + ask) / 2
            # A long limit at `ref` lifts immediately only if the true ask
            # was at or under it; a short hits only if the true bid was at
            # or over. Everything else would have rested — maybe filled
            # later, maybe not, which is exactly what the shadow season is
            # for measuring.
            marketable = (ask <= ref) if side > 0 else (bid >= ref)
            await ctx.note(
                {"fill_check": {
                    **base,
                    "sip_bid": round(bid, 4), "sip_ask": round(ask, 4),
                    "sip_spread_bp": round((ask - bid) / mid * 1e4, 1)
                    if mid > 0 else None,
                    "print_vs_mid_bp": round((ref - mid) / mid * 1e4 * side, 1)
                    if mid > 0 else None,
                    "marketable_at_limit": bool(marketable),
                }},
                signal_ids=check["ids"])
