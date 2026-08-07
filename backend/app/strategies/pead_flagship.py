"""PEAD flagship — the policy the paper concludes for.

Working Note PR-2026-01, Table 1. This is NOT the configuration the study
reports as its headline gate; it is the one the study argues for after
measuring that the gate spends its verdicts the wrong way and that the exit
rule matters more than the gate does.

    universe      AMC reporters, top `n_names` per night by prior-session
                  dollar volume, floor `min_dv_musd`/day                §3
    event gate    |reaction| >= `min_move_pct` in the fifteen minutes after
                  the release, measured against the official close    §2.1, §4.5
    decision      one model call on the release text, no tools, no
                  retrieval, and the tape's move is NOT in the prompt   §4.1
    direction     with the tape where the verdict agrees; AGAINST the tape
                  where it disagrees or is neutral — never stand down    §5.4
    entry         the post-auction print, after a confirmation delay     §4.5
    exit          the next session's close; the THIRD session's close
                  where the verdict reports guidance raised or lowered   §5.3
    book          `slots` concurrent positions, equal weight, contested
                  slot to the highest prior-session dollar volume        §5.5
    bracket       none — removed, not tuned                              §5.3

Measured over 1,787 events, 2016-2026: 48.2% CAGR at Sharpe 2.15 and an 18.0%
maximum drawdown, taking 1,420 events and declining 20.5% for want of a free
slot. With the $100M/day floor: 44.6% at Sharpe 2.27 on 1,167 trades, which is
the version the paper says it would run.

NO STOP, AND WHERE THE RISK BOUND WENT INSTEAD. Plans here carry no TP and no
SL — Table 12 is unambiguous that a stop inside the whipsaw band of a name
that just moved 5% is where the edge goes (38.3bp per trade with the shipped
bracket against 138.6bp without one). The engine's invariant survives: every
position still has a server-enforced exit plan, and a hard time stop is one.

The loss bound moves up a level, from the position to the strategy:

  allocation       caps what this instance can commit at all, so a bad night
                   cannot reach past the capital it was given.
  circuit breaker  a drawdown limit on this strategy's own P&L that flattens
                   its book and pauses it.

That is the deliberate trade. A per-position stop bounds one trade and costs
edge on every trade; a breaker bounds the strategy and costs nothing until it
fires. Do not run this unbracketed with the breaker disabled.

WHAT IS NOT PROVEN. No after-hours entry has ever filled on this account —
the free IEX feed is dead after 17:00 ET, so every entry currently dies at the
quote-freshness gate. `requires = {"sip", "shorts", "llm"}` says so, and the
console shows the gap. Run this in note-mode (`live: false`) until a fill has
been demonstrated. Section 8 of the paper is blunt about it: the part of this
strategy least supported by evidence is the part that puts the trade on.

SELECTION CAVEAT. The conditional exit and the slot limit were both chosen
after seeing results. A pre-registered forward test is worth more than any
further sweep, which is what a shadow season here is for.
"""

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.llm import AnalysisError
from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent

ET = ZoneInfo("America/New_York")

WATCHLIST_ET = "15:30"     # freeze tonight's watchlist
REANCHOR_ET = "16:04"      # re-anchor px0 to the post-auction tape
EXIT_ET = dtime(15, 55)    # the paper exits at the close; 15:55 is the
                           # engine's last liquid minute before it
QUOTE_MAX_AGE_S = 120.0
SCAN_CAP_MULT = 3          # quote at most 3*n_names candidates at 15:30

# The verdict contract. Identical to the schema every cached verdict in the
# study was scored under — changing it invalidates the comparison, so the
# research tree imports THIS constant rather than copying it.
SURPRISE_SCHEMA = {
    "type": "object",
    "properties": {
        "eps_vs_consensus": {
            "type": "string", "enum": ["beat", "miss", "inline", "not_stated"]},
        "revenue_vs_consensus": {
            "type": "string", "enum": ["beat", "miss", "inline", "not_stated"]},
        "guidance": {
            "type": "string",
            "enum": ["raised", "lowered", "maintained", "none_given"]},
        "quality_flags": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "one_time_items", "non_gaap_emphasis", "segment_weakness",
                "margin_pressure", "big_buyback", "restructuring"]}},
        "direction": {
            "type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
    },
    "required": ["eps_vs_consensus", "revenue_vs_consensus", "guidance",
                 "quality_flags", "direction", "confidence", "summary"],
    "additionalProperties": False,
}

NUMS_HEADLINE = ("EPS", "Sales", "Revenue", "Q1", "Q2", "Q3", "Q4")

# Guidance values that buy the longer hold (§5.3). A verdict field, known at
# entry — the horizon rule must never key off the outcome.
GUIDANCE_MOVED = ("raised", "lowered")


@register
class PeadFlagship(Strategy):
    """PEAD, never standing down: trade every qualifying event, with the tape when the model agrees and against it otherwise."""

    kind = "pead_flagship"
    subscriptions = ("estimate", "news", "timer", "manual")
    event_timeout_s = 240.0
    requires = frozenset({"sip", "shorts", "llm"})

    default_params = {
        # --- universe (§3) -------------------------------------------------
        # Five per night, not eight: the paper's universe is the top 5 by
        # prior-session dollar volume, and the liquidity floor below is most
        # of the liquidity effect in §5.2.
        "n_names": 5,
        "min_dv_musd": 100.0,   # $100M/day — the version the paper would run
        "min_price": 5.0,
        # --- the gate (§2.1) -----------------------------------------------
        "min_move_pct": 5.0,
        "confirm_min": 10.0,    # the tape must STILL clear the gate after this
        # --- direction (§5.4) ----------------------------------------------
        # THE FLAGSHIP LINE. False reproduces the paper's headline gate, which
        # stands down on disagreement and is the policy the paper argues
        # against; the refused third returns strongly negative when taken in
        # reverse, so a refusal was never an absence of information.
        "never_stand_down": True,
        # --- exit (§5.3) ---------------------------------------------------
        "hold_days": 1,
        "hold_days_guidance": 3,   # when the verdict reports guidance moved
        # --- book (§5.5) ---------------------------------------------------
        # Six concurrent, equal weight, so peak exposure is 100% of THIS
        # strategy's allocation and the book is never levered. Contested
        # slots go to the highest prior-session dollar volume.
        "slots": 6,
        # --- bracket: none (§5.3) ------------------------------------------
        # Both null = a time-stop-only plan. Set either to opt back into a
        # bracket; the paper's measurement of what that costs is in the module
        # docstring, and the strategy-level circuit breaker is what bounds the
        # loss in the meantime.
        "sl_pct": None,
        "tp_pct": None,
        # --- plumbing ------------------------------------------------------
        "max_spread_pct": 2.0,
        "live": False,
        "max_text": 12_000,
        "effort": "medium",
        "analysis_timeout_s": 90.0,
    }

    def __init__(self):
        self._estimates: dict[str, dict[str, dict]] = {}
        self._est_ids: dict[str, int] = {}
        self._watch: dict[str, dict] = {}
        self._watch_date: str | None = None
        self._decided: set[str] = set()
        self._pending: dict[str, dict] = {}

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not (1 <= int(clean["n_names"]) <= 20):
            raise ValueError("n_names must be 1-20")
        if not (0.0 <= float(clean["min_dv_musd"]) <= 10_000.0):
            raise ValueError("min_dv_musd must be 0-10000 ($M/day)")
        if float(clean["min_price"]) < 1.0:
            raise ValueError("min_price must be >= 1")
        if not (0.2 <= float(clean["min_move_pct"]) <= 15.0):
            raise ValueError("min_move_pct must be 0.2-15.0 (percent)")
        if not (0.0 <= float(clean["confirm_min"]) <= 60.0):
            raise ValueError("confirm_min must be 0-60 minutes")
        if not (1 <= int(clean["slots"]) <= 24):
            raise ValueError("slots must be 1-24")
        for key in ("hold_days", "hold_days_guidance"):
            if not (1 <= int(clean[key]) <= 10):
                raise ValueError(f"{key} must be 1-10 sessions")
        # The bracket is all-or-nothing: one leg alone is neither a target nor
        # a stop, and place_trade refuses it anyway — fail here with the
        # reason instead of at the broker.
        for key in ("sl_pct", "tp_pct"):
            if clean[key] is not None and not (0.02 <= float(clean[key]) <= 5.0):
                raise ValueError(f"{key} must be null (the paper's rule — no "
                                 f"bracket) or 0.02-5.0")
        if (clean["sl_pct"] is None) != (clean["tp_pct"] is None):
            raise ValueError("sl_pct and tp_pct must both be null (time-stop "
                             "only, what the paper runs) or both be set")
        if not (0.1 <= float(clean["max_spread_pct"]) <= 20.0):
            raise ValueError("max_spread_pct must be 0.1-20.0 (percent of mid)")
        if not (1_000 <= int(clean["max_text"]) <= 20_000):
            raise ValueError("max_text must be 1000-20000")
        if not (10.0 <= float(clean["analysis_timeout_s"]) <= 200.0):
            raise ValueError("analysis_timeout_s must be 10-200")
        return clean

    # ---------------------------------------------------------------- events

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        if event.type == "estimate":
            self._stash_estimate(event)
        elif event.type == "timer":
            if self._tick_at(event, WATCHLIST_ET):
                await self._build_watchlist(ctx)
            elif self._tick_at(event, REANCHOR_ET) and self._watch:
                await self._reanchor(ctx)
            await self._process_due(ctx)
        elif event.type == "news":
            for sym in event.symbols:
                if self._eligible(sym, event):
                    await self._queue_or_decide(sym, event, ctx)
        elif event.type == "manual":
            await self._manual(event, ctx)

    def _stash_estimate(self, event: Event) -> None:
        p = event.payload or {}
        sym, date = str(p.get("symbol") or ""), str(p.get("date") or "")
        if not sym or not date:
            return
        self._estimates.setdefault(date, {})[sym] = p
        if event.signal_id is not None:
            self._est_ids[sym] = event.signal_id

    @staticmethod
    def _tick_at(event: Event, hhmm: str) -> bool:
        if event.payload.get("kind") != "tick":
            return False
        et_iso = event.payload.get("et")
        if not et_iso:
            return False
        now_et = datetime.fromisoformat(et_iso)
        return now_et.weekday() < 5 and now_et.strftime("%H:%M") == hhmm

    # ------------------------------------------------------------- watchlist

    async def _build_watchlist(self, ctx: StrategyContext) -> None:
        """15:30 ET: freeze tonight's names and snapshot each one's anchor.

        Ranking is by prior-session dollar volume and the floor is applied to
        the same number, so the universe here is the study's universe rather
        than a nearby one.
        """
        today = datetime.now(ET).date().isoformat()
        candidates = {
            sym: est for sym, est in self._estimates.get(today, {}).items()
            if est.get("when") == "amc"
        }
        n_names = int(ctx.params["n_names"])
        min_price = float(ctx.params["min_price"])
        floor = float(ctx.params["min_dv_musd"]) * 1e6
        watch: dict[str, dict] = {}
        skipped: list[str] = []
        below_floor: list[str] = []

        volumes = await ctx.market.daily_dollar_volumes(sorted(candidates))
        ranked = sorted(candidates, key=lambda s: (-(volumes.get(s, 0.0)), s))
        for sym in ranked:
            if len(watch) >= n_names or len(watch) + len(skipped) >= \
                    n_names * SCAN_CAP_MULT:
                break
            dv = volumes.get(sym, 0.0)
            # The floor is a HARD filter, not a tiebreak: §5.2 finds the edge
            # concentrated in the most liquid issuers, and most of the gap
            # further down the list is the quoted spread.
            if floor > 0 and dv < floor:
                below_floor.append(sym)
                continue
            quote = await ctx.market.overnight_price(sym)
            px = _usable_price(quote)
            if px is None or px < min_price:
                skipped.append(sym)
                continue
            closes = await ctx.market.daily_closes(sym, days=6)
            prior_close = closes[-1]["close"] if closes else None
            run_pct = (round((closes[-1]["close"] / closes[0]["close"] - 1) * 100, 2)
                       if len(closes) >= 2 else None)
            day_move = (round((px / prior_close - 1) * 100, 2)
                        if prior_close else None)
            watch[sym] = {"est": candidates[sym], "px0": px,
                          "est_id": self._est_ids.get(sym),
                          "prior_close": prior_close,
                          "day_move_pct": day_move,
                          "run5d_pct": run_pct,
                          "dv": dv}
        self._watch = watch
        self._watch_date = today
        self._decided = set()
        await ctx.note(
            {"watchlist": {sym: {"px0": w["px0"], "dv_musd": round(w["dv"] / 1e6, 1),
                                 "eps_consensus": w["est"].get("eps_consensus"),
                                 "day_move_pct": w["day_move_pct"],
                                 "run5d_pct": w["run5d_pct"]}
                           for sym, w in watch.items()},
             "date": today, "amc_candidates": len(candidates),
             "below_dv_floor": below_floor[:20], "skipped": skipped},
            signal_ids=tuple(w["est_id"] for w in watch.values()
                             if w["est_id"] is not None),
        )

    async def _reanchor(self, ctx: StrategyContext) -> None:
        """16:04 ET: measure the reaction against the OFFICIAL close, not
        against 15:30 drift. A name already past the gate keeps its anchor —
        a move that size before our sources fired means the release is in
        flight, and re-anchoring would erase exactly the signal being traded.
        """
        min_move = float(ctx.params["min_move_pct"])
        changed: dict[str, dict] = {}
        skipped: dict[str, str] = {}
        for sym, w in self._watch.items():
            if f"{sym}:{self._watch_date}" in self._decided:
                skipped[sym] = "decided"
                continue
            quote = await ctx.market.overnight_price(sym)
            px = _usable_price(quote, max_age_s=QUOTE_MAX_AGE_S)
            if px is None:
                skipped[sym] = "no fresh quote"
                continue
            drift_pct = (px / w["px0"] - 1) * 100
            if abs(drift_pct) >= min_move:
                skipped[sym] = f"already {drift_pct:+.2f}% - release in flight?"
                continue
            changed[sym] = {"px0": round(px, 4), "was": w["px0"]}
            w["px0"] = px
            if w.get("prior_close"):
                w["day_move_pct"] = round((px / w["prior_close"] - 1) * 100, 2)
        await ctx.note({"reanchor": changed, "skipped": skipped})

    # -------------------------------------------------------------- decision

    def _eligible(self, sym: str, event: Event) -> bool:
        if self._watch_date != datetime.now(ET).date().isoformat():
            return False
        if sym not in self._watch or f"{sym}:{self._watch_date}" in self._decided:
            return False
        if event.source == "edgar-8k":
            return True
        if len(event.symbols) != 1:
            return False
        headline = str((event.payload or {}).get("headline") or "")
        return any(tok in headline for tok in NUMS_HEADLINE) and "$" in headline

    async def _queue_or_decide(self, sym: str, event: Event,
                               ctx: StrategyContext) -> None:
        import time as _time

        delay_min = float(ctx.params["confirm_min"])
        if delay_min <= 0:
            await self._decide_text(sym, _text_of(event, int(ctx.params["max_text"])),
                                    _ids(event), event.source, ctx)
            return
        self._decided.add(f"{sym}:{self._watch_date}")  # burst-safe
        self._pending[sym] = {
            "text": _text_of(event, int(ctx.params["max_text"])),
            "ids": _ids(event),
            "source": event.source,
            "due_mono": _time.monotonic() + delay_min * 60.0,
        }
        await ctx.note({"queued": {"symbol": sym, "source": event.source,
                                   "confirm_min": delay_min}},
                       signal_ids=_ids(event))

    async def _process_due(self, ctx: StrategyContext) -> None:
        import time as _time

        now = _time.monotonic()
        for sym in [s for s, p in self._pending.items() if p["due_mono"] <= now]:
            pending = self._pending.pop(sym)
            await self._decide_text(sym, pending["text"], tuple(pending["ids"]),
                                    pending["source"], ctx)

    async def _decide_text(self, sym: str, text: str, event_ids: tuple[int, ...],
                           source: str, ctx: StrategyContext) -> None:
        key = f"{sym}:{self._watch_date}"
        self._decided.add(key)
        watch = self._watch[sym]
        if not text:
            self._decided.discard(key)
            await ctx.note({"skip": f"no text for {sym}", "source": source},
                           signal_ids=event_ids)
            return

        quote = await ctx.market.overnight_price(sym)
        px = _usable_price(quote, max_age_s=QUOTE_MAX_AGE_S)
        if px is None:
            self._decided.discard(key)
            await ctx.note({"skip": f"no fresh quote for {sym} — this is the "
                                    "SIP requirement, not a bug"},
                           signal_ids=event_ids)
            return
        move_pct = (px / watch["px0"] - 1) * 100
        min_move = float(ctx.params["min_move_pct"])

        # THE EVENT GATE, before the model call. A move under the threshold is
        # not an event this strategy has an opinion about, and asking anyway
        # would spend inference on it.
        if abs(move_pct) < min_move:
            await ctx.note({"no_trade": {"symbol": sym, "move_pct": round(move_pct, 2)},
                            "why": f"reaction {move_pct:+.2f}% below the "
                                   f"{min_move:.1f}% gate"},
                           signal_ids=event_ids)
            return

        est = watch["est"]
        setup_bits = []
        if watch.get("day_move_pct") is not None:
            setup_bits.append(f"{watch['day_move_pct']:+.1f}% today into the print")
        if watch.get("run5d_pct") is not None:
            setup_bits.append(f"{watch['run5d_pct']:+.1f}% over the prior 5 sessions")
        setup_line = (
            f" Setup into the print: {', '.join(setup_bits)}. Weigh whether "
            f"these results were already priced in by that run-up."
            if setup_bits else ""
        )
        # The tape's move is deliberately NOT in this prompt (§4.1). The model
        # judges the release; the agreement test happens here, afterwards.
        task = (
            f"{sym} released quarterly results after the close. Street "
            f"consensus for {est.get('fiscal_period') or 'the quarter'}: "
            f"EPS {est.get('eps_consensus', 'unknown')}, revenue "
            f"{est.get('revenue_consensus', 'unknown')}. Extract the "
            f"reported figures and guidance from the release, compare "
            f"against that consensus, and judge near-term direction."
            f"{setup_line}"
        )
        signal_ids = tuple(i for i in (*event_ids, watch.get("est_id"))
                           if i is not None)
        try:
            analysis = await ctx.analyze(
                task=task, data=text, schema=SURPRISE_SCHEMA, symbols=(sym,),
                signal_ids=signal_ids, effort=ctx.params["effort"],
                timeout_s=float(ctx.params["analysis_timeout_s"]),
            )
        except AnalysisError as exc:
            self._decided.discard(key)
            await ctx.note({"skip": f"analysis failed for {sym}: {exc}"},
                           signal_ids=signal_ids)
            return

        verdict = analysis.result
        provenance = tuple(i for i in (*signal_ids, analysis.signal_id)
                           if i is not None)
        side, rule = _side(verdict["direction"], move_pct,
                           bool(ctx.params["never_stand_down"]))
        hold = (int(ctx.params["hold_days_guidance"])
                if verdict.get("guidance") in GUIDANCE_MOVED
                else int(ctx.params["hold_days"]))
        detail = {
            "symbol": sym, "verdict": verdict, "source": source,
            "px0": watch["px0"], "px": px, "move_pct": round(move_pct, 2),
            "day_move_pct": watch.get("day_move_pct"),
            "run5d_pct": watch.get("run5d_pct"),
            "dv_musd": round(watch.get("dv", 0.0) / 1e6, 1),
            "rule": rule, "hold_days": hold,
            "spread_pct": _spread_pct(quote),
            "model": analysis.model, "latency_ms": analysis.latency_ms,
        }
        if side == 0:
            await ctx.note({"no_trade": detail, "why": rule},
                           signal_ids=provenance)
            return

        spread = detail.get("spread_pct")
        cap = float(ctx.params["max_spread_pct"])
        if spread is not None and spread > cap:
            await ctx.note({"veto": {**detail, "side": side},
                            "why": f"AH book too wide: spread {spread:.2f}% of "
                                   f"mid exceeds the {cap:.2f}% cap"},
                           signal_ids=provenance)
            return

        # THE SLOT ALLOCATOR (§5.5). Capital is this strategy's allocation, not
        # the account's — ctx.account() is scoped. Equal weight per slot, so
        # peak exposure is 100% of the allocation and never levered.
        try:
            book = await ctx.account()
        except Exception as exc:                              # noqa: BLE001
            await ctx.note({"skip": f"cannot size {sym}: {exc}"},
                           signal_ids=provenance)
            return
        slots = int(ctx.params["slots"])
        allocated = float(book.get("equity") or 0.0)
        available = float(book.get("available") or 0.0)
        per_slot = allocated / slots if slots else 0.0
        detail["capital"] = {
            "allocated": round(allocated, 2), "available": round(available, 2),
            "per_slot": round(per_slot, 2), "slots": slots,
        }
        if per_slot <= 0:
            await ctx.note({"skip": f"no capital allocated to size {sym}"},
                           signal_ids=provenance)
            return
        if available < per_slot:
            # This is the capacity limit, not an error. The paper declines
            # 20.5% of qualifying events for exactly this reason and reports
            # it as a property of the strategy.
            await ctx.note({"no_slot": detail,
                            "why": f"all {slots} slots committed — "
                                   f"${available:,.0f} left, ${per_slot:,.0f} "
                                   f"needed. Contested slots go to the highest "
                                   f"prior-session dollar volume; this event "
                                   f"({detail['dv_musd']}M/day) lost."},
                           signal_ids=provenance)
            return

        intent = self._intent(sym, side, px, quote, per_slot, hold, ctx.params,
                              provenance)
        detail["intent"] = _intent_dict(intent)
        record = {**detail, "side": side}
        if not bool(ctx.params["live"]):
            await ctx.note({"would_trade": record}, signal_ids=provenance)
            return
        await ctx.note({"decision": record}, signal_ids=provenance)
        await ctx.submit(intent)
        ctx.log.info("pead_flagship %s %s @ %.2f (%+.2f%% tape, %s, T+%d, %s)",
                     "long" if side > 0 else "short", sym, px, move_pct,
                     verdict["direction"], hold, rule)

    def _intent(self, sym: str, side: int, px: float, quote: dict,
                notional: float, hold_days: int, params: dict,
                signal_ids: tuple[int, ...]) -> TradeIntent:
        touch = float((quote.get("ask") if side > 0
                       else (quote.get("bid") or quote.get("ask"))) or px)
        price = round(touch, 2)
        qty = max(1, int(notional // price))
        entry = side * price
        # No bracket (§5.3): both null, so the enforcer runs this as a
        # time-stop-only plan and evaluates no thresholds at all. Setting the
        # params opts back into a real bracket.
        sl_pct, tp_pct = params["sl_pct"], params["tp_pct"]
        if sl_pct is None:
            tp = sl = None
        else:
            sl = round(side * price * (1 - side * float(sl_pct)), 2)
            tp = round(side * price * (1 + side * float(tp_pct)), 2)
        return TradeIntent(
            asset_class="equity",
            underlying=sym,
            legs=[{"symbol": sym, "side": side, "ratio": 1, "entry": entry}],
            qty=qty,
            entry_limit=entry,
            tp=tp,
            sl=sl,
            time_stop_utc=_exit_time(hold_days),
            extended_hours=True,
            reason=f"pead flagship {'long' if side > 0 else 'short'} T+{hold_days}",
            signal_ids=signal_ids,
            dedupe_key=f"pead:{sym}:{self._watch_date}",
            max_event_age_s=600.0,
        )

    # ---------------------------------------------------------------- manual

    async def _manual(self, event: Event, ctx: StrategyContext) -> None:
        """Operator commands. Not a trading path — the strategy is autonomous.

        {"cmd": "build_watchlist"}  freeze the watchlist NOW. The late-boot
                                    salvage: an engine that was down through
                                    the 15:30 tick has no watchlist and would
                                    sit out the night.
        {"symbol": X, "text": Y}    run the decision path for X as if its
                                    release just crossed (dry run).
        """
        p = event.payload or {}
        if p.get("cmd") == "build_watchlist":
            await self._build_watchlist(ctx)
            return
        sym = str(p.get("symbol") or "").upper()
        text = str(p.get("text") or "")
        if not sym or not text:
            await ctx.note({"skip": "manual command needs {\"cmd\": "
                                    "\"build_watchlist\"} or {symbol, text}"},
                           signal_ids=_ids(event))
            return
        if sym not in self._watch:
            px0 = p.get("px0")
            if px0 is None:
                await ctx.note({"skip": f"{sym} not on tonight's watchlist — "
                                        "pass px0 to dry-run it"},
                               signal_ids=_ids(event))
                return
            self._watch[sym] = {"est": {}, "px0": float(px0), "est_id": None,
                                "dv": 0.0}
            self._watch_date = datetime.now(ET).date().isoformat()
        self._decided.discard(f"{sym}:{self._watch_date}")
        await self._decide_text(sym, text, _ids(event), event.source, ctx)


# ------------------------------------------------------------------ helpers

def _side(direction: str, move_pct: float, never_stand_down: bool
          ) -> tuple[int, str]:
    """THE POLICY (§5.4), and the only place direction is decided.

    Agreement trades with the tape. Everything else — disagreement AND
    neutral — trades AGAINST it, which is the paper's central finding: the
    gate's refusals are not an absence of information, they are information
    spent the wrong way. Neutral verdicts faded are the single strongest row
    in the mutation table (+77.3bp on 296 events).

    With never_stand_down False this degrades to the paper's headline gate,
    which stands down instead. Kept so the two policies can be run side by
    side on the same live events — the comparison the study could only make
    on cached verdicts.
    """
    agrees = ((direction == "bullish" and move_pct > 0)
              or (direction == "bearish" and move_pct < 0))
    if agrees:
        return (1 if move_pct > 0 else -1), "with the tape (verdict agrees)"
    if not never_stand_down:
        return 0, f"gate: verdict {direction} disagrees with a {move_pct:+.2f}% tape"
    # Fade: against the tape, so the sign is the opposite of the move.
    return (-1 if move_pct > 0 else 1), (
        f"against the tape (verdict {direction})")


def _spread_pct(quote: dict | None) -> float | None:
    """Full bid/ask spread as a percent of mid. None when the quote carries no
    two-sided book — a tape print synthesized from a bar has bid == ask, which
    is absence of spread information, not a tight market."""
    if not quote:
        return None
    bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2) * 100, 3)


def _usable_price(quote: dict | None, max_age_s: float | None = None
                  ) -> float | None:
    if not quote:
        return None
    if max_age_s is not None:
        ts = float(quote.get("ts") or 0)
        if not ts:
            return None
        import time as _time

        if (_time.time() * 1000 - ts) / 1000 > max_age_s:
            return None
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return float(ask or bid or 0) or None


def _text_of(event: Event, max_text: int) -> str:
    p = event.payload or {}
    text = str(p.get("text") or "").strip()
    if not text:
        headline = str(p.get("headline") or "").strip()
        summary = str(p.get("summary") or "").strip()
        text = f"{headline}\n{summary}".strip()
    return text[:max_text]


def _exit_time(hold_days: int, now_et: datetime | None = None) -> datetime:
    """The close of the Nth following SESSION. Weekend-aware; holidays are
    not, so a holiday shifts the exit one session later than intended — the
    enforcer's time stop still fires, it just fires at the next open."""
    day = (now_et or datetime.now(ET)).date()
    added = 0
    while added < hold_days:
        day += timedelta(days=1)
        if day.weekday() < 5:
            added += 1
    return datetime.combine(day, EXIT_ET, tzinfo=ET).astimezone(timezone.utc)


def _intent_dict(intent: TradeIntent) -> dict:
    return {
        "underlying": intent.underlying, "qty": intent.qty,
        "entry_limit": intent.entry_limit, "tp": intent.tp, "sl": intent.sl,
        "time_stop_utc": intent.time_stop_utc.isoformat(),
        "dedupe_key": intent.dedupe_key, "extended_hours": intent.extended_hours,
    }


def _ids(event: Event) -> tuple[int, ...]:
    return (event.signal_id,) if event.signal_id is not None else ()
