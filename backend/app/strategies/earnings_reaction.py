"""Earnings reaction-continuation strategy (Phase 10 skeleton, note-mode).

Thesis: after-close earnings releases reprice the stock within the release
minute (measured 2026-08-04: 8/10 top reporters' first >=1% bar WAS the
minute the numbers crossed — docs/notes/earnings_latency_20260804.md); what
remains tradeable is the CONTINUATION when the release's content and the
tape's verdict AGREE. Long shares when the LLM reads the release bullish
AND the tape is already up >= min_move_pct (short: both inverted); exit by
time stop T+1/T+2. Disagreement = no trade, journaled.

One instance watches many tickers:
- `estimate` events (earnings-cal feed) accumulate; the 15:30 ET timer tick
  freezes tonight's AMC watchlist (n_names cap, min_price floor) and
  snapshots each name's pre-release price (px0) plus BEFORE-CLOSE CONTEXT:
  prior close, day move, and 5-session run-up (market.daily_closes). The
  run-up rides into the LLM task so "beat but priced in" is scoreable —
  AMD 2026-08-04 came in +14.1%/5d, double-beat, and dropped 5.7%.
- The 16:04 ET tick re-anchors px0 to the post-auction tape for names whose
  release hasn't crossed (guards: decided names and anything already past
  min_move_pct keep their anchor), so move_pct measures reaction to the
  release, not 15:30->close drift.
- The first results-bearing `news` event per watched name — edgar-8k
  full text, or a single-symbol numbers headline from alpaca-news,
  whichever lands first (neither source dominates: measured median -33s
  benzinga-vs-edgar with wide spread both directions) — triggers ONE
  ctx.analyze() against the stored consensus, then the agreement gate.
- NOTE-MODE: with live=false (default) the would-be trade is journaled as
  a `would_trade` note and nothing is submitted. Flip live=true only after
  reviewing a night of journaled would-be trades. Shorts additionally stay
  behind the engine-wide equity_long_only gate (Phase 9).

Restart behavior: watchlist state is in-memory; the calendar feed
republishes estimates on every fetch (at-least-once bus), so a restart
recovers at the next fetch/boot — but a restart AFTER 15:30 loses tonight's
px0 snapshots and the instance sits out the night. Acceptable for a
skeleton whose job is to journal evidence.
"""

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.llm import AnalysisError
from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent

ET = ZoneInfo("America/New_York")

WATCHLIST_ET = "15:30"     # freeze tonight's watchlist on this tick
REANCHOR_ET = "16:04"      # re-anchor px0 to the post-auction tape (see below)
EXIT_ET = dtime(15, 55)    # time-stop lands just before the close T+1/T+2
QUOTE_MAX_AGE_S = 120.0    # ref_tick's freshness backstop, same reasoning
SCAN_CAP_MULT = 3          # quote at most 3*n_names candidates at 15:30

# The verdict is deliberately coarse enums + one line of prose; downstream
# code branches on enums only. Consensus numbers travel IN THE TASK so the
# model compares the release against tonight's street numbers, not priors.
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


@register
class EarningsReaction(Strategy):
    """LLM release-parse + tape-agreement gate; journals would-be trades (note-mode skeleton)."""

    kind = "earnings_reaction"
    subscriptions = ("estimate", "news", "timer", "manual")
    # One analysis on up to max_text chars plus quote fetches; the per-call
    # analysis timeout below stays well inside this budget.
    event_timeout_s = 240.0
    default_params = {
        "n_names": 8,
        "min_price": 5.0,
        # Sizing is stop-risk based, scaled by account equity and the
        # verdict (2026-08-05, replacing fixed notional): risk_pct_per_name
        # of equity is what the STOP may lose, so base notional =
        # equity * risk_pct / sl_pct (0.5% risk / 5% stop = 10% of equity),
        # then conviction multipliers — confidence x1.5/x1.0/x0.5, any
        # quality flag x0.75, |tape move| > 6% x0.5 (the continuation is
        # partly spent). Engine risk gates (per-name notional cap, gross
        # exposure, borrow checks) still apply downstream.
        "risk_pct_per_name": 0.5,
        "min_move_pct": 1.0,       # tape agreement threshold, percent
        # Bracket scales with the NAME's volatility (2026-08-05): a flat 5%
        # stop sits inside the whipsaw band of a +/-10% earnings mover and
        # gets shaken out on path noise. Stop = clamp(vol_stop_mult x avg
        # |daily ret| over the run-up window, sl_pct floor, sl_pct_cap);
        # TP = 2x stop (keeps the 2:1 shape). Sizing divides risk by the
        # SAME effective stop, so wilder name = wider berth = fewer shares,
        # identical dollars at risk.
        "tp_pct": 0.10,            # floor TP (2x sl floor); both scale up
        "sl_pct": 0.05,            # stop FLOOR
        "sl_pct_cap": 0.12,        # stop ceiling (wildest names)
        "vol_stop_mult": 2.5,      # x avg abs daily return
        # Confirmation delay (2026-08-05): the first minutes post-release
        # are the worst microstructure of the day (price discovery,
        # widest AH spreads, dip-then-spike whipsaw). Queue the release and
        # decide confirm_min minutes later — the tape must STILL clear
        # min_move_pct then. The drift thesis is day-scale; skipping the
        # discovery chaos costs little and filters transient first reads.
        # 0 = decide immediately (the pre-2026-08-05 behavior).
        "confirm_min": 10.0,
        "hold": "T+1",             # T+1 | T+2, exits 15:55 ET
        "live": False,             # note-mode until Matthew flips it
        "max_text": 12_000,
        "effort": "medium",        # release parsing deserves real effort
        "analysis_timeout_s": 90.0,
    }

    def __init__(self):
        self._estimates: dict[str, dict[str, dict]] = {}   # date -> sym -> est
        self._est_ids: dict[str, int] = {}                 # sym -> signal id
        self._watch: dict[str, dict] = {}                  # sym -> watch entry
        self._watch_date: str | None = None
        self._decided: set[str] = set()                    # "sym:date"
        # Releases queued for the confirmation delay: sym -> {text, ids,
        # source, due_mono}. In-memory: a restart mid-window drops pending
        # confirmations (the release event cannot re-fire past the journal
        # dedupe) — documented cost of the delay design.
        self._pending: dict[str, dict] = {}

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        # Legacy key from the fixed-notional era: tolerate and drop, so
        # pre-2026-08-05 instance rows can still spawn after upgrade.
        params = {k: v for k, v in params.items() if k != "notional_per_name"}
        clean = super().validate_params(params)
        if not (1 <= int(clean["n_names"]) <= 20):
            raise ValueError("n_names must be 1-20")
        if float(clean["min_price"]) < 1.0:
            raise ValueError("min_price must be >= 1")
        if not (0.05 <= float(clean["risk_pct_per_name"]) <= 2.0):
            raise ValueError("risk_pct_per_name must be 0.05-2.0 (% of equity "
                             "at the stop)")
        if not (0.2 <= float(clean["min_move_pct"]) <= 5.0):
            raise ValueError("min_move_pct must be 0.2-5.0 (percent)")
        for key in ("tp_pct", "sl_pct", "sl_pct_cap"):
            if not (0.01 <= float(clean[key]) <= 0.30):
                raise ValueError(f"{key} must be 0.01-0.30")
        if float(clean["sl_pct_cap"]) < float(clean["sl_pct"]):
            raise ValueError("sl_pct_cap must be >= sl_pct (floor)")
        if not (1.0 <= float(clean["vol_stop_mult"]) <= 5.0):
            raise ValueError("vol_stop_mult must be 1-5")
        if not (0.0 <= float(clean["confirm_min"]) <= 60.0):
            raise ValueError("confirm_min must be 0-60 minutes")
        if clean["hold"] not in ("T+1", "T+2"):
            raise ValueError("hold must be T+1 or T+2")
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

    async def _queue_or_decide(self, sym: str, event: Event,
                               ctx: StrategyContext) -> None:
        """confirm_min > 0: park the release and decide when the delay
        elapses (the tape must still agree then). 0: the old immediate
        path."""
        import time as _time

        delay_min = float(ctx.params["confirm_min"])
        if delay_min <= 0:
            await self._decide(sym, event, ctx)
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
            await self._decide_text(
                sym, pending["text"], tuple(pending["ids"]),
                pending["source"], ctx,
            )

    # ------------------------------------------------------------- estimates

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

    async def _build_watchlist(self, ctx: StrategyContext) -> None:
        today = datetime.now(ET).date().isoformat()
        candidates = {
            sym: est for sym, est in self._estimates.get(today, {}).items()
            if est.get("when") == "amc"
        }
        n_names = int(ctx.params["n_names"])
        min_price = float(ctx.params["min_price"])
        watch: dict[str, dict] = {}
        skipped: list[str] = []
        # Liquidity rank FIRST (one batched daily-bars call): a peak-season
        # night has 250+ AMC reporters and alphabetical scanning would
        # watch the ABC small-caps while DIS/UBER report unwatched
        # (2026-08-05: 269 AMC candidates). Fall back to alphabetical when
        # the batch fails - a degraded rank must not kill the night.
        volumes = await ctx.market.daily_dollar_volumes(sorted(candidates))
        ranked = sorted(candidates,
                        key=lambda s: (-(volumes.get(s, 0.0)), s))
        for sym in ranked:
            if len(watch) >= n_names or len(watch) + len(skipped) >= \
                    n_names * SCAN_CAP_MULT:
                break
            quote = await ctx.market.overnight_price(sym)
            px = _usable_price(quote)
            if px is None or px < min_price:
                skipped.append(sym)
                continue
            # Before-close context: run-up into the print is a first-class
            # feature (AMD 2026-08-04 came in +14.1%/5d, double-beat, and
            # dropped 5.7% — "priced in" is real). Optional by design:
            # daily_closes returning [] never blocks watching.
            closes = await ctx.market.daily_closes(sym, days=6)
            prior_close = closes[-1]["close"] if closes else None
            run_pct = (round((closes[-1]["close"] / closes[0]["close"] - 1) * 100, 2)
                       if len(closes) >= 2 else None)
            day_move = (round((px / prior_close - 1) * 100, 2)
                        if prior_close else None)
            abs_rets = [abs(closes[i]["close"] / closes[i - 1]["close"] - 1)
                        for i in range(1, len(closes))]
            avg_abs = (round(sum(abs_rets) / len(abs_rets) * 100, 2)
                       if abs_rets else None)
            watch[sym] = {"est": candidates[sym], "px0": px,
                          "est_id": self._est_ids.get(sym),
                          "prior_close": prior_close,
                          "day_move_pct": day_move,
                          "run5d_pct": run_pct,
                          "avg_abs_ret_pct": avg_abs}
        self._watch = watch
        self._watch_date = today
        await ctx.note(
            {"watchlist": {sym: {"px0": w["px0"],
                                 "eps_consensus": w["est"].get("eps_consensus"),
                                 "day_move_pct": w["day_move_pct"],
                                 "run5d_pct": w["run5d_pct"]}
                           for sym, w in watch.items()},
             "date": today, "amc_candidates": len(candidates),
             "skipped": skipped},
            signal_ids=tuple(w["est_id"] for w in watch.values()
                             if w["est_id"] is not None),
        )

    async def _reanchor(self, ctx: StrategyContext) -> None:
        """16:04 ET: the closing auction has printed, so re-anchor px0 for
        names whose release has not crossed yet — move_pct should measure
        reaction to the RELEASE, not 15:30->close drift plus the auction
        (AMD 2026-08-04: 15:59 tape ~526 vs 518.58 official close). Guards:
        decided names keep their anchor, and a name already >= min_move_pct
        off its 15:30 snapshot is left alone — a move that size before our
        sources fired means a release is in flight and re-anchoring would
        erase exactly the signal we trade."""
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
        """First results-bearing source wins: edgar-8k always qualifies;
        alpaca-news only as a single-symbol numbers headline (multi-tag
        stories are roundups, not releases)."""
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

    async def _decide(self, sym: str, event: Event, ctx: StrategyContext,
                      forced_text: str | None = None) -> None:
        text = forced_text or _text_of(event, int(ctx.params["max_text"]))
        await self._decide_text(sym, text, _ids(event), event.source, ctx)

    async def _decide_text(self, sym: str, text: str,
                           event_ids: tuple[int, ...], source: str,
                           ctx: StrategyContext) -> None:
        key = f"{sym}:{self._watch_date}"
        self._decided.add(key)  # burst-safe: headline+filing arrive seconds apart
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
            await ctx.note({"skip": f"no fresh quote for {sym}"},
                           signal_ids=event_ids)
            return
        move_pct = (px / watch["px0"] - 1) * 100

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
        task = (
            f"{sym} released quarterly results after the close. Street "
            f"consensus for {est.get('fiscal_period') or 'the quarter'}: "
            f"EPS {est.get('eps_consensus', 'unknown')}, revenue "
            f"{est.get('revenue_consensus', 'unknown')}. Extract the "
            f"reported figures and guidance from the release, compare "
            f"against that consensus, and judge near-term direction."
            f"{setup_line}"
        )
        # News id first: the analysis row chains (parent_id) to the text it
        # analyzed; the estimate rides along as secondary provenance.
        signal_ids = tuple(i for i in (*event_ids, watch.get("est_id"))
                           if i is not None)
        try:
            analysis = await ctx.analyze(
                task=task, data=text, schema=SURPRISE_SCHEMA, symbols=(sym,),
                signal_ids=signal_ids, effort=ctx.params["effort"],
                timeout_s=float(ctx.params["analysis_timeout_s"]),
            )
        except AnalysisError as exc:
            # Un-mark so the OTHER source (edgar text after headline, or
            # vice versa) gets a fresh attempt tonight.
            self._decided.discard(key)
            await ctx.note({"skip": f"analysis failed for {sym}: {exc}"},
                           signal_ids=signal_ids)
            return

        verdict = analysis.result
        provenance = tuple(i for i in (*signal_ids, analysis.signal_id)
                           if i is not None)
        min_move = float(ctx.params["min_move_pct"])
        side = 0
        if verdict["direction"] == "bullish" and move_pct >= min_move:
            side = 1
        elif verdict["direction"] == "bearish" and move_pct <= -min_move:
            side = -1
        sl_eff, tp_eff = _effective_bracket(watch, ctx.params)
        detail = {
            "symbol": sym, "verdict": verdict, "source": source,
            "px0": watch["px0"], "px": px, "move_pct": round(move_pct, 2),
            "day_move_pct": watch.get("day_move_pct"),
            "run5d_pct": watch.get("run5d_pct"),
            "bracket": {"sl_pct": sl_eff, "tp_pct": tp_eff,
                        "avg_abs_ret_pct": watch.get("avg_abs_ret_pct")},
            "model": analysis.model, "latency_ms": analysis.latency_ms,
        }
        if side == 0:
            await ctx.note({"no_trade": detail,
                            "why": "verdict/tape disagree or move below "
                                   f"threshold ({move_pct:+.2f}% vs "
                                   f"{min_move:.2f}%)"},
                           signal_ids=provenance)
            return

        try:
            equity = float((await ctx.account()).get("equity") or 0)
        except Exception as exc:
            await ctx.note({"skip": f"cannot size {sym}: account equity "
                                    f"unavailable ({exc})"},
                           signal_ids=provenance)
            return
        if equity <= 0:
            await ctx.note({"skip": f"cannot size {sym}: equity reads {equity}"},
                           signal_ids=provenance)
            return
        sizing = _size_position(equity, verdict, move_pct, ctx.params, sl_eff)
        detail["sizing"] = sizing
        intent = self._intent(sym, side, px, quote, sizing["notional"],
                              sl_eff, tp_eff, ctx.params, provenance)
        if not bool(ctx.params["live"]):
            await ctx.note({"would_trade": {**detail, "side": side,
                                            "intent": _intent_dict(intent)}},
                           signal_ids=provenance)
            return
        await ctx.submit(intent)
        ctx.log.info("earnings_reaction placed %s %s @ %.2f (%+.2f%% tape, %s)",
                     "long" if side > 0 else "short", sym, px, move_pct,
                     verdict["confidence"])

    def _intent(self, sym: str, side: int, px: float, quote: dict,
                notional: float, sl_pct: float, tp_pct: float, params: dict,
                signal_ids: tuple[int, ...]) -> TradeIntent:
        # Marketable entry: cross the spread on the entry side (ref_tick's
        # convention) — longs lift the ask, shorts hit the bid. `px` (mid)
        # stays the MOVE measurement; the order prices at the touch, and the
        # exec-quality ledger records what immediacy cost.
        touch = float((quote.get("ask") if side > 0
                       else (quote.get("bid") or quote.get("ask"))) or px)
        price = round(touch, 2)
        qty = max(1, int(notional // price))
        entry = side * price
        tp = round(side * price * (1 + side * tp_pct), 2)
        sl = round(side * price * (1 - side * sl_pct), 2)
        return TradeIntent(
            asset_class="equity",
            underlying=sym,
            legs=[{"symbol": sym, "side": side, "ratio": 1, "entry": entry}],
            qty=qty,
            entry_limit=entry,
            tp=tp,
            sl=sl,
            time_stop_utc=_exit_time(str(params["hold"])),
            extended_hours=True,
            reason=f"earnings reaction {'long' if side > 0 else 'short'}",
            signal_ids=signal_ids,
            dedupe_key=f"earn:{sym}:{self._watch_date}",
            # Event ts = EDGAR acceptance; acceptance->text->analysis can
            # legitimately take 2-5 min on a busy night. 10 min still means
            # "same release, reaction phase" for a T+1 hold; the default
            # 300s would refuse valid entries on slow-analysis nights.
            max_event_age_s=600.0,
        )

    # ---------------------------------------------------------------- manual

    async def _manual(self, event: Event, ctx: StrategyContext) -> None:
        """Dry-run hook: {"symbol": X, "text": Y[, "px0": P]} runs the
        decision path for X as if its release just crossed. Unwatched
        symbols need px0 (no snapshot exists).

        {"cmd": "build_watchlist"} freezes the watchlist NOW — the
        late-boot salvage (born 2026-08-05: the engine was down through
        the 15:30 tick and the night had no watchlist). px0 = current
        price, a correct pre-release anchor only for names that have NOT
        reported yet; already-reported names hold a stale anchor but
        cannot re-fire their filing event (journal dedupe), so at worst a
        follow-up headline produces a no_trade note against a flat move."""
        p = event.payload or {}
        if p.get("cmd") == "build_watchlist":
            await self._build_watchlist(ctx)
            return
        sym = str(p.get("symbol") or "").upper()
        text = str(p.get("text") or "")
        if not sym or not text:
            await ctx.note({"skip": "manual trigger needs symbol+text"},
                           signal_ids=_ids(event))
            return
        if sym not in self._watch:
            px0 = p.get("px0")
            if px0 is None:
                await ctx.note({"skip": f"{sym} not on tonight's watchlist - "
                                        "pass px0 to dry-run it"},
                               signal_ids=_ids(event))
                return
            self._watch[sym] = {"est": {}, "px0": float(px0), "est_id": None}
            self._watch_date = datetime.now(ET).date().isoformat()
        self._decided.discard(f"{sym}:{self._watch_date}")
        await self._decide(sym, event, ctx, forced_text=text)


# ------------------------------------------------------------------ helpers

def _effective_bracket(watch: dict, params: dict) -> tuple[float, float]:
    """(sl_pct, tp_pct) scaled to the name's own volatility — see the
    default_params note. Unknown vol (no daily history) -> the floors."""
    sl_floor = float(params["sl_pct"])
    avg_abs = watch.get("avg_abs_ret_pct")
    if avg_abs:
        sl = float(params["vol_stop_mult"]) * float(avg_abs) / 100.0
        sl = min(max(sl, sl_floor), float(params["sl_pct_cap"]))
    else:
        sl = sl_floor
    return round(sl, 4), round(2 * sl, 4)


def _size_position(equity: float, verdict: dict, move_pct: float,
                   params: dict, sl_pct: float) -> dict:
    """Stop-risk sizing with conviction scaling (see default_params note).
    Returns the full audit trail — every sizing decision must be
    reconstructible from the journal."""
    risk_dollars = equity * float(params["risk_pct_per_name"]) / 100.0
    base_notional = risk_dollars / sl_pct
    conf_mult = {"high": 1.5, "medium": 1.0, "low": 0.5}.get(
        str(verdict.get("confidence")), 1.0)
    flag_mult = 0.75 if verdict.get("quality_flags") else 1.0
    move_mult = 0.5 if abs(move_pct) > 6.0 else 1.0
    notional = base_notional * conf_mult * flag_mult * move_mult
    return {
        "equity": round(equity, 2),
        "risk_dollars": round(risk_dollars, 2),
        "base_notional": round(base_notional, 2),
        "conf_mult": conf_mult,
        "flag_mult": flag_mult,
        "move_mult": move_mult,
        "notional": round(notional, 2),
    }


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


def _exit_time(hold: str, now_et: datetime | None = None) -> datetime:
    days = 1 if hold == "T+1" else 2
    day = (now_et or datetime.now(ET)).date()
    added = 0
    while added < days:
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
