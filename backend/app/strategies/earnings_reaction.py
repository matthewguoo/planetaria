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
  snapshots each name's pre-release price (px0).
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
        "notional_per_name": 1000.0,
        "min_move_pct": 1.0,       # tape agreement threshold, percent
        "tp_pct": 0.10,            # guardrail bracket; the THESIS exit is
        "sl_pct": 0.05,            # the time stop
        "hold": "T+1",             # T+1 | T+2, exits 15:55 ET
        "live": False,             # note-mode until Matthew flips it
        "max_text": 12_000,
        "effort": None,            # None -> engine default
        "analysis_timeout_s": 90.0,
    }

    def __init__(self):
        self._estimates: dict[str, dict[str, dict]] = {}   # date -> sym -> est
        self._est_ids: dict[str, int] = {}                 # sym -> signal id
        self._watch: dict[str, dict] = {}                  # sym -> watch entry
        self._watch_date: str | None = None
        self._decided: set[str] = set()                    # "sym:date"

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not (1 <= int(clean["n_names"]) <= 20):
            raise ValueError("n_names must be 1-20")
        if float(clean["min_price"]) < 1.0:
            raise ValueError("min_price must be >= 1")
        if not (100.0 <= float(clean["notional_per_name"]) <= 5000.0):
            raise ValueError("notional_per_name must be 100-5000 (paper skeleton)")
        if not (0.2 <= float(clean["min_move_pct"]) <= 5.0):
            raise ValueError("min_move_pct must be 0.2-5.0 (percent)")
        for key in ("tp_pct", "sl_pct"):
            if not (0.01 <= float(clean[key]) <= 0.25):
                raise ValueError(f"{key} must be 0.01-0.25")
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
            if self._watchlist_tick(event):
                await self._build_watchlist(ctx)
        elif event.type == "news":
            for sym in event.symbols:
                if self._eligible(sym, event):
                    await self._decide(sym, event, ctx)
        elif event.type == "manual":
            await self._manual(event, ctx)

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
    def _watchlist_tick(event: Event) -> bool:
        if event.payload.get("kind") != "tick":
            return False
        et_iso = event.payload.get("et")
        if not et_iso:
            return False
        now_et = datetime.fromisoformat(et_iso)
        return (now_et.weekday() < 5
                and now_et.strftime("%H:%M") == WATCHLIST_ET)

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
        # No liquidity data on ctx.market yet, so selection is price-gated
        # first-come (sorted for determinism); ranking by $ volume is a
        # later refinement the latency study says matters.
        for sym in sorted(candidates):
            if len(watch) >= n_names or len(watch) + len(skipped) >= \
                    n_names * SCAN_CAP_MULT:
                break
            quote = await ctx.market.overnight_price(sym)
            px = _usable_price(quote)
            if px is None or px < min_price:
                skipped.append(sym)
                continue
            watch[sym] = {"est": candidates[sym], "px0": px,
                          "est_id": self._est_ids.get(sym)}
        self._watch = watch
        self._watch_date = today
        await ctx.note(
            {"watchlist": {sym: {"px0": w["px0"],
                                 "eps_consensus": w["est"].get("eps_consensus")}
                           for sym, w in watch.items()},
             "date": today, "amc_candidates": len(candidates),
             "skipped": skipped},
            signal_ids=tuple(w["est_id"] for w in watch.values()
                             if w["est_id"] is not None),
        )

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
        key = f"{sym}:{self._watch_date}"
        self._decided.add(key)  # burst-safe: headline+filing arrive seconds apart
        watch = self._watch[sym]
        text = forced_text or _text_of(event, int(ctx.params["max_text"]))
        if not text:
            self._decided.discard(key)
            await ctx.note({"skip": f"no text for {sym}", "source": event.source},
                           signal_ids=_ids(event))
            return

        quote = await ctx.market.overnight_price(sym)
        px = _usable_price(quote, max_age_s=QUOTE_MAX_AGE_S)
        if px is None:
            self._decided.discard(key)
            await ctx.note({"skip": f"no fresh quote for {sym}"},
                           signal_ids=_ids(event))
            return
        move_pct = (px / watch["px0"] - 1) * 100

        est = watch["est"]
        task = (
            f"{sym} released quarterly results after the close. Street "
            f"consensus for {est.get('fiscal_period') or 'the quarter'}: "
            f"EPS {est.get('eps_consensus', 'unknown')}, revenue "
            f"{est.get('revenue_consensus', 'unknown')}. Extract the "
            f"reported figures and guidance from the release, compare "
            f"against that consensus, and judge near-term direction."
        )
        # News id first: the analysis row chains (parent_id) to the text it
        # analyzed; the estimate rides along as secondary provenance.
        signal_ids = tuple(i for i in (*_ids(event), watch.get("est_id"))
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
        detail = {
            "symbol": sym, "verdict": verdict, "source": event.source,
            "px0": watch["px0"], "px": px, "move_pct": round(move_pct, 2),
            "model": analysis.model, "latency_ms": analysis.latency_ms,
        }
        if side == 0:
            await ctx.note({"no_trade": detail,
                            "why": "verdict/tape disagree or move below "
                                   f"threshold ({move_pct:+.2f}% vs "
                                   f"{min_move:.2f}%)"},
                           signal_ids=provenance)
            return

        intent = self._intent(sym, side, px, ctx.params, provenance)
        if not bool(ctx.params["live"]):
            await ctx.note({"would_trade": {**detail, "side": side,
                                            "intent": _intent_dict(intent)}},
                           signal_ids=provenance)
            return
        await ctx.submit(intent)
        ctx.log.info("earnings_reaction placed %s %s @ %.2f (%+.2f%% tape, %s)",
                     "long" if side > 0 else "short", sym, px, move_pct,
                     verdict["confidence"])

    def _intent(self, sym: str, side: int, px: float, params: dict,
                signal_ids: tuple[int, ...]) -> TradeIntent:
        price = round(px, 2)
        qty = max(1, int(float(params["notional_per_name"]) // price))
        entry = side * price
        tp = round(side * price * (1 + side * float(params["tp_pct"])), 2)
        sl = round(side * price * (1 - side * float(params["sl_pct"])), 2)
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
        )

    # ---------------------------------------------------------------- manual

    async def _manual(self, event: Event, ctx: StrategyContext) -> None:
        """Dry-run hook: {"symbol": X, "text": Y[, "px0": P]} runs the
        decision path for X as if its release just crossed. Unwatched
        symbols need px0 (no snapshot exists)."""
        p = event.payload or {}
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
