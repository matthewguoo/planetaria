"""Ride the second day of an earnings pop — RTH only, long only, no LLM.

Evidence: research/pead-llm-gate/notes/day2_mech_20260810_0355.md (the
effect), day2_sim_20260810_0416.md (the account shape), and
day2_shift_20260810_2123.md (the entry): after an UP earnings reaction of
>= 5% (clean anchors, AMC events), buying the SECOND session at 09:32 and
selling at 15:55 earned +29.2bp/trade net@6 (t 3.12), ~0.55 trades/day
at 4 slots, account Sharpe 0.80 over 2016-2026.

ADMITTED AS A STACK COMPONENT (Matthew, 2026-08-11): the standalone-
Sharpe bar (>= 1) is explicitly waived because this sleeve's risk-hours
(T+2 09:32-15:55) are exactly the hours gap_fail_fade's dollars sit idle
— the 09:32 entry exists so gff's 09:28-09:31:30 auction round trip
completes first on the same capital. Measured tail behavior across
sleeves is independence-or-better (day2 averaged +134bp on the delayed
sleeve's 20 worst days). See docs/pre-registration-day2-pop.md.

Clock (ET, weekdays):
  09:05  scan — reporters from the estimate journal (ctx.reporters, the
         restart-proof calendar read): AMC reporters over the last
         `lookback_days` whose SECOND session after the report date is
         today; reaction = close(first session after D) / close(anchor
         session <= D) - 1 >= min_move_pct, UP only; dollar-volume floor
         and top-`slots` ranking by prior-session dollar volume.
  09:32  enter — marketable limit at the live quote, long, equal slots.
  15:55  hard time stop — the ExitEnforcer takes the book off. No tp, no
         sl (event books never carry narrow stops — wick study).

BMO reporters are out of scope (the studied panel is AMC events; the BMO
twin is a separate queued study). Calendar coverage is Finnhub's (~2/3 of
reporters measured) — a stated pre-registration assumption, journaled per
scan so the forward test can price it.

Run in note-mode (`live: false`) until the pre-registration's gates pass.
"""

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent
from app.strategies.pead_flagship import _usable_price

ET = ZoneInfo("America/New_York")

MARK_SCAN = "09:05"
MARK_ENTER = "09:32"
EXIT_ET = dtime(15, 55)


@register
class Day2Pop(Strategy):
    """Buy the second morning of a >=5% earnings pop at 09:32, sell 15:55."""

    kind = "day2_pop"
    subscriptions = ("timer", "manual")
    requires = frozenset()      # RTH quotes + day orders: nothing gated

    registered = {
        "doc": "docs/pre-registration-day2-pop.md",
        "registered_commit": "430307f",
        "source_note": "research/pead-llm-gate/notes/day2_shift_20260810_2123.md",
        "window": "2016-01..2026-08",
        "metric": "net_bp_per_trade",
        "metric_label": "net bp/trade (long, 09:32 entry)",
        "band": [10.0, 30.0],
        "sharpe_band": [0.5, 0.9],
        "trades_per_day_band": [0.3, 0.8],
        "backtest": {"value": 29.2, "t": 3.12, "sharpe": 0.80,
                     "basis": "net@6bp, 4 slots, entry 09:32 close, exit 15:55"},
        "costs_assumed": "6bp round trip on $100M+ dollar-volume books",
        "ladder": [{"stage": "note-mode", "sessions": 20},
                   {"stage": "one-share live", "sessions": 10},
                   {"stage": "full size", "sessions": None}],
    }

    default_params = {
        "min_move_pct": 5.0,     # clean-anchor reaction gate, UP side only
        "min_price": 5.0,
        "min_dv_musd": 100.0,    # prior-session dollar-volume floor
        "slots": 4,
        "position_frac": 0.25,   # of allocation per position; 4 x 25% = 100%
        "lookback_days": 5,      # calendar dates queried for AMC reporters
        "live": False,
    }

    def __init__(self):
        self._day: str | None = None
        self._cands: list[dict] = []
        self._decided: set[str] = set()

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not (1.0 <= float(clean["min_move_pct"]) <= 50.0):
            raise ValueError("min_move_pct must be 1-50")
        if float(clean["min_price"]) < 1.0:
            raise ValueError("min_price must be >= 1")
        if not (10.0 <= float(clean["min_dv_musd"]) <= 10_000.0):
            raise ValueError("min_dv_musd must be 10-10000")
        if not (1 <= int(clean["slots"]) <= 8):
            raise ValueError("slots must be 1-8")
        if not (0.05 <= float(clean["position_frac"]) <= 0.5):
            raise ValueError("position_frac must be 0.05-0.5")
        if not (2 <= int(clean["lookback_days"]) <= 10):
            raise ValueError("lookback_days must be 2-10")
        return clean

    # ---------------------------------------------------------------- events

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        if event.type == "manual":
            if (event.payload or {}).get("cmd") == "scan":
                await self._scan(ctx)
                await self._enter(ctx, dry=True)
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
        elif hm == MARK_ENTER:
            if self._day != now_et.date().isoformat():
                await self._scan(ctx)          # late boot: scan now, then act
            if self._cands:
                await self._enter(ctx)

    # ---------------------------------------------------------------- stages

    async def _scan(self, ctx: StrategyContext) -> None:
        today = datetime.now(ET).date()
        self._day = today.isoformat()
        self._cands = []
        p = ctx.params

        # AMC reporters from the estimate journal, most recent report wins.
        reporters: dict[str, str] = {}
        rows_seen = 0
        for back in range(int(p["lookback_days"]), 0, -1):
            d = (today - timedelta(days=back)).isoformat()
            for row in await ctx.reporters(d):
                rows_seen += 1
                if str(row.get("when")) == "amc" and row.get("symbol"):
                    reporters[str(row["symbol"]).upper()] = str(row["date"])
        if not reporters:
            await ctx.note({"skip": "no AMC reporters in the estimate "
                                    "journal for the lookback window "
                                    "(calendar dark or quiet week)",
                            "rows_seen": rows_seen})
            return

        volumes = await ctx.market.daily_dollar_volumes(sorted(reporters))
        floor = float(p["min_dv_musd"]) * 1e6
        liquid = {s: d for s, d in reporters.items()
                  if volumes.get(s, 0.0) >= floor}

        rejects: dict[str, str] = {}
        cands: list[dict] = []
        for sym, report_date in sorted(liquid.items()):
            closes = await ctx.market.daily_closes(sym, days=7)
            if len(closes) < 2:
                rejects[sym] = "no daily closes"
                continue
            after = [c for c in closes if c["date"] > report_date]
            anchor = [c for c in closes if c["date"] <= report_date]
            if not anchor:
                rejects[sym] = "no anchor close at/before the report date"
                continue
            if len(after) != 1:
                # 0 = reaction session is today (too early); 2+ = day2 has
                # already passed. Either way, not today's trade.
                rejects[sym] = f"{len(after)} sessions since the report"
                continue
            a, r = float(anchor[-1]["close"]), float(after[0]["close"])
            if a <= 0:
                rejects[sym] = "bad anchor close"
                continue
            move_pct = (r / a - 1) * 100
            if move_pct < float(p["min_move_pct"]):
                rejects[sym] = f"reaction {move_pct:+.1f}% under the gate " \
                               f"(UP side only)"
                continue
            if r < float(p["min_price"]):
                rejects[sym] = f"price {r:.2f} under the floor"
                continue
            cands.append({"symbol": sym, "report_date": report_date,
                          "move_pct": round(move_pct, 2),
                          "reaction_close": r,
                          "dv": volumes.get(sym, 0.0)})

        cands.sort(key=lambda c: -c["dv"])
        self._cands = cands[: int(p["slots"])]
        await ctx.note({"scan": {
            "date": self._day, "reporters": len(reporters),
            "past_dv_floor": len(liquid),
            "candidates": {c["symbol"]: c["move_pct"] for c in self._cands},
            "overflow": [c["symbol"] for c in cands[int(p["slots"]):]],
            "rejects": rejects}})

    async def _enter(self, ctx: StrategyContext, dry: bool = False) -> None:
        p = ctx.params
        today = self._day or datetime.now(ET).date().isoformat()
        try:
            book = await ctx.account()
        except Exception as exc:                              # noqa: BLE001
            await ctx.note({"skip": f"cannot size: {exc}"})
            return
        per_pos = float(book.get("equity") or 0.0) * float(p["position_frac"])

        for c in self._cands:
            sym = c["symbol"]
            key = f"{sym}:{today}"
            if key in self._decided:
                continue
            quote = await ctx.market.fetch_latest_stock_quote(sym)
            px = _usable_price(quote, max_age_s=90.0)
            if px is None:
                await ctx.note({"skip": f"{sym}: no fresh 09:32 quote"})
                continue
            qty = int(per_pos // px)
            if qty < 1:
                await ctx.note({"skip": f"{sym}: position size under one "
                                        f"share at {px:.2f}"})
                continue
            self._decided.add(key)
            intent = TradeIntent(
                asset_class="equity",
                underlying=sym,
                legs=[{"symbol": sym, "side": 1, "ratio": 1, "entry": px}],
                qty=qty,
                entry_limit=px,
                tp=None, sl=None,
                time_stop_utc=datetime.combine(
                    datetime.now(ET).date(), EXIT_ET, tzinfo=ET,
                ).astimezone(timezone.utc),
                reason=(f"day2 pop long {sym}: reaction {c['move_pct']:+.1f}% "
                        f"on {c['report_date']} (AMC), second session"),
                dedupe_key=f"d2:{sym}:{today}",
                max_event_age_s=1200.0,
            )
            record = {**{k: c[k] for k in ("symbol", "report_date",
                                           "move_pct")}, "px": px, "qty": qty}
            if dry:
                await ctx.note({"dry_run": record})
                continue
            await ctx.note(
                {("decision" if bool(p["live"]) else "would_trade"): record})
            await ctx.submit(intent)
            ctx.log.info("day2_pop long %s x%d (reaction %+.1f%%)",
                         sym, qty, c["move_pct"])
