"""Trade lifecycle: validated placement -> DB-persisted plan -> Alpaca
order -> fill tracking -> exit handoff to the enforcer.

Discipline invariants enforced here, not in the UI:
- No order without TP, SL, and time stop.
- Plan row committed BEFORE the order hits Alpaca.
- Which broker environment this process talks to is fixed at boot by
  TRADING_MODE (config.validate_paper_lock): the paper server refuses
  live outright; the isolated live_manual server accepts entries only
  from the human ticket (strategy_id refused in place_trade) with the
  strategy plane never constructed.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from app.models.trade import TradePlan, as_utc, utcnow
from app.services.alpaca import AlpacaService
from app.services.market_clock import equity_session
from app.services.plan_fsm import (
    TERMINAL_STATES,
    PlanEvent,
    PlanState,
    PlanStateMachine,
)

log = logging.getLogger("app.trade")

TICK = 0.01


def auction_submit_ok(now_utc: datetime | None = None) -> bool:
    """Whether today's opening auction is still joinable. The broker's OPG
    cutoff is 09:28 ET; between 09:28 and the close there is no auction
    left to join today (evening submissions queue for tomorrow's, which is
    a different trade and must be a deliberate one)."""
    from zoneinfo import ZoneInfo

    now = (now_utc or datetime.now(timezone.utc)).astimezone(
        ZoneInfo("America/New_York"))
    hm = now.hour * 60 + now.minute
    return not (9 * 60 + 28 <= hm < 16 * 60)


def round_tick(x: float) -> float:
    """Round to a cent, preserving sign (negative = net credit for MLEG
    orders per Alpaca semantics). Never returns exactly 0."""
    r = round(round(x / TICK) * TICK, 2)
    if r == 0.0:
        r = TICK if x >= 0 else -TICK
    return r


def position_mid_from_quotes(legs: list[dict], quotes: dict[str, dict]) -> float | None:
    """Side-weighted net mid per share; None if any leg quote is missing."""
    total = 0.0
    for leg in legs:
        quote = quotes.get(leg["symbol"])
        if not quote or not (quote.get("bid") or quote.get("ask")):
            return None
        total += leg["side"] * leg.get("ratio", 1) * quote["mid"]
    return total


def quality_fill(snapshot: dict, kind: str, fill: float) -> dict:
    """Realized execution quality for one fill against its submit snapshot.

    Signed-premium convention: entry cost = fill − fair (paying a HIGHER net
    premium is worse, for credit entries too); exit cost = fair − fill
    (receiving LESS is worse). spread_capture = 1 − cost/half_spread: 1.0 =
    filled at fair value, 0.0 = crossed the whole half-spread, negative =
    worse than the quoted touch. This is the effective-vs-quoted-spread
    metric from Muravyev & Pearson (RFS 2020) applied per contract-set.
    """
    cost = (fill - snapshot["fair"]) if kind == "entry" else (snapshot["fair"] - fill)
    out = {**snapshot, "fill": round(fill, 4), "cost": round(cost, 4)}
    if snapshot.get("half_spread"):
        out["spread_capture"] = round(1 - cost / snapshot["half_spread"], 3)
    return out


def staged_price_ok(entry_limit: float, live_mid: float, half_spread: float
                    ) -> tuple[bool, float, float]:
    """Fast-tape staleness check for a staged entry price.

    (ok, drift, tolerance): the staged limit may deviate from the live
    position value by at most max(2x half-spread, 8% of |mid|) — wide books
    get spread-scaled slack, tight books get the percentage floor. Signed
    premiums compare on the same axis (credit entries are negative).
    """
    drift = abs(entry_limit - live_mid)
    tolerance = max(2 * half_spread, 0.08 * max(abs(live_mid), 0.05))
    return drift <= tolerance, drift, tolerance


def parse_occ_symbol(symbol: str) -> dict | None:
    """OCC option symbol -> {underlying, expiry, right, strike}; None if not OCC."""
    try:
        if len(symbol) < 16:
            return None
        strike = int(symbol[-8:]) / 1000.0
        right = symbol[-9].upper()
        if right not in ("C", "P"):
            return None
        yymmdd = symbol[-15:-9]
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        underlying = symbol[:-15].strip()
        if not underlying:
            return None
        return {"underlying": underlying, "expiry": expiry, "right": right, "strike": strike}
    except (ValueError, IndexError):
        return None


def mark_with_fallback(
    legs: list[dict], quotes: dict[str, dict | None], broker_prices: dict[str, float],
) -> tuple[float | None, str | None]:
    """Position mark for the UI, with the broker's own position prices as the
    fallback when the quote cache has nothing usable (extended hours on the
    free tier, the weekend). Returns (mid, source): source "quote" when
    every leg had a live quote, "broker" when the broker's current_price
    filled the gaps, None when neither could mark the position. The enforcer
    never reads this — it marks off its own quote map; this is sight, not
    exits."""
    mid = position_mid_from_quotes(legs, quotes)
    if mid is not None:
        return mid, "quote"
    merged: dict[str, dict] = {}
    for leg in legs:
        quote = quotes.get(leg["symbol"])
        if quote and (quote.get("bid") or quote.get("ask")):
            merged[leg["symbol"]] = quote
            continue
        price = broker_prices.get(leg["symbol"])
        if price is None or price <= 0:
            return None, None
        merged[leg["symbol"]] = {"bid": price, "ask": price, "mid": price}
    mid = position_mid_from_quotes(legs, merged)
    return (mid, "broker") if mid is not None else (None, None)


def merge_holdings(positions: list[dict], plans: list[dict]) -> list[dict]:
    """Broker positions × open plans by leg symbol. A position managed by a
    plan carries that plan's id and exits (`protected` = it has a stop);
    everything else is `protected: False` — the overview's loudest column
    on a live account. Options group under their underlying for display."""
    by_symbol: dict[str, dict] = {}
    for plan in plans:
        for leg in plan.get("legs") or []:
            by_symbol[leg["symbol"]] = plan
    out: list[dict] = []
    for pos in positions:
        plan = by_symbol.get(pos["symbol"])
        occ = pos.get("occ")
        row = dict(pos)
        row["underlying"] = occ["underlying"] if occ else pos["symbol"]
        row["plan_id"] = plan["id"] if plan else None
        row["plan_status"] = plan["status"] if plan else None
        row["sl"] = plan.get("sl_premium") if plan else None
        row["tp"] = plan.get("tp_premium") if plan else None
        row["time_stop_utc"] = plan.get("time_stop_utc") if plan else None
        row["protected"] = bool(plan and plan.get("sl_premium") is not None)
        out.append(row)
    return out


class TradeService:
    def __init__(self, db, alpaca: AlpacaService, market, risk):
        self.db = db
        self.alpaca = alpaca
        self.market = market
        self.risk = risk
        self.fsm = PlanStateMachine(db, market.broadcast)
        self.enforcer = None  # set by ExitEnforcer at startup (circular)
        self._account_cache: tuple[float, dict] | None = None
        self._positions_cache: tuple[float, list[dict]] | None = None
        self._shortable_cache: dict[str, tuple[float, bool]] = {}

    # ------------------------------------------------------------- account

    async def get_account(self) -> dict:
        if self._account_cache and time.monotonic() - self._account_cache[0] < 10:
            return self._account_cache[1]
        cfg = getattr(self.alpaca, "settings", None)
        paper = bool(getattr(cfg, "alpaca_paper", True))
        mode = getattr(cfg, "trading_mode", "paper")
        if not self.alpaca.configured:
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0,
                    "daytrade_count": 0, "status": "NO_KEYS",
                    "paper": paper, "mode": mode}
        acct = await self.alpaca.call(self.alpaca.trading.get_account, retries=1)
        out = {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "daytrade_count": int(acct.daytrade_count or 0),
            "status": str(acct.status.value if hasattr(acct.status, "value") else acct.status),
            "paper": paper,
            "mode": mode,
        }
        self._account_cache = (time.monotonic(), out)
        return out

    # ---------------------------------------------------- broker positions

    async def broker_positions(self, max_age_s: float = 5.0) -> list[dict]:
        """Live positions straight from the Alpaca account, normalized.
        These are the ground truth; TradePlans are our management layer on
        top. Cached briefly to keep the positions poll off the rate budget."""
        if self._positions_cache and time.monotonic() - self._positions_cache[0] < max_age_s:
            return self._positions_cache[1]
        if not self.alpaca.configured:
            return []
        positions = await self.alpaca.call(self.alpaca.trading.get_all_positions, retries=1)
        out: list[dict] = []
        for pos in positions:
            occ = parse_occ_symbol(pos.symbol)
            asset_class = str(getattr(pos, "asset_class", "") or "").lower()
            # OCC parse is the reliable signal; asset_class strings vary.
            is_option = occ is not None and ("option" in asset_class or not asset_class)
            qty = float(pos.qty)

            def _f(name: str) -> float | None:
                value = getattr(pos, name, None)
                return float(value) if value is not None else None

            out.append(
                {
                    "symbol": pos.symbol,
                    "qty": qty,
                    "side": 1 if qty > 0 else -1,
                    "asset_class": "option" if is_option else "stock",
                    "avg_entry_price": _f("avg_entry_price") or 0.0,
                    "current_price": _f("current_price"),
                    "market_value": _f("market_value"),
                    "unrealized_pl": _f("unrealized_pl"),
                    # Overview fields (the holdings table sorts on these).
                    "unrealized_plpc": _f("unrealized_plpc"),
                    "unrealized_intraday_pl": _f("unrealized_intraday_pl"),
                    "change_today": _f("change_today"),
                    "lastday_price": _f("lastday_price"),
                    "cost_basis": _f("cost_basis"),
                    "occ": occ if is_option else None,
                }
            )
        self._positions_cache = (time.monotonic(), out)
        return out

    async def holdings(self, plans: list | None = None) -> list[dict]:
        """EVERY broker position, each stamped with the plan that manages it
        (stop / target / time stop) or `plan_id: None` — the account
        overview's one list. See merge_holdings."""
        if plans is None:
            plans = await self.risk.open_plans()
        universe = getattr(getattr(self.alpaca, "settings", None), "_unused", None)  # placeholder, names come from the route
        del universe
        return merge_holdings(await self.broker_positions(), [p.to_dict() for p in plans])

    async def untracked_positions(self, plans: list | None = None) -> list[dict]:
        """Broker positions not covered by any open plan's legs. Callers that
        already hold the open plans pass them to skip the second query."""
        if plans is None:
            plans = await self.risk.open_plans()
        covered = {leg["symbol"] for plan in plans for leg in plan.legs}
        return [p for p in await self.broker_positions() if p["symbol"] not in covered]

    async def adopt_positions(
        self,
        symbols: list[str],
        tp_pct: float,
        sl_pct: float,
        time_stop_utc: datetime,
    ) -> list[dict]:
        """Fold untracked broker positions into managed TradePlans so the
        enforcer runs TP/SL/time exits on them. Options adopt as one
        multi-leg plan per underlying (chunked at 4 legs, the MLEG close
        limit); stock positions adopt as single-leg equity plans — one per
        symbol, floor(qty) whole shares (TradePlan.qty is integral; any
        fractional residue stays visible as untracked and is closed by hand
        at the broker). tp_pct/sl_pct are fractions of the entry basis."""
        from math import floor, gcd

        untracked = {p["symbol"]: p for p in await self.untracked_positions()}
        chosen = [untracked[s] for s in symbols if s in untracked and untracked[s]["occ"]]
        stocks = [untracked[s] for s in symbols
                  if s in untracked and not untracked[s]["occ"]
                  and untracked[s].get("asset_class") == "stock"]
        if not chosen and not stocks:
            raise ValueError("no adoptable untracked positions in selection")

        account_name = getattr(getattr(self.alpaca, "settings", None),
                               "alpaca_account_name", None)
        by_underlying: dict[str, list[dict]] = {}
        for p in chosen:
            by_underlying.setdefault(p["occ"]["underlying"], []).append(p)

        adopted: list[dict] = []
        for p in sorted(stocks, key=lambda x: x["symbol"]):
            shares = floor(abs(p["qty"]))
            if shares < 1:
                log.warning("skipping %s: %.4f shares is under one whole "
                            "share (fractional adoption unsupported)",
                            p["symbol"], abs(p["qty"]))
                continue
            entry = p["side"] * p["avg_entry_price"]
            if abs(entry) < TICK:
                entry = TICK  # zero-cost basis: manage on absolute price
            plan = TradePlan(
                underlying=p["symbol"][:12],
                strategy="adopted",
                asset_class="equity",
                legs=[{"symbol": p["symbol"], "side": p["side"], "ratio": 1,
                       "entry": p["avg_entry_price"]}],
                qty=shares,
                entry_limit=round(entry, 4),
                tp_premium=round(entry + abs(entry) * tp_pct, 4),
                sl_premium=round(entry - abs(entry) * sl_pct, 4),
                time_stop_utc=time_stop_utc,
                status="filled",
                filled_qty=shares,
                fill_premium=round(entry, 4),
                account=account_name,
                notes="adopted from broker positions",
            )
            async with self.db.session() as session:
                session.add(plan)
                await session.commit()
                await session.refresh(plan)
            self.market.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
            if self.enforcer:
                await self.enforcer.arm(plan.id)
            log.info("adopted %d shares of %s into plan %s",
                     shares, p["symbol"], plan.id)
            adopted.append(plan.to_dict())
        for underlying, group in sorted(by_underlying.items()):
            group.sort(key=lambda p: (p["occ"]["expiry"], p["occ"]["strike"]))
            for chunk_start in range(0, len(group), 4):
                chunk = group[chunk_start : chunk_start + 4]
                qtys = [max(int(abs(p["qty"])), 1) for p in chunk]
                sets = qtys[0]
                for q in qtys[1:]:
                    sets = gcd(sets, q)
                sets = max(sets, 1)

                legs, entry = [], 0.0
                for p, q in zip(chunk, qtys):
                    ratio = max(q // sets, 1)
                    legs.append(
                        {
                            "symbol": p["symbol"],
                            "right": p["occ"]["right"],
                            "strike": p["occ"]["strike"],
                            "expiry": p["occ"]["expiry"],
                            "side": p["side"],
                            "ratio": ratio,
                            "entry": p["avg_entry_price"],
                            "iv": 0.0,
                        }
                    )
                    entry += p["side"] * ratio * p["avg_entry_price"]
                if abs(entry) < TICK:
                    entry = TICK  # zero-cost basis: manage on absolute premium

                plan = TradePlan(
                    underlying=underlying[:12],
                    strategy="adopted",
                    legs=legs,
                    qty=sets,
                    entry_limit=round(entry, 4),
                    tp_premium=round(entry + abs(entry) * tp_pct, 4),
                    sl_premium=round(entry - abs(entry) * sl_pct, 4),
                    time_stop_utc=time_stop_utc,
                    status="filled",
                    filled_qty=sets,
                    fill_premium=round(entry, 4),
                    account=account_name,
                    notes="adopted from broker positions",
                )
                async with self.db.session() as session:
                    session.add(plan)
                    await session.commit()
                    await session.refresh(plan)
                self.market.broadcast.publish("plans", {"t": "plan", "plan": plan.to_dict()})
                if self.enforcer:
                    await self.enforcer.arm(plan.id)
                log.info("adopted %d broker legs into plan %s (%s)", len(legs), plan.id, underlying)
                adopted.append(plan.to_dict())
        return adopted

    # ------------------------------------------------------------ placement

    async def place_trade(self, payload: dict) -> dict:
        """payload: {underlying, strategy, legs:[{symbol,right,strike,expiry,side,ratio,entry,iv}],
        qty, entry_limit, tp_premium, sl_premium, time_stop_utc,
        asset_class?: option|equity, extended_hours?: bool, strategy_id?}.

        Equity plans are single-leg shares: "premium" is the share price,
        multiplier 1, and extended_hours puts the DAY limit in the 24/5
        book (verified 2026-08-04: fills premarket/postmarket/overnight)."""
        if not self.alpaca.configured:
            raise ValueError("Alpaca keys not configured - trading unavailable")

        if getattr(getattr(self.alpaca, "settings", None),
                   "trading_mode", "paper") == "live_manual":
            # LIVE ENTRY GATES. Entries on the live server are discretionary
            # by definition (the strategy plane does not exist in this
            # process); refusing strategy_id closes the spoofable payload
            # field and keeps every live plan under the manual risk rules.
            if payload.get("strategy_id") is not None:
                raise ValueError(
                    "automation ids are not accepted on the live server - "
                    "manual entries only")
            # The live account is options level 2: long single-leg only.
            # Refuse here with a clear message instead of letting the broker
            # reject the MLEG/short leg downstream.
            if (payload.get("asset_class") or "option") == "option":
                option_legs = payload.get("legs") or []
                if len(option_legs) != 1 or any(
                        int(leg.get("side", 0)) < 0 for leg in option_legs):
                    raise ValueError(
                        "live account is options level 2 - long single-leg "
                        "calls/puts only (no spreads, no short legs)")

        legs = payload["legs"]
        qty = int(payload["qty"])
        entry_limit = float(payload["entry_limit"])
        asset_class = payload.get("asset_class") or "option"
        # Exit combinations. BOTH null = a time-stop-only plan (the exit
        # enforcer runs it without thresholds; risk bounds it at the strategy
        # level). Equity additionally allows SL-WITHOUT-TP — the swing
        # contract: a hard stop under an open-ended winner. A target without
        # a stop is not an exit plan anywhere.
        tp = payload.get("tp_premium")
        sl = payload.get("sl_premium")
        bracketless = tp is None and sl is None
        if not bracketless:
            if sl is None:
                raise ValueError(
                    "a take-profit without a stop is not an exit plan — set "
                    "sl_premium too (or null both for time-stop-only)")
            if tp is None and asset_class != "equity":
                raise ValueError(
                    "tp_premium and sl_premium must both be set, or both be "
                    "null for a time-stop-only plan — SL-only is an equity "
                    "swing shape")
            sl = float(sl)
            tp = float(tp) if tp is not None else None
        extended_hours = bool(payload.get("extended_hours", False))
        ts_raw = payload.get("time_stop_utc")
        if not ts_raw:
            # The column is non-null and the enforcer's wake math keys on it.
            # Open-ended swing holds send a FAR backstop instead of nothing.
            raise ValueError(
                "time_stop_utc required — for an open-ended swing hold send "
                "a far backstop (e.g. +30 days)")
        time_stop = datetime.fromisoformat(ts_raw)
        if time_stop.tzinfo is None:
            time_stop = time_stop.replace(tzinfo=timezone.utc)

        if asset_class not in ("option", "equity"):
            raise ValueError("asset_class must be option or equity")
        if qty < 1:
            raise ValueError("qty must be >= 1")
        if not legs or len(legs) > 4:
            raise ValueError("1-4 legs required")
        if asset_class == "equity" and len(legs) != 1:
            raise ValueError("equity plans are single-leg (one symbol of shares)")
        if asset_class == "option":
            if extended_hours:
                raise ValueError("extended_hours is an equity-only flag")
            for leg in legs:
                if any(leg.get(k) is None for k in ("right", "strike", "expiry")):
                    raise ValueError("option legs require right/strike/expiry")
        # AUCTION ENTRIES. A leg carrying auction="open" becomes a
        # market-on-open order: the fill IS the opening print, which is the
        # only fill some edges exist at. The engine's no-market-outside-RTH
        # rule is about SILENT queuing; OPG queues to the auction by design
        # and is refused here once today's cutoff has passed.
        at_open = bool(legs and legs[0].get("auction") == "open")
        if at_open:
            if asset_class != "equity":
                raise ValueError("auction=open entries are equity-only")
            if extended_hours:
                raise ValueError("auction=open and extended_hours are exclusive")
            if not auction_submit_ok():
                raise ValueError(
                    "auction=open orders must reach the broker before "
                    "09:28 ET — today's opening auction is not joinable")
        if abs(entry_limit) < TICK:
            raise ValueError("net premium must be at least one tick")
        if sl is not None and tp is not None and not (sl < entry_limit < tp):
            raise ValueError(
                f"exits must bracket entry: SL {sl} < entry {entry_limit} < TP {tp}"
            )
        if sl is not None and tp is None and not (sl < entry_limit):
            raise ValueError(
                f"stop must sit below entry on the value axis: SL {sl} < entry {entry_limit}"
            )

        account = await self.get_account()
        is_short_equity = asset_class == "equity" and legs[0]["side"] < 0
        if asset_class == "equity":
            # Shares: notional is the whole story — no margin formula, no
            # expiry. max_loss is the SL distance in dollars (x1); the signed
            # convention makes it positive for shorts too (SL sits below the
            # negative entry credit on the position-value axis).
            max_loss = None if sl is None else (entry_limit - sl) * qty
            # Capital consumed: longs claim their notional. Shorts claim
            # Reg-T initial — 150% of market value (100% proceeds held +50%)
            # — so the per-name and gross caps count the true BP bite.
            entry_cost = abs(entry_limit) * qty * (1.5 if is_short_equity else 1.0)
            expiry = ""
        else:
            # Capital consumed: broker-margin estimate. Naked shorts charge the
            # standard 20%-of-spot formula rather than full cash-secured value —
            # a jade lizard's short put must not consume $70k/set of "BP" that
            # Alpaca itself would never require in a margin account.
            max_loss = None if sl is None else (entry_limit - sl) * 100 * qty
            from app.services.options_math import Leg, bp_per_set_estimate

            spot = self.market.spot(payload["underlying"].upper())
            leg_objs = [Leg.from_dict(leg) for leg in legs]
            if spot > 0:
                entry_cost = bp_per_set_estimate(leg_objs, spot) * qty
            elif entry_limit > 0:
                entry_cost = entry_limit * 100 * qty
            else:
                from app.services.options_math import structural_max_loss

                structural = structural_max_loss(leg_objs)
                if structural is not None:
                    entry_cost = structural * 100 * qty
                else:
                    # Last resort. Without a stop there is no max_loss to
                    # scale, so fall back to the credit/debit itself — an
                    # under-estimate, but the gross-exposure cap still bites
                    # and inventing a number here would be worse.
                    entry_cost = (max_loss * 3 if max_loss is not None
                                  else abs(entry_limit) * 100 * qty)
            expiry = max(leg["expiry"] for leg in legs)
        # Force-refresh the legs' quotes BEFORE validation, and fill any
        # missing half_spread from the live book — the illiquidity gate must
        # not be skippable by a client simply omitting the field.
        leg_symbols = [leg["symbol"] for leg in legs]
        if self.alpaca.configured:
            try:
                if asset_class == "equity":
                    await self.market.fetch_latest_stock_quote(leg_symbols[0])
                else:
                    await self.market.refresh_option_quotes(leg_symbols, max_age_s=1.5)
            except Exception as exc:
                log.warning("entry-guard quote refresh failed: %s", exc)
            for leg in legs:
                if leg.get("half_spread") is None:
                    q = self.market.latest_quote(leg["symbol"]) or {}
                    bid, ask = q.get("bid"), q.get("ask")
                    if bid is not None and ask is not None and ask >= bid:
                        leg["half_spread"] = max((float(ask) - float(bid)) / 2, 0.0)

        stream_age: float | None = None
        if self.alpaca.configured:
            stream_age = self._entry_staleness_s(asset_class, legs)
        shortable: bool | None = None
        if is_short_equity and self.alpaca.configured:
            shortable = await self._asset_shortable(legs[0]["symbol"])
        violations = await self.risk.validate_new_trade(
            account_equity=account["equity"],
            entry_cost_dollars=entry_cost,
            max_loss_dollars=max_loss,
            time_stop_utc=time_stop,
            expiry_date_et=expiry,
            legs=legs,
            underlying=payload["underlying"],
            stream_age_s=stream_age,
            daytrade_count=account["daytrade_count"],
            asset_class=asset_class,
            shortable=shortable,
            overnight_session=(asset_class == "equity"
                               and equity_session() == "overnight"),
            strategy_id=payload.get("strategy_id"),
        )
        if violations:
            raise ValueError("; ".join(violations))

        # Fast-tape guard: the staged entry limit was computed from a chain
        # snapshot up to several seconds old plus human decision time. In a
        # fast market that limit can be far through the CURRENT market —
        # force-refresh the legs' quotes and refuse when the staged price has
        # drifted beyond max(2x position half-spread, 8% of |mid|). The user
        # re-stages off fresh numbers instead of chasing a vanished price.
        if self.alpaca.configured:
            from app.services.fair_value import position_quote_stats

            # Quotes were force-refreshed just before validation above.
            live = position_quote_stats(
                legs, {s: self.market.latest_quote(s) for s in leg_symbols}
            )
            if live is not None:
                live_mid, half_spread = live
                ok, drift, tolerance = staged_price_ok(entry_limit, live_mid, half_spread)
                if not ok:
                    raise ValueError(
                        f"stale staged price: entry {entry_limit:.2f} is "
                        f"{drift:.2f} from the live market ({live_mid:.2f}, "
                        f"tolerance {tolerance:.2f}) - market moved, re-stage the order"
                    )

        plan = TradePlan(
            underlying=payload["underlying"].upper(),
            strategy=payload["strategy"],
            strategy_id=payload.get("strategy_id"),
            account=getattr(getattr(self.alpaca, "settings", None),
                            "alpaca_account_name", None),
            asset_class=asset_class,
            extended_hours=extended_hours,
            legs=legs,
            qty=qty,
            entry_limit=entry_limit,
            tp_premium=tp,
            sl_premium=sl,
            time_stop_utc=time_stop,
            status="planned",
            exec_quality=self._quality_snapshot(legs, "entry"),
        )
        async with self.db.session() as session:
            session.add(plan)
            await session.commit()
            await session.refresh(plan)

        try:
            order = await self._submit_entry(plan)
        except Exception as exc:
            await self.fsm.apply(plan.id, PlanEvent.ENTRY_REJECTED, notes=str(exc))
            raise ValueError(f"order rejected: {exc}")

        await self.fsm.apply(
            plan.id, PlanEvent.ENTRY_SUBMITTED, entry_order_id=str(order.id)
        )
        if self.enforcer:
            await self.enforcer.arm(plan.id)
        log.info("plan %s submitted (%s x%d, order %s)", plan.id, plan.strategy, qty, order.id)
        return (await self.get_plan(plan.id)).to_dict()

    def _entry_staleness_s(self, asset_class: str, legs: list[dict]) -> float:
        """Market-data age fed to the risk gate (>30s blocks). inf — meaning
        no data has ever arrived — must block trading, not silently pass.

        Options read the GLOBAL stream age. Equity entries read it only
        inside RTH, where a live stream vouches for its subscriptions; in
        every other session — overnight, postmarket, premarket — the stream
        being silent is a property of the feed's operating hours (IEX runs
        08:00-17:00 ET) while other tapes keep printing, so the gate
        measures the entry symbol's OWN evidence instead: the freshest of
        its cached quote vs its latest 1m bar. The overnight poller and the
        no-SIP path's external prints both land in exactly those caches. A
        symbol with no fresh per-symbol evidence outside RTH blocks, even if
        an unrelated global clock happens to be ticking."""
        if asset_class == "equity" and equity_session() != "rth":
            age = self.market.equity_tape_age_s(legs[0]["symbol"])
        else:
            age = self.market.stream_age_s
        return float("inf") if age is None else age

    async def _asset_shortable(self, symbol: str) -> bool | None:
        """Broker borrow check for a short entry: True only when the asset is
        BOTH shortable and easy-to-borrow (Alpaca rejects HTB shorts anyway;
        treating the flags as one gate keeps the refusal on our side, where
        it journals). None = lookup failed — the risk gate refuses on None
        rather than shorting on unverified borrow. Cached briefly: the flags
        change on a daily cadence, not per-order."""
        symbol = symbol.upper()
        now = time.monotonic()
        cached = self._shortable_cache.get(symbol)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            asset = await self.alpaca.call(
                self.alpaca.trading.get_asset, symbol, timeout=5.0
            )
            value = bool(getattr(asset, "shortable", False)) and bool(
                getattr(asset, "easy_to_borrow", False)
            )
        except Exception as exc:
            log.warning("shortable lookup failed for %s: %s", symbol, exc)
            return None
        self._shortable_cache[symbol] = (now, value)
        return value

    def _quality_snapshot(self, legs: list, kind: str) -> dict | None:
        """{kind: {fair, half_spread, ts}} from the live quote cache at
        SUBMIT time — the baseline every later fill is measured against.
        None when no leg has a usable quote (never blocks the order)."""
        quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in legs}
        from app.services.fair_value import position_quote_stats

        stats = position_quote_stats(legs, quotes)
        if stats is None:
            return None
        fair, half_spread = stats
        return {kind: {
            "fair": round(fair, 4),
            "half_spread": round(half_spread, 4),
            "ts": datetime.now(timezone.utc).isoformat(),
        }}

    def _quality_on_fill(self, plan: TradePlan, kind: str, fill: float | None) -> dict | None:
        """Merged exec_quality with the realized fill folded into `kind`'s
        snapshot; None when there is nothing to update (no snapshot/fill)."""
        snapshot = (plan.exec_quality or {}).get(kind)
        if snapshot is None or fill is None or "fair" not in snapshot:
            return None
        return {**(plan.exec_quality or {}), kind: quality_fill(snapshot, kind, fill)}

    async def _submit_entry(self, plan: TradePlan):
        legs = plan.legs
        client_order_id = f"{plan.id}-e"
        if plan.asset_class == "equity":
            leg = legs[0]
            if leg.get("auction") == "open":
                # Market-on-open: the fill is the auction print itself.
                # entry_limit on the plan is the sizing/journal estimate;
                # the auction sets the price. place_trade refused this past
                # the 09:28 ET cutoff, so it cannot silently queue a day.
                request = MarketOrderRequest(
                    symbol=leg["symbol"],
                    qty=plan.qty * leg.get("ratio", 1),
                    side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                    time_in_force=TimeInForce.OPG,
                    client_order_id=client_order_id,
                )
                return await self._submit_idempotent(request, client_order_id)
            # Shares: plain DAY limit; extended_hours puts it in the 24/5
            # book. NO position_intent — an options concept; equity orders
            # are accepted without it (verified 2026-08-04).
            request = LimitOrderRequest(
                symbol=leg["symbol"],
                qty=plan.qty * leg.get("ratio", 1),
                side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                limit_price=abs(round_tick(plan.entry_limit)),
                time_in_force=TimeInForce.DAY,
                extended_hours=plan.extended_hours,
                client_order_id=client_order_id,
            )
            return await self._submit_idempotent(request, client_order_id)
        if len(legs) == 1:
            # Single-leg limit prices are unsigned premiums; side carries
            # direction (a short entry's net credit arrives as negative
            # entry_limit in position terms).
            leg = legs[0]
            request = LimitOrderRequest(
                symbol=leg["symbol"],
                qty=plan.qty * leg.get("ratio", 1),
                side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                position_intent=(
                    PositionIntent.BUY_TO_OPEN if leg["side"] > 0 else PositionIntent.SELL_TO_OPEN
                ),
                limit_price=abs(round_tick(plan.entry_limit)),
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        else:
            # MLEG limit prices are signed: positive = net debit, negative =
            # net credit. entry_limit already uses that convention.
            request = LimitOrderRequest(
                order_class=OrderClass.MLEG,
                qty=plan.qty,
                limit_price=round_tick(plan.entry_limit),
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
                legs=[
                    OptionLegRequest(
                        symbol=leg["symbol"],
                        ratio_qty=leg.get("ratio", 1),
                        side=OrderSide.BUY if leg["side"] > 0 else OrderSide.SELL,
                        position_intent=(
                            PositionIntent.BUY_TO_OPEN
                            if leg["side"] > 0
                            else PositionIntent.SELL_TO_OPEN
                        ),
                    )
                    for leg in legs
                ],
            )
        return await self._submit_idempotent(request, client_order_id)

    async def _submit_idempotent(self, request, client_order_id: str):
        """Submit an order; on ANY failure, check whether the broker actually
        accepted it (a timed-out or dropped-connection submit may still have
        landed). The deterministic client_order_id makes the check exact and
        makes accidental double-submits impossible — the broker rejects a
        reused id, and recovery then returns the original order.
        """
        try:
            return await self.alpaca.call(self.alpaca.trading.submit_order, request)
        except Exception as submit_exc:
            recovered = await self._order_by_client_id(client_order_id)
            if recovered is not None:
                status = str(getattr(recovered, "status", "")).lower()
                if not any(s in status for s in ("cancel", "expired", "rejected")):
                    log.warning(
                        "submit ambiguous for %s (%s) - recovered live order %s",
                        client_order_id, submit_exc, recovered.id,
                    )
                    return recovered
            raise

    async def _order_by_client_id(self, client_order_id: str):
        try:
            return await self.alpaca.call(
                self.alpaca.trading.get_order_by_client_id, client_order_id, retries=2
            )
        except Exception:
            return None

    # ------------------------------------------------------------- updates

    async def get_plan(self, plan_id: str) -> TradePlan:
        async with self.db.session() as session:
            plan = await session.get(TradePlan, plan_id)
            if plan is None:
                raise ValueError(f"no plan {plan_id}")
            return plan

    async def _update_plan(self, plan_id: str, **fields) -> TradePlan:
        """Field-only update (exit tightening, notes). State changes must go
        through self.fsm.apply — this method never touches `status`."""
        assert "status" not in fields, "state changes go through the FSM"
        return await self.fsm.update_fields(plan_id, **fields)

    def _fill_value(self, plan: TradePlan, avg: float | None, *, is_entry: bool) -> float | None:
        """Normalize a broker fill price into position-value terms (signed,
        same axis as entry_limit/TP/SL).

        Single-leg: broker prices are unsigned premiums; leg side supplies the
        sign for entry AND exit (a short's buy-to-close debit is negative
        position value). MLEG: prices are signed net debit/credit in the
        ORDER's orientation — entries share the position orientation, exits
        are reversed so the sign flips.
        """
        if avg is None:
            return None
        if len(plan.legs) == 1:
            return plan.legs[0]["side"] * abs(avg)
        if is_entry:
            # Defensive: a credit entry cannot fill at a debit; force the
            # entry_limit's sign if the feed reports magnitude only.
            import math as _math

            return _math.copysign(abs(avg), plan.entry_limit) if avg > 0 else avg
        return -avg

    async def on_trade_update(self, update) -> None:
        """TradingStream handler: maps broker order events onto FSM events.

        All lifecycle legality lives in the FSM's transition table — this
        method only translates. Duplicate or out-of-order broker events land
        on absent (state, event) pairs and are ignored by construction.
        """
        try:
            order = update.order
            order_id = str(order.id)
            event = str(update.event)
            async with self.db.session() as session:
                from sqlalchemy import or_, select

                result = await session.execute(
                    select(TradePlan).where(
                        or_(
                            TradePlan.entry_order_id == order_id,
                            TradePlan.exit_order_id == order_id,
                            TradePlan.tp_order_id == order_id,
                        )
                    )
                )
                plan = result.scalars().first()
            if plan is None:
                return
            # Broker-resting TP events: a fill closes the plan; a dead order
            # clears the field so the monitor re-rests it.
            if plan.tp_order_id == order_id and plan.entry_order_id != order_id:
                if event == "fill":
                    # Serialize against the exit ladder: an absorb interleaving
                    # a concurrently-triggered ladder would lose the fill (the
                    # ladder overwrites exit_order_id and the guard drops it).
                    if self.enforcer:
                        await self.enforcer.absorb_tp_locked(plan.id, order_id)
                    else:
                        await self._absorb_tp_fill(plan.id, order_id)
                elif event in ("canceled", "expired", "rejected"):
                    await self.fsm.update_fields(
                        plan.id, expect={"tp_order_id": order_id}, tp_order_id=None
                    )
                return
            is_entry = plan.entry_order_id == order_id
            raw_avg = float(order.filled_avg_price) if order.filled_avg_price else None
            avg = self._fill_value(plan, raw_avg, is_entry=is_entry)
            filled_qty = int(float(order.filled_qty or 0))
            log.info(
                "trade update: plan %s %s event=%s avg=%s filled=%d",
                plan.id, "entry" if is_entry else "exit", event, avg, filled_qty,
            )

            fsm_event: PlanEvent | None = None
            fields: dict = {}
            # Exit-order events are ABOUT a specific order; guard so a stale
            # event for an order the escalation ladder already replaced can't
            # clobber the live one (entry_order_id never changes — no guard).
            guard = None if is_entry else {"exit_order_id": order_id}
            if event == "fill" and is_entry:
                fsm_event = PlanEvent.ENTRY_FILLED
                fields = {"fill_premium": avg, "filled_qty": filled_qty or plan.qty}
                if (eq := self._quality_on_fill(plan, "entry", avg)) is not None:
                    fields["exec_quality"] = eq
            elif event == "fill":
                realized = plan.pnl_at(avg)
                fsm_event = PlanEvent.EXIT_FILLED
                fields = {
                    "exit_premium": avg,
                    "realized_pnl": realized,
                    "exited_at": as_utc(getattr(order, "filled_at", None)) or utcnow(),
                }
                if (eq := self._quality_on_fill(plan, "exit", avg)) is not None:
                    fields["exec_quality"] = eq
            elif event == "partial_fill" and is_entry:
                fsm_event = PlanEvent.ENTRY_PARTIAL_FILL
                fields = {"fill_premium": avg, "filled_qty": filled_qty}
                if (eq := self._quality_on_fill(plan, "entry", avg)) is not None:
                    fields["exec_quality"] = eq
            elif event in ("canceled", "expired", "rejected"):
                if is_entry and filled_qty > 0:
                    # Entry died with a partial position: shrink to what
                    # filled and manage it as a filled plan.
                    fsm_event = PlanEvent.ENTRY_CANCELLED_PARTIAL
                    fields = {
                        "qty": filled_qty,
                        "filled_qty": filled_qty,
                        "fill_premium": avg if avg is not None else plan.fill_premium,
                        "notes": f"entry {event} after partial fill {filled_qty}",
                    }
                elif is_entry:
                    fsm_event = (
                        PlanEvent.ENTRY_REJECTED if event == "rejected" else PlanEvent.ENTRY_CANCELLED
                    )
                    fields = {"notes": f"entry {event}"}
                else:
                    fsm_event = PlanEvent.EXIT_ORDER_DEAD
                    fields = {"exit_order_id": None}
            if fsm_event is None:
                return  # informational event (new/accepted/replaced)

            result = await self.fsm.apply(plan.id, fsm_event, guard=guard, **fields)
            if not result.applied:
                return
            # Effects, driven by the state actually ENTERED:
            if result.target in (PlanState.FILLED, PlanState.PARTIALLY_FILLED) and self.enforcer:
                await self.enforcer.arm(plan.id)
            elif result.target in TERMINAL_STATES and self.enforcer:
                self.enforcer.disarm(plan.id)
        except Exception:
            log.exception("trade update handling failed")

    # -------------------------------------------------------------- exits

    def _close_request(self, plan: TradePlan, limit_price: float | None, client_order_id: str):
        """Closing order (reverse all legs) at a POSITION-VALUE limit (signed,
        same axis as entry/TP/SL); None => market. The closing order has every
        leg reversed, so MLEG submits the NEGATION of the position-terms
        limit; single-leg orders take the unsigned premium."""
        legs = plan.legs
        if plan.asset_class == "equity":
            # Reverse the single share leg. extended_hours propagates so a
            # closing limit works the 24/5 book too (a resting equity TP
            # also fills after hours — verified). Market orders here are
            # RTH-only BY POLICY: outside RTH the broker silently queues
            # them for the next open (verified 2026-08-04) — the enforcer's
            # session policy substitutes an aggressive limit instead.
            leg = legs[0]
            common = dict(
                symbol=leg["symbol"],
                qty=plan.effective_qty * leg.get("ratio", 1),
                side=OrderSide.SELL if leg["side"] > 0 else OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
            return (
                MarketOrderRequest(**common)
                if limit_price is None
                else LimitOrderRequest(**common, extended_hours=plan.extended_hours,
                                       limit_price=abs(round_tick(limit_price)))
            )
        if len(legs) == 1:
            leg = legs[0]
            common = dict(
                symbol=leg["symbol"],
                qty=plan.effective_qty * leg.get("ratio", 1),
                side=OrderSide.SELL if leg["side"] > 0 else OrderSide.BUY,
                position_intent=(
                    PositionIntent.SELL_TO_CLOSE if leg["side"] > 0 else PositionIntent.BUY_TO_CLOSE
                ),
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
            return (
                MarketOrderRequest(**common)
                if limit_price is None
                else LimitOrderRequest(**common, limit_price=abs(round_tick(limit_price)))
            )
        mleg_legs = [
            OptionLegRequest(
                symbol=leg["symbol"],
                ratio_qty=leg.get("ratio", 1),
                side=OrderSide.SELL if leg["side"] > 0 else OrderSide.BUY,
                position_intent=(
                    PositionIntent.SELL_TO_CLOSE
                    if leg["side"] > 0
                    else PositionIntent.BUY_TO_CLOSE
                ),
            )
            for leg in legs
        ]
        common = dict(
            order_class=OrderClass.MLEG,
            qty=plan.effective_qty,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            legs=mleg_legs,
        )
        return (
            MarketOrderRequest(**common)
            if limit_price is None
            else LimitOrderRequest(**common, limit_price=round_tick(-limit_price))
        )

    async def submit_exit(self, plan: TradePlan, reason: str, limit_price: float | None,
                          attempt_key: str = "0") -> None:
        """Submit closing order. attempt_key names the escalation rung
        (r0/r1/mkt/v3...) so each rung's submit is idempotent: an ambiguous
        failure recovers the SAME order rather than stacking a second close.
        """
        if not self.alpaca.configured:
            # Keyless: there is no broker to route to. Simulate the fill at
            # the current mark so paper plans still complete their lifecycle
            # — the alternative is every submit failing until the verify loop
            # mistakes the (unreachable) empty position list for a vanished
            # position and FORCE_CLOSEs the plan with no P&L recorded.
            await self._simulate_exit_fill(plan, reason, limit_price)
            return
        client_order_id = f"{plan.id}-x{attempt_key}"
        request = self._close_request(plan, limit_price, client_order_id)
        # Snapshot the book NOW (before the submit): each escalation rung
        # re-snapshots, so the filling order is measured against the market
        # it was actually submitted into.
        snap = self._quality_snapshot(plan.legs, "exit")
        order = await self._submit_idempotent(request, client_order_id)
        fields: dict = {}
        if snap is not None:
            fields["exec_quality"] = {**(plan.exec_quality or {}), **snap}
        await self.fsm.apply(
            plan.id,
            PlanEvent.EXIT_SUBMITTED,
            exit_order_id=str(order.id),
            exit_reason=reason,
            **fields,
        )

    async def _simulate_exit_fill(self, plan: TradePlan, reason: str,
                                  limit_price: float | None) -> None:
        """Keyless close: fill at the live position mid (fallback: the
        ladder's limit, then the triggered level) through the normal FSM
        path, so journal/history/slippage all read like a real exit."""
        quotes = {leg["symbol"]: self.market.latest_quote(leg["symbol"]) for leg in plan.legs}
        mid = position_mid_from_quotes(plan.legs, quotes)
        if mid is None:
            mid = limit_price
        if mid is None:
            mid = plan.sl_premium if reason == "sl" else plan.tp_premium
        order_id = f"sim-{uuid4().hex[:10]}"
        await self.fsm.apply(
            plan.id, PlanEvent.EXIT_SUBMITTED,
            exit_order_id=order_id, exit_reason=reason, tp_order_id=None,
        )
        realized = plan.pnl_at(mid)
        result = await self.fsm.apply(
            plan.id, PlanEvent.EXIT_FILLED,
            guard={"exit_order_id": order_id},
            exit_premium=round(mid, 4), realized_pnl=realized,
        )
        if result.applied:
            log.info("keyless simulated %s exit for plan %s @ %.2f", reason, plan.id, mid)
            if self.enforcer:
                self.enforcer.disarm(plan.id)

    # ------------------------------------------------- broker-resting TP

    async def submit_resting_tp(self, plan: TradePlan) -> None:
        """Rest the take-profit AT THE BROKER as a live limit close: zero
        software latency and it fills even if this engine is down. The key
        is deterministic per (plan, TP level) so a crash between broker
        accept and the DB write can never double-place it; tighten-reuse of
        a burned level walks to an r2/r3 suffix."""
        cents = int(round(abs(plan.tp_premium) * 100))
        last_exc: Exception | None = None
        for suffix in ("", "r2", "r3", "r4"):
            key = f"{plan.id}-xtp{cents}{suffix}"
            request = self._close_request(plan, plan.tp_premium, key)
            try:
                order = await self._submit_idempotent(request, key)
                await self.fsm.update_fields(plan.id, tp_order_id=str(order.id))
                log.info("resting TP placed for plan %s @ %.2f (order %s)",
                         plan.id, plan.tp_premium, order.id)
                return
            except Exception as exc:
                last_exc = exc
                if "unique" not in str(exc).lower():
                    raise
        raise last_exc  # every suffix burned (should not happen in practice)

    async def cancel_resting_tp(self, plan: TradePlan, confirm_s: float = 5.0) -> bool:
        """Take down the broker-resting TP before any other close goes up.
        Returns True when the TP turned out to be FILLED (position already
        closed — the caller must NOT submit another close on top).

        Broker cancels are ASYNCHRONOUS: a clean cancel call means
        pending_cancel, not dead — the order can still fill in flight. So a
        returned False is only ever based on an observed TERMINAL status;
        anything less raises, because proceeding blind is the double-close.
        """
        if not plan.tp_order_id:
            return False
        order_id = plan.tp_order_id
        try:
            await self.cancel_order(order_id)
        except Exception as exc:
            # Cancel can lose the race to a fill (or just fail) — the status
            # poll below is the arbiter either way.
            log.warning("resting TP cancel errored for %s: %s", plan.id, exc)
        deadline = time.monotonic() + confirm_s
        while True:
            try:
                status = await self.order_status(order_id)
            except Exception as exc:
                log.warning("resting TP status poll failed for %s: %s", plan.id, exc)
                status = ""
            if status == "filled":
                await self._absorb_tp_fill(plan.id, order_id)
                return True
            if status in ("canceled", "expired", "rejected"):
                await self.fsm.update_fields(
                    plan.id, expect={"tp_order_id": order_id}, tp_order_id=None
                )
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"resting TP cancel unconfirmed for plan {plan.id} "
                    f"(order {order_id} status {status or 'unknown'})"
                )
            await asyncio.sleep(0.25)

    async def _absorb_tp_fill(self, plan_id: str, order_id: str) -> None:
        """A resting TP filled at the broker: close the plan from REST truth
        (works whether we learn it from the stream, a cancel race, or
        reconcile)."""
        order = await self.alpaca.call(
            self.alpaca.trading.get_order_by_id, order_id, retries=1
        )
        plan = await self.get_plan(plan_id)
        raw = float(order.filled_avg_price or 0) or None
        avg = self._fill_value(plan, raw, is_entry=False)
        await self.fsm.apply(
            plan_id, PlanEvent.EXIT_SUBMITTED,
            exit_order_id=order_id, exit_reason="tp", tp_order_id=None,
        )
        realized = plan.pnl_at(avg)
        result = await self.fsm.apply(
            plan_id, PlanEvent.EXIT_FILLED,
            guard={"exit_order_id": order_id},
            exit_premium=avg, realized_pnl=realized,
            exited_at=as_utc(getattr(order, "filled_at", None)) or utcnow(),
        )
        if result.applied and self.enforcer:
            self.enforcer.disarm(plan_id)

    async def cancel_order(self, order_id: str) -> None:
        # Cancels are idempotent broker-side; retry transient failures.
        await self.alpaca.call(self.alpaca.trading.cancel_order_by_id, order_id, retries=2)

    async def cancel_entry(self, plan: TradePlan) -> None:
        if plan.entry_order_id and plan.status in ("submitted", "partially_filled"):
            try:
                await self.cancel_order(plan.entry_order_id)
            except Exception as exc:
                log.warning("cancel entry %s failed: %s", plan.entry_order_id, exc)

    async def order_status(self, order_id: str) -> str:
        order = await self.alpaca.call(
            self.alpaca.trading.get_order_by_id, order_id, retries=2
        )
        return str(order.status.value if hasattr(order.status, "value") else order.status)
