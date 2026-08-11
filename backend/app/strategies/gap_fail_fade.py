"""Fade the failed gap at the opening auction — one minute of risk.

Evidence: research/open-window/ (premarket_auction and failed_gap_split
notes, 2026-08-10; pre-registration at docs/pre-registration-gap-fail-
fade.md). On 6,836 liquid gap events 2022-2026: when the LATE PREMARKET
tape (09:15->09:29) has moved >= 20bp AGAINST a >= 1.5% overnight gap,
entering AGAINST the gap at the opening auction and exiting one minute
later earned +23.9bp gross per trade (t 5.85), both directions, positive
four of five years and strongest in 2025-26. The account sim at two slots
per leg and 10bp costs: long leg Sharpe 1.00, short 1.11, both 1.53 (leg
correlation -0.05).

THE EDGE IS THE PRINT. The same trade entered at 09:31 instead of the
auction is flat-to-negative in every window — so the entry is a
market-on-open order submitted ~09:27, which fills at the official
auction print with no spread crossed and no human latency. That is the
`auction: "open"` leg flag place_trade turns into an OPG order (and
refuses after the broker's 09:28 ET cutoff).

Clock (ET, weekdays):
  09:12  candidates from the premarket movers screen; prior closes derive
         from the screen's own percent-vs-close; dollar-volume ranks fetch
  09:15  premarket price snapshot per candidate — broker feed while fresh,
         consolidated public prints where IEX is dark (Amendment 1: the
         first live morning proved IEX-only premarket sight is blind on
         gapped small/mids; the study's own panel was consolidated prints)
  09:27  the decision: gap vs prior close >= min_gap, premarket turn
         (09:15 -> now) >= min_turn AGAINST the gap -> trade against the
         gap; contested slots to the highest |gap| x log10(dollar volume)
  09:28  MOO orders are already at the broker (submitted at the 09:27
         decision); the auction fills them at 09:30
  09:31:30  hard time stop — the ExitEnforcer takes the book off. No tp,
         no sl: the snap-back either happened in that minute or it is not
         this trade.

The SHORT leg (gap-up, premarket fading) is coded and measured slightly
stronger than the long, but it defaults OFF (`enable_short_leg: false`)
— flip it together with the engine's `equity_long_only` gate. Until then
the strategy journals the shorts it would have taken, which is the
forward evidence for turning them on.

Run in note-mode (`live: false`) until the pre-registration's gates pass.
"""

import math
import time
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent
from app.strategies.pead_flagship import _usable_price

ET = ZoneInfo("America/New_York")

MARK_SCAN = "09:12"
MARK_SNAP = "09:15"
MARK_DECIDE = "09:27"
EXIT_ET = dtime(9, 31, 30)


@register
class GapFailFade(Strategy):
    """Fade gaps the premarket has already abandoned, at the auction print, out one minute later."""

    kind = "gap_fail_fade"
    subscriptions = ("timer", "manual")
    requires = frozenset()      # premarket IEX + MOO: nothing gated

    # Frozen at registration (246f21e). Band and Sharpe are the LONG leg —
    # the short leg journals would-takes until shorts unlock (both-legs
    # registered expectation is 1.2-1.7).
    registered = {
        "doc": "docs/pre-registration-gap-fail-fade.md",
        "registered_commit": "246f21e",
        "source_note": "research/open-window/notes/failed_gap_split_20260810.md",
        "window": "2022-01..2026-08",
        "metric": "net_bp_per_trade",
        "metric_label": "net bp/trade (long leg)",
        "band": [8.0, 15.0],
        "sharpe_band": [0.7, 1.1],
        "trades_per_day_band": [0.5, 0.8],
        "backtest": {"value": 23.9, "t": 5.85, "sharpe": 1.00,
                     "basis": "gross at the auction print; Sharpe is the account sim at 10bp"},
        "costs_assumed": "10bp round trip; measured exit cost >20bp is stopping rule #1",
        "ladder": [{"stage": "note-mode", "sessions": 20},
                   {"stage": "one-share live", "sessions": 10},
                   {"stage": "full size", "sessions": None}],
    }

    default_params = {
        "min_gap_pct": 1.5,      # |open gap| vs prior close, at 09:27
        "min_turn_bp": 20.0,     # 09:15 -> 09:27 move against the gap
        "min_price": 10.0,
        "min_dv_musd": 150.0,    # prior-day dollar volume floor
        "movers_top": 50,
        "slots_per_leg": 2,
        "position_frac": 0.25,   # of allocation per position; 4 x 25% = 100%
        "enable_short_leg": False,
        "live": False,
    }

    def __init__(self):
        self._day: str | None = None
        self._cands: dict[str, dict] = {}
        self._px0915: dict[str, float] = {}
        self._decided: set[str] = set()

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not (0.5 <= float(clean["min_gap_pct"]) <= 10.0):
            raise ValueError("min_gap_pct must be 0.5-10.0")
        if not (5.0 <= float(clean["min_turn_bp"]) <= 200.0):
            raise ValueError("min_turn_bp must be 5-200")
        if float(clean["min_price"]) < 1.0:
            raise ValueError("min_price must be >= 1")
        if not (10.0 <= float(clean["min_dv_musd"]) <= 10_000.0):
            raise ValueError("min_dv_musd must be 10-10000")
        if not (10 <= int(clean["movers_top"]) <= 100):
            raise ValueError("movers_top must be 10-100")
        if not (1 <= int(clean["slots_per_leg"]) <= 5):
            raise ValueError("slots_per_leg must be 1-5")
        if not (0.05 <= float(clean["position_frac"]) <= 0.5):
            raise ValueError("position_frac must be 0.05-0.5")
        return clean

    # ---------------------------------------------------------------- events

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        if event.type == "manual":
            if (event.payload or {}).get("cmd") == "scan":
                await self._scan(ctx)
                await self._snapshot(ctx)
                await self._decide(ctx, dry=True)
            else:
                await ctx.note({"skip": 'manual command is {"cmd": "scan"}'})
            return
        if event.type != "timer" or event.payload.get("kind") != "tick":
            return
        et_iso = event.payload.get("et")
        if not et_iso:
            return
        now_et = datetime.fromisoformat(et_iso)
        if now_et.weekday() >= 5:
            return
        hm = now_et.strftime("%H:%M")
        if hm == MARK_SCAN:
            await self._scan(ctx)
        elif hm == MARK_SNAP and self._cands:
            await self._snapshot(ctx)
        elif hm == MARK_DECIDE and self._cands:
            await self._decide(ctx)

    # ---------------------------------------------------------------- stages

    async def _scan(self, ctx: StrategyContext) -> None:
        today = datetime.now(ET).date().isoformat()
        self._day = today
        self._cands, self._px0915 = {}, {}
        p = ctx.params
        movers = await ctx.market.premarket_movers(int(p["movers_top"]))
        if not movers:
            await ctx.note({"skip": "movers screen returned nothing — no "
                                    "candidates today (screener coverage is "
                                    "a pre-registered assumption)"})
            return
        near = [m for m in movers
                if m["price"] >= float(p["min_price"])
                and abs(m["pct"]) * 100 >= float(p["min_gap_pct"]) * 0.5]
        volumes = await ctx.market.daily_dollar_volumes(
            sorted({m["symbol"] for m in near}))
        floor = float(p["min_dv_musd"]) * 1e6
        for m in near:
            dv = volumes.get(m["symbol"], 0.0)
            if dv >= floor:
                self._cands[m["symbol"]] = {
                    "prior_close": m["prior_close"], "dv": dv,
                    "pct_at_scan": round(m["pct"] * 100, 2)}
        await ctx.note({"scan": {
            "date": today, "movers": len(movers), "near": len(near),
            "candidates": {s: c["pct_at_scan"]
                           for s, c in self._cands.items()}}})

    async def _mark(self, sym: str, ctx: StrategyContext,
                    max_age_s: float) -> tuple[float | None, dict | None]:
        """A premarket price mark: the broker feed while it is fresh,
        consolidated public prints where IEX is dark (pre-reg Amendment 1).
        The study's panel WAS consolidated minute prints, so the fallback
        restores the measured basis rather than deviating from it — the
        same seam pead_nosip trades through every evening. `src` carries
        provenance into the journal; callers age-check via _usable_price
        exactly as before."""
        quote = await ctx.market.ah_quote(sym, max_age_s=max_age_s)
        return _usable_price(quote, max_age_s=max_age_s), quote

    async def _snapshot(self, ctx: StrategyContext) -> None:
        srcs: dict[str, str] = {}
        unusable: dict[str, str] = {}
        for sym in list(self._cands):
            px, quote = await self._mark(sym, ctx, 120.0)
            if px is not None:
                self._px0915[sym] = px
                srcs[sym] = str((quote or {}).get("src") or "broker")
            elif quote and quote.get("ts"):
                age_s = time.time() - float(quote["ts"]) / 1000.0
                unusable[sym] = f"quote {age_s:.0f}s stale"
            else:
                unusable[sym] = "no quote"
        # unusable is the coverage stopping rule's raw data — a morning of
        # "no quote" rows is a data gap, not a quiet market.
        await ctx.note({"snapshot_0915": {s: round(v, 4)
                                          for s, v in self._px0915.items()},
                        "src": srcs, "unusable": unusable})

    async def _decide(self, ctx: StrategyContext, dry: bool = False) -> None:
        p = ctx.params
        today = self._day or datetime.now(ET).date().isoformat()
        qualified, stood_down, skipped = [], [], {}
        for sym, cand in self._cands.items():
            key = f"{sym}:{today}"
            if key in self._decided:
                continue
            px15 = self._px0915.get(sym)
            if px15 is None:
                skipped[sym] = "no 09:15 snapshot"
                continue
            px27, quote = await self._mark(sym, ctx, 90.0)
            if px27 is None:
                skipped[sym] = "no fresh 09:27 quote"
                continue
            prior = float(cand["prior_close"] or 0)
            if prior <= 0:
                skipped[sym] = "no prior close"
                continue
            gap_pct = (px27 / prior - 1) * 100
            turn_bp = (px27 / px15 - 1) * 1e4
            if abs(gap_pct) < float(p["min_gap_pct"]):
                skipped[sym] = f"gap {gap_pct:+.2f}% under the gate"
                continue
            if abs(turn_bp) < float(p["min_turn_bp"]) or \
                    (turn_bp > 0) == (gap_pct > 0):
                skipped[sym] = (f"premarket not failing: gap {gap_pct:+.2f}%, "
                                f"turn {turn_bp:+.0f}bp")
                continue
            side = -1 if gap_pct > 0 else 1
            qualified.append({
                "symbol": sym, "side": side, "px": px27,
                "gap_pct": round(gap_pct, 2), "turn_bp": round(turn_bp, 0),
                "dv": cand["dv"],
                "rank": abs(gap_pct) * math.log10(max(cand["dv"], 10.0)),
                # prints have bid == ask, which _spread_of maps to None —
                # the cost stopping rule reads true NBBO spreads only
                "spread_pct": _spread_of(quote),
                "px_src": str((quote or {}).get("src") or "broker"),
            })
        slots = int(p["slots_per_leg"])
        qualified.sort(key=lambda q: -q["rank"])
        longs = [q for q in qualified if q["side"] > 0][:slots]
        shorts = [q for q in qualified if q["side"] < 0][:slots]
        if not bool(p["enable_short_leg"]):
            stood_down = shorts
            shorts = []

        try:
            book = await ctx.account()
        except Exception as exc:                              # noqa: BLE001
            await ctx.note({"skip": f"cannot size: {exc}"})
            return
        per_pos = float(book.get("equity") or 0.0) * float(p["position_frac"])

        detail = {"date": today, "qualified": qualified,
                  "stood_down_shorts": [q["symbol"] for q in stood_down],
                  "skipped": skipped, "per_position_usd": round(per_pos, 2)}
        if not longs and not shorts:
            await ctx.note({"no_trade": detail,
                            "why": "no failed gaps qualified"})
            return
        for q in longs + shorts:
            key = f"{q['symbol']}:{today}"
            self._decided.add(key)
            qty = int(per_pos // q["px"])
            if qty < 1:
                await ctx.note({"skip": f"{q['symbol']}: position size under "
                                        "one share", **detail})
                continue
            intent = TradeIntent(
                asset_class="equity",
                underlying=q["symbol"],
                legs=[{"symbol": q["symbol"], "side": q["side"], "ratio": 1,
                       "entry": q["side"] * q["px"], "auction": "open"}],
                qty=qty,
                entry_limit=q["side"] * q["px"],
                tp=None, sl=None,
                time_stop_utc=datetime.combine(
                    datetime.now(ET).date(), EXIT_ET, tzinfo=ET,
                ).astimezone(timezone.utc),
                reason=(f"gap fail fade {'long' if q['side'] > 0 else 'short'}"
                        f" {q['symbol']}: gap {q['gap_pct']:+.2f}%, premarket "
                        f"turn {q['turn_bp']:+.0f}bp"),
                dedupe_key=f"gff:{q['symbol']}:{today}",
                max_event_age_s=1200.0,
            )
            record = {**q, "qty": qty}
            if dry:
                # Manual scans are diagnostics, not decisions — they never
                # reach submit, live or simulated.
                await ctx.note({"dry_run": record, **detail})
                continue
            await ctx.note(
                {("decision" if bool(p["live"]) else "would_trade"): record})
            await ctx.submit(intent)
            ctx.log.info("gap_fail_fade %s %s x%d (gap %+0.2f%%, turn %+0.0fbp)",
                         "long" if q["side"] > 0 else "short", q["symbol"],
                         qty, q["gap_pct"], q["turn_bp"])


def _spread_of(quote: dict | None) -> float | None:
    if not quote:
        return None
    bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
    if bid <= 0 or ask <= bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2) * 100, 3)
