"""Afternoon 0DTE iron fly — the mechanical sleeve.

Evidence: research/0dte-vrp/ (notes/straddle_0dte_20260810_0202.md and
notes/flies_0dte_20260810_0214.md). On 639 QQQ sessions 2024-02..2026-08,
selling the 10:00 ATM straddle earns the seller +6.4bp of the underlying
per day (t 2.52) and the AFTERNOON is the reliable part of the decay: the
14:00 entry with 1.0%-width wings measured +3.2bp/day, t 3.51, 57% win,
worst day -68bp (bounded by the wings), gross Sharpe 2.20 — and the whole
1.0%-width row clears t 2.8-3.5 at every entry time, a ridge rather than a
lucky cell. Selection caveat: that is still the best cell of a 9-cell grid;
expect live closer to the row average (+2.5-2.8bp gross) minus ~0.5-1bp of
leg friction the minute-bar marks do not charge.

The structure is DEFINED-RISK BY CONSTRUCTION — short ATM call + put, long
both wings — which is also the only form this account can hold: naked short
calls are broker-rejected and short puts are cash-secured (~strike x 100),
so the collateral guard in strategy_runner would refuse anything less
covered. Margin per set is (width - credit) x 100, ~$260 at QQQ 560.

Clock: enter at `entry_et` (14:00 measured), exit via hard time stop at
`exit_et` (15:50 — before the expiry-liquidation window the broker's desk
runs on positions the account cannot exercise; the backtest held to expiry
intrinsic, so the live exit gives up a slice of terminal decay and pays an
exit spread. That deviation is pre-registered, not discovered later).
tp/sl are both None: a time-stop-only plan, bounded by allocation and the
strategy circuit breaker. Do not run with the breaker disabled.

Half-days: et_session() does not know early closes; on those ~4 days/year
the 14:00 entry meets a closing options market and place_trade's own hours
check refuses it — a journaled skip, not a bug.

No LLM, no forecast, no discretion. `live: false` until the one-set fill
proof per docs/briefs/strategy-authoring.md.
"""

from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from app.services.signals.events import Event
from app.strategies import register
from app.strategies.base import Strategy, StrategyContext, TradeIntent
from app.strategies.pead_flagship import _usable_price

ET = ZoneInfo("America/New_York")


def occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    return (f"{underlying}{expiry.strftime('%y%m%d')}{right}"
            f"{int(round(strike * 1000)):08d}")


@register
class AfternoonFly(Strategy):
    """Sell the 0DTE ATM straddle into the afternoon decay, wings on, time-stop out before the close."""

    kind = "afternoon_fly"
    subscriptions = ("timer", "manual")
    requires = frozenset({"options"})

    # Frozen at registration (ec841f8) + amendments 1-2 (same day, pre-live).
    # The band is the NET expectation, not the gross best cell — deviations
    # 1-3 in the doc are why they differ.
    registered = {
        "doc": "docs/pre-registration-afternoon-fly.md",
        "registered_commit": "ec841f8",
        "source_note": "research/0dte-vrp/notes/flies_0dte_20260810_0214.md",
        "window": "2024-02..2026-08",
        "metric": "bp_of_underlying_per_day",
        "metric_label": "net bp of underlying/day",
        "band": [1.5, 2.5],
        "sharpe_band": [1.3, 1.8],
        "backtest": {"value": 3.2, "t": 3.51, "sharpe": 2.20,
                     "basis": "gross, best-of-9 cell; honest prior +2.5-2.8"},
        "costs_assumed": "$0.02/structure marketability; 2-5c/side leg friction unpriced",
        "ladder": [{"stage": "note-mode", "sessions": 20},
                   {"stage": "one-set live", "sessions": 10},
                   {"stage": "full size", "sessions": None}],
    }

    default_params = {
        "underlying": "QQQ",       # where the premium lives; SPY measured thin
        "entry_et": "14:00",       # measured cell; 15:30 is the other live one
        "exit_et": "15:50",        # hard time stop, ahead of expiry handling
        "width_pct": 1.0,          # wings at ~1.0% of spot, min $1 strike
        # sizing: sets = floor(available * max_margin_frac / margin_per_set),
        # capped. margin_per_set = (width - credit) x 100.
        "max_sets": 10,
        "max_margin_frac": 0.5,
        # quote sanity: every leg two-sided and tight, credit a sane fraction
        # of width (the measured mean is ~54%; far outside that band means a
        # bad quote, not a good trade).
        "max_leg_spread_usd": 0.10,
        # 0.10 is QUOTE sanity, not a premium filter: the study's
        # +3.2bp/day includes thin-credit days (2026-08-10 priced 21%
        # and would have paid 86% of max profit), and a floor above
        # what the study accepted makes live a different strategy.
        "min_credit_frac": 0.10,
        "max_credit_frac": 0.90,
        "quote_max_age_s": 30.0,
        "slippage_usd": 0.02,      # credit given up to be marketable
        "live": False,
    }

    def __init__(self):
        self._done: set[str] = set()

    async def on_start(self, ctx: StrategyContext) -> None:
        # The global risk gate reads the STOCK stream's age, and options
        # plans always face that global check — on 2026-08-11 nothing else
        # subscribed a share symbol all day, the stream had "no ticks
        # received yet" at 14:00, and the one-set intent died on it. The
        # fly is options-only, so it feeds the gate itself: one refcounted
        # subscription to its own underlying.
        await ctx.market.subscribe_stock(str(ctx.params["underlying"]))

    async def on_stop(self, ctx: StrategyContext) -> None:
        await ctx.market.unsubscribe_stock(str(ctx.params["underlying"]))

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        clean = super().validate_params(params)
        if not str(clean["underlying"]).strip().isalpha():
            raise ValueError("underlying must be a plain ticker")
        for key in ("entry_et", "exit_et"):
            try:
                h, m = str(clean[key]).split(":")
                dtime(int(h), int(m))
            except Exception:
                raise ValueError(f"{key} must be HH:MM (ET)")
        if not (clean["entry_et"] < clean["exit_et"]):
            raise ValueError("entry_et must be before exit_et")
        if not (0.2 <= float(clean["width_pct"]) <= 5.0):
            raise ValueError("width_pct must be 0.2-5.0 (percent of spot)")
        if not (1 <= int(clean["max_sets"]) <= 100):
            raise ValueError("max_sets must be 1-100")
        if not (0.05 <= float(clean["max_margin_frac"]) <= 1.0):
            raise ValueError("max_margin_frac must be 0.05-1.0")
        if not (0.01 <= float(clean["max_leg_spread_usd"]) <= 1.0):
            raise ValueError("max_leg_spread_usd must be 0.01-1.00 dollars")
        lo, hi = float(clean["min_credit_frac"]), float(clean["max_credit_frac"])
        if not (0.0 < lo < hi <= 1.0):
            raise ValueError("credit fracs must satisfy 0 < min < max <= 1")
        if not (5.0 <= float(clean["quote_max_age_s"]) <= 300.0):
            raise ValueError("quote_max_age_s must be 5-300")
        if not (0.0 <= float(clean["slippage_usd"]) <= 0.25):
            raise ValueError("slippage_usd must be 0-0.25")
        return clean

    # ---------------------------------------------------------------- events

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        if event.type == "manual":
            if (event.payload or {}).get("cmd") == "scan":
                await self._enter(ctx, datetime.now(ET), forced=True)
            else:
                await ctx.note({"skip": 'manual command is {"cmd": "scan"}'})
            return
        if event.type != "timer" or event.payload.get("kind") != "tick":
            return
        if event.payload.get("session") != "rth":
            return
        et_iso = event.payload.get("et")
        if not et_iso:
            return
        now_et = datetime.fromisoformat(et_iso)
        if now_et.strftime("%H:%M") != str(ctx.params["entry_et"]):
            return
        await self._enter(ctx, now_et)

    # ----------------------------------------------------------------- entry

    async def _enter(self, ctx: StrategyContext, now_et: datetime,
                     forced: bool = False) -> None:
        today = now_et.date()
        key = today.isoformat()
        if key in self._done and not forced:
            return
        self._done.add(key)
        p = ctx.params
        underlying = str(p["underlying"]).upper()

        quote = await ctx.market.fetch_latest_stock_quote(underlying)
        spot = _usable_price(quote, max_age_s=float(p["quote_max_age_s"]))
        if spot is None:
            await ctx.note({"skip": f"no fresh {underlying} quote at entry"})
            return

        strike = float(round(spot))
        width = max(1.0, float(round(spot * float(p["width_pct"]) / 100)))
        spec = [("C", strike, -1), ("P", strike, -1),
                ("C", strike + width, 1), ("P", strike - width, 1)]
        symbols = [occ_symbol(underlying, today, right, k)
                   for right, k, _ in spec]

        await ctx.market.subscribe_options(symbols)
        await ctx.market.refresh_option_quotes(
            symbols, max_age_s=float(p["quote_max_age_s"]))

        legs, credit = [], 0.0
        for (right, k, side), sym in zip(spec, symbols):
            q = ctx.market.latest_quote(sym) or {}
            bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
            if bid <= 0 or ask <= bid:
                await ctx.note({"skip": f"leg {sym} has no two-sided book",
                                "spot": spot})
                return
            if ask - bid > float(p["max_leg_spread_usd"]) + 1e-9:
                await ctx.note({"skip": f"leg {sym} too wide: "
                                        f"{ask - bid:.2f} > "
                                        f"{p['max_leg_spread_usd']:.2f}",
                                "spot": spot})
                return
            mid = (bid + ask) / 2
            credit += side * mid
            legs.append({"symbol": sym, "right": right, "strike": k,
                         "expiry": today.isoformat(), "side": side,
                         "ratio": 1, "entry": round(mid, 4), "iv": 0.0,
                         "half_spread": round((ask - bid) / 2, 4)})

        # credit is signed net premium: short straddle minus wing cost,
        # negative when we are paid. Refuse structures whose quotes imply
        # nonsense rather than trading them.
        frac = -credit / width
        if not (float(p["min_credit_frac"]) <= frac <= float(p["max_credit_frac"])):
            await ctx.note({"skip": f"credit {-credit:.2f} is {frac:.0%} of the "
                                    f"{width:.0f} width — outside "
                                    f"[{p['min_credit_frac']:.0%}, "
                                    f"{p['max_credit_frac']:.0%}]",
                            "spot": spot})
            return

        entry_limit = round(credit + float(p["slippage_usd"]), 2)
        margin_per_set = (width + entry_limit) * 100.0   # width - |credit|
        try:
            book = await ctx.account()
        except Exception as exc:                          # noqa: BLE001
            await ctx.note({"skip": f"cannot size: {exc}"})
            return
        available = float(book.get("available") or 0.0)
        budget = available * float(p["max_margin_frac"])
        sets = min(int(p["max_sets"]),
                   int(budget // margin_per_set) if margin_per_set > 0 else 0)
        detail = {
            "underlying": underlying, "spot": round(spot, 2),
            "strike": strike, "width": width, "credit": round(-credit, 2),
            "credit_frac": round(frac, 3), "entry_limit": entry_limit,
            "margin_per_set": round(margin_per_set, 2), "sets": sets,
            "available": round(available, 2),
            "legs": [{"s": leg["symbol"], "mid": leg["entry"],
                      "spread": leg["half_spread"] * 2} for leg in legs],
        }
        if sets < 1:
            await ctx.note({"skip": "margin budget below one set", **detail})
            return

        h, m = map(int, str(p["exit_et"]).split(":"))
        intent = TradeIntent(
            asset_class="option", underlying=underlying, legs=legs, qty=sets,
            entry_limit=entry_limit, tp=None, sl=None,
            time_stop_utc=datetime.combine(today, dtime(h, m), tzinfo=ET)
            .astimezone(timezone.utc),
            reason=f"afternoon fly {underlying} K={strike:g} w={width:g} "
                   f"x{sets}",
            dedupe_key=f"fly:{underlying}:{key}",
        )
        await ctx.note(
            {("decision" if bool(p["live"]) else "would_trade"): detail})
        await ctx.submit(intent)
        ctx.log.info("afternoon_fly %s K=%g w=%g x%d credit %.2f",
                     underlying, strike, width, sets, -credit)
