"""ACCOUNT CAPABILITIES: what this account can actually do, proven at the
broker and persisted per account, instead of three toggles the human
guesses at.

Two layers, one blob (AppSetting key ``capabilities:{account}``):

- Layer A ("broker flags") is free and read-only: ``get_account`` +
  ``get_account_configurations`` give the approved/effective options level,
  shorting, multiplier, fractional, PDT state. Refreshed at boot and on
  demand.
- Layer B ("the probe") is a human-clicked sequence of the smallest real
  orders that can prove or refute each capability - a 1-share round trip,
  a non-marketable short attempt, a far-OTM long option, a two-leg spread,
  a short put - each recorded with the broker's VERBATIM answer, then
  cancelled/flattened. It runs on the live account too (that is where IRA
  rules bite), behind a typed confirm, never on its own, and never through
  place_trade (which would write plans and refuse non-marketable limits).

The result is a CEILING, not a suggestion: RiskService.get_settings() caps
``options_level`` at the verified level and forces ``equity_long_only``
when shorting was refused, so enforcement stays in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.trade import AppSetting
from app.services.market_clock import equity_session

log = logging.getLogger("app.capabilities")

CAPS_KEY = "capabilities:{account}"
PROBE_DEADLINE_S = 90.0
CLEANUP_BUDGET_S = 30.0
FILL_WAIT_S = 20.0
FLATTEN_WAIT_S = 10.0          # per rung of the cleanup flatten ladder
PROBE_PREFIX = "probe-"
EQUITY_CANDIDATES = ("SPLG", "SCHX", "XLF", "SCHD", "F", "SOFI")
SHORT_CANDIDATES = ("XLF", "SPLG", "F", "SOFI")
OPTION_UNDERLYING = "SPY"
CHEAP_OPTION_UNDERLYINGS = ("F", "SOFI", "NIO")
MAX_PROBE_PRICE = 40.0
ET = ZoneInfo("America/New_York")

CHECKS = (
    "broker_flags", "precondition", "equity_buy_sell", "equity_extended_hours",
    "equity_short", "equity_opg", "equity_cls", "option_l2_long", "option_l3_spread",
    "option_short_put", "option_naked_call", "option_l1_covered", "fractional", "cleanup",
)


# ------------------------------------------------------------ pure layer


class ProbeRunning(RuntimeError):
    pass


def broker_message(exc: BaseException) -> str:
    """The broker's verbatim text. alpaca-py's APIError carries the JSON
    body; anything else falls back to str()."""
    for attr in ("message", "error"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    return str(exc) or exc.__class__.__name__


def opg_window_open(now_et: datetime) -> bool:
    """Alpaca rejects OPG orders submitted 09:28-19:00 ET."""
    hm = now_et.hour * 60 + now_et.minute
    return not (9 * 60 + 28 <= hm < 19 * 60)


def cls_window_open(now_et: datetime) -> bool:
    """Alpaca rejects CLS orders submitted 15:50-19:00 ET."""
    hm = now_et.hour * 60 + now_et.minute
    return not (15 * 60 + 50 <= hm < 19 * 60)


def pick_equity_symbol(candidates, prices: dict[str, float], assets: dict[str, dict],
                       held: set[str], plan_symbols: set[str], open_order_symbols: set[str],
                       *, max_price: float = MAX_PROBE_PRICE, need_short: bool = False) -> str | None:
    """First candidate that is tradable, cheap, not held, not in any open
    plan's legs and without a working order - so the exit enforcer's
    external-capture never sees a probe order (it filters on plan legs)."""
    for sym in candidates:
        a = assets.get(sym) or {}
        price = prices.get(sym)
        if not a.get("tradable") or not price or price > max_price:
            continue
        if sym in held or sym in plan_symbols or sym in open_order_symbols:
            continue
        if need_short and not (a.get("shortable") and a.get("easy_to_borrow")):
            continue
        return sym
    return None


def pick_far_otm(contracts: list[dict], spot: float, quotes: dict[str, dict], *,
                 right: str = "P", min_bid: float = 0.10, otm_pct: float = 0.10) -> dict | None:
    """Cheapest far-OTM contract with a real bid: strike <= spot*(1-otm)
    for puts / >= spot*(1+otm) for calls."""
    best = None
    for c in contracts:
        if str(c.get("type", "")).lower()[:1] != right.lower()[:1]:
            continue
        strike = float(c["strike"])
        if right.upper() == "P" and strike > spot * (1 - otm_pct):
            continue
        if right.upper() == "C" and strike < spot * (1 + otm_pct):
            continue
        q = quotes.get(c["symbol"]) or {}
        bid = float(q.get("bid") or 0)
        if bid < min_bid:
            continue
        # Cheapest bid wins; on a tie, the strike nearest the money (a
        # put's highest / a call's lowest) - it is the more liquid one and
        # leaves room below it for the vertical's short leg.
        nearer = best is not None and bid == best["bid"] and (
            strike > best["strike"] if right.upper() == "P" else strike < best["strike"])
        if best is None or bid < best["bid"] or nearer:
            best = {**c, "bid": bid, "ask": float(q.get("ask") or bid)}
    return best


def pick_vertical(contracts: list[dict], long_leg: dict, quotes: dict[str, dict]) -> dict | None:
    """The next strike below a long put (a debit vertical): same expiry, put."""
    same = [c for c in contracts
            if str(c.get("type", "")).lower().startswith("p")
            and c.get("expiry") == long_leg.get("expiry")
            and float(c["strike"]) < float(long_leg["strike"])]
    if not same:
        return None
    short = max(same, key=lambda c: float(c["strike"]))
    q = quotes.get(short["symbol"]) or {}
    return {**short, "bid": float(q.get("bid") or 0), "ask": float(q.get("ask") or 0)}


def _status(checks: list[dict], name: str) -> str | None:
    for row in checks:
        if row.get("name") == name:
            return row.get("status")
    return None


def derive(broker: dict, checks: list[dict], mode: str) -> tuple[dict, dict]:
    """(derived, sources). Precedence per capability: a probe PASS/FAIL
    beats the broker's flags, which beat the mode default. SKIP/INFO rows
    never override anything."""
    derived: dict[str, Any] = {}
    sources: dict[str, str] = {}
    live = mode == "live_manual"

    # options level
    l2, l3 = _status(checks, "option_l2_long"), _status(checks, "option_l3_spread")
    if l3 == "PASS":
        derived["options_level"], sources["options_level"] = 3, "probe"
    elif l2 == "PASS":
        derived["options_level"], sources["options_level"] = 2, "probe"
    elif l2 == "FAIL":
        derived["options_level"], sources["options_level"] = 0, "probe"
    elif broker.get("options_trading_level") is not None:
        derived["options_level"] = int(broker["options_trading_level"])
        sources["options_level"] = "broker"
    elif broker.get("options_approved_level") is not None:
        derived["options_level"] = int(broker["options_approved_level"])
        sources["options_level"] = "broker"
    else:
        derived["options_level"], sources["options_level"] = (2 if live else 3), "default"

    # equity shorts
    sh = _status(checks, "equity_short")
    cfg = broker.get("config") or {}
    if sh in ("PASS", "FAIL"):
        derived["equity_shorts"], sources["equity_shorts"] = sh == "PASS", "probe"
    elif broker.get("shorting_enabled") is not None or cfg.get("no_shorting") is not None:
        derived["equity_shorts"] = bool(broker.get("shorting_enabled")) and not bool(cfg.get("no_shorting"))
        sources["equity_shorts"] = "broker"
    else:
        derived["equity_shorts"], sources["equity_shorts"] = (False if live else None), "default"

    # probe-only booleans
    for name, key in (("equity_buy_sell", "equity_long"), ("equity_extended_hours", "extended_hours"),
                      ("equity_opg", "opg"), ("equity_cls", "cls"), ("option_short_put", "short_puts"),
                      ("option_naked_call", "naked_calls"), ("option_l1_covered", "covered_calls")):
        st = _status(checks, name)
        if st in ("PASS", "FAIL"):
            derived[key], sources[key] = st == "PASS", "probe"
        else:
            derived[key], sources[key] = None, "unknown"

    # broker-only facts
    if cfg.get("fractional_trading") is not None:
        derived["fractional"], sources["fractional"] = bool(cfg["fractional_trading"]), "broker"
    else:
        derived["fractional"], sources["fractional"] = None, "unknown"
    mult = broker.get("multiplier")
    derived["cash_account"] = (float(mult) <= 1.0) if mult is not None else None
    sources["cash_account"] = "broker" if mult is not None else "unknown"
    return derived, sources


def ceiling_from(derived: dict, sources: dict, mode: str) -> dict | None:
    """What RiskService may not exceed. None when nothing is known on paper;
    the live server always has at least its unprobed floor of 2."""
    out: dict[str, Any] = {}
    if sources.get("options_level") in ("probe", "broker"):
        out["options_level"] = int(derived["options_level"])
    elif mode == "live_manual":
        out["options_level"] = 2
    if sources.get("equity_shorts") in ("probe", "broker"):
        out["equity_shorts"] = bool(derived["equity_shorts"])
    elif mode == "live_manual":
        out["equity_shorts"] = False
    return out or None


# ------------------------------------------------------------ service


@dataclass
class _Ctx:
    """Everything one probe run needs, gathered in `precondition`."""
    session: str | None = None
    rth: bool = False
    weekend: bool = False
    now_et: datetime = field(default_factory=lambda: datetime.now(ET))
    held: set[str] = field(default_factory=set)
    plan_symbols: set[str] = field(default_factory=set)
    open_order_symbols: set[str] = field(default_factory=set)
    equity_symbol: str | None = None
    equity_price: float = 0.0
    short_symbol: str | None = None
    short_price: float = 0.0
    daytrade_count: int = 0
    margin: bool = True
    equity: float = 0.0
    settled_bp: float = 0.0
    cash: float = 0.0
    fractional: bool | None = None


class CapabilitiesService:
    def __init__(self, db, alpaca, trade, clock, settings):
        self.db = db
        self.alpaca = alpaca
        self.trade = trade
        self.clock = clock
        self.settings = settings
        self._state: dict = self._empty()
        self._task: asyncio.Task | None = None
        self._orders: dict[str, str] = {}       # order_id -> check
        self._symbols: set[str] = set()
        self._ctx = _Ctx()

    # ---------------------------------------------------------- persistence

    @property
    def mode(self) -> str:
        return getattr(self.settings, "trading_mode", "paper")

    @property
    def account(self) -> str:
        return getattr(self.settings, "alpaca_account_name", "default") or "default"

    @property
    def key(self) -> str:
        return CAPS_KEY.format(account=self.account)

    @staticmethod
    def _empty() -> dict:
        return {"probed_at": None, "probe_session": None, "broker": {}, "checks": [],
                "derived": {}, "sources": {}, "manual_action": None, "applied_to_risk_at": None}

    async def load(self) -> None:
        try:
            async with self.db.session() as session:
                row = await session.get(AppSetting, self.key)
            if row and row.value:
                self._state = {**self._empty(), **dict(row.value)}
        except Exception:  # noqa: BLE001
            log.exception("capabilities load failed - starting empty")
        self._rederive()

    async def save(self) -> None:
        async with self.db.session() as session:
            row = await session.get(AppSetting, self.key)
            if row is None:
                session.add(AppSetting(key=self.key, value=dict(self._state)))
            else:
                row.value = dict(self._state)
            await session.commit()

    def _rederive(self) -> None:
        derived, sources = derive(self._state.get("broker") or {}, self._state.get("checks") or [], self.mode)
        self._state["derived"], self._state["sources"] = derived, sources

    # ----------------------------------------------------------- layer A

    async def refresh_broker(self) -> dict:
        """Read-only broker facts. Never raises: a failed read is recorded."""
        broker: dict = {"fetched_at": datetime.now(timezone.utc).isoformat(), "error": None}
        try:
            acct = await self.alpaca.call(self.alpaca.trading.get_account, retries=1)
            for name in ("status", "trading_blocked", "options_approved_level", "options_trading_level",
                         "shorting_enabled", "pattern_day_trader", "daytrade_count"):
                v = getattr(acct, name, None)
                broker[name] = v.value if hasattr(v, "value") else v
            for name in ("multiplier", "equity", "cash", "non_marginable_buying_power", "buying_power"):
                v = getattr(acct, name, None)
                broker[name] = float(v) if v is not None else None
            try:
                cfg = await self.alpaca.call(self.alpaca.trading.get_account_configurations, retries=1)
                broker["config"] = {
                    "no_shorting": getattr(cfg, "no_shorting", None),
                    "fractional_trading": getattr(cfg, "fractional_trading", None),
                    "max_margin_multiplier": getattr(cfg, "max_margin_multiplier", None),
                    "max_options_trading_level": getattr(cfg, "max_options_trading_level", None),
                    "ptp_no_exception_entry": getattr(cfg, "ptp_no_exception_entry", None),
                }
            except Exception as exc:  # noqa: BLE001
                broker["config"] = {}
                broker["error"] = f"configurations: {broker_message(exc)}"
        except Exception as exc:  # noqa: BLE001
            broker["error"] = broker_message(exc)
            log.warning("capabilities: broker flags unavailable: %s", broker["error"])
        self._state["broker"] = broker
        self._rederive()
        try:
            await self.save()
        except Exception:  # noqa: BLE001
            log.exception("capabilities save failed")
        return broker

    # ----------------------------------------------------------- reads

    def ceiling(self) -> dict | None:
        return ceiling_from(self._state.get("derived") or {}, self._state.get("sources") or {}, self.mode)

    def options_ceiling(self) -> int:
        ceil = self.ceiling() or {}
        return int(ceil.get("options_level", 3 if self.mode == "paper" else 2))

    def level_provenance(self) -> str:
        src = (self._state.get("sources") or {}).get("options_level", "default")
        if src == "probe":
            return f"verified by probe {str(self._state.get('probed_at') or '')[:16]}"
        if src == "broker":
            return "broker reports it (unprobed)"
        return "unprobed default"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def summary(self) -> dict:
        return {
            "probed_at": self._state.get("probed_at"),
            "running": self.running,
            "derived": self._state.get("derived") or {},
            "sources": self._state.get("sources") or {},
            "manual_action": self._state.get("manual_action"),
            "level_provenance": self.level_provenance(),
        }

    def status(self, stored_risk: dict | None = None) -> dict:
        checks = self._state.get("checks") or []
        derived = self._state.get("derived") or {}
        current = next((c["name"] for c in checks if c.get("status") == "RUNNING"), None)
        out = {
            "account": self.account,
            "mode": self.mode,
            "running": self.running,
            "started_at": self._state.get("started_at") if self.running else None,
            "progress": {"done": sum(1 for c in checks if c.get("status") != "RUNNING"),
                         "total": len(CHECKS), "current": current},
            "probed_at": self._state.get("probed_at"),
            "probe_session": self._state.get("probe_session"),
            "broker": self._state.get("broker") or {},
            "checks": checks,
            "derived": derived,
            "sources": self._state.get("sources") or {},
            "manual_action": self._state.get("manual_action"),
            "applied_to_risk_at": self._state.get("applied_to_risk_at"),
            "level_provenance": self.level_provenance(),
            "ceiling": self.ceiling(),
        }
        if stored_risk is not None:
            stored_level = int(stored_risk.get("options_level", 3))
            stored_long_only = bool(stored_risk.get("equity_long_only", True))
            out["stored_risk"] = {"options_level": stored_level, "equity_long_only": stored_long_only}
            verified_level = derived.get("options_level")
            verified_shorts = derived.get("equity_shorts")
            out["apply_pending"] = bool(
                (verified_level is not None and stored_level < int(verified_level))
                or (verified_shorts is True and stored_long_only)
            )
        return out

    async def apply_to_risk(self, risk) -> dict:
        """Widen the STORED risk settings up to what was verified. The
        ceiling already narrows; this is the only direction APPLY moves."""
        derived = self._state.get("derived") or {}
        patch: dict = {}
        if derived.get("options_level") is not None:
            patch["options_level"] = int(derived["options_level"])
        if derived.get("equity_shorts") is not None:
            patch["equity_long_only"] = not bool(derived["equity_shorts"])
        if patch:
            await risk.update_settings(patch)
        self._state["applied_to_risk_at"] = datetime.now(timezone.utc).isoformat()
        await self.save()
        return patch

    # ----------------------------------------------------------- the probe

    async def start_probe(self, *, confirm: str | None = None, only: list[str] | None = None) -> None:
        if self.running:
            raise ProbeRunning("a probe is already running")
        if not getattr(self.alpaca, "configured", False):
            raise RuntimeError("broker not configured")
        if self.mode == "live_manual" and confirm != "LIVE":
            raise ValueError('the live probe places real orders - confirm with {"confirm": "LIVE"}')
        bad = [c for c in (only or []) if c not in CHECKS]
        if bad:
            raise ValueError(f"unknown checks: {bad}")
        self._task = asyncio.create_task(self._probe_task(only), name="capabilities-probe")

    async def abort(self) -> bool:
        if not self.running:
            return False
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return True

    def _row(self, name: str, status: str, detail: str, **extra) -> None:
        checks = self._state.setdefault("checks", [])
        for existing in checks:
            if existing.get("name") == name and existing.get("status") == "RUNNING":
                existing.update({"status": status, "detail": detail[:600],
                                 "at": datetime.now(timezone.utc).isoformat(), **extra})
                break
        else:
            checks.append({"name": name, "status": status, "detail": detail[:600],
                           "at": datetime.now(timezone.utc).isoformat(), **extra})
        (log.error if status == "FAIL" else log.info)("probe %-22s %-4s %s", name, status, detail[:200])

    def _begin(self, name: str) -> None:
        self._state.setdefault("checks", []).append(
            {"name": name, "status": "RUNNING", "detail": "", "at": datetime.now(timezone.utc).isoformat()})

    async def _probe_task(self, only: list[str] | None) -> None:
        self._orders.clear()
        self._symbols.clear()
        self._ctx = _Ctx()
        self._state.update({"checks": [], "manual_action": None,
                            "started_at": datetime.now(timezone.utc).isoformat()})
        wanted = set(only) if only else set(CHECKS)
        try:
            await asyncio.wait_for(self._run_checks(wanted), PROBE_DEADLINE_S)
        except asyncio.TimeoutError:
            self._row("probe", "FAIL", f"deadline {PROBE_DEADLINE_S:.0f}s exceeded - cleanup running")
        except asyncio.CancelledError:
            self._row("probe", "INFO", "aborted - cleanup running")
        except Exception as exc:  # noqa: BLE001
            self._row("probe", "FAIL", f"unexpected: {exc!r}")
        finally:
            try:
                await asyncio.wait_for(asyncio.shield(self._cleanup()), CLEANUP_BUDGET_S)
            except Exception as exc:  # noqa: BLE001
                self._state["manual_action"] = f"FLATTEN MANUALLY - cleanup failed: {broker_message(exc)}"
                self._row("cleanup", "FAIL", self._state["manual_action"])
            self._state["probed_at"] = datetime.now(timezone.utc).isoformat()
            self._state["probe_session"] = self._ctx.session
            self._rederive()
            try:
                await self.save()
            except Exception:  # noqa: BLE001
                log.exception("capabilities save failed after probe")

    async def _run_checks(self, wanted: set[str]) -> None:
        steps = (
            ("broker_flags", self._check_broker_flags),
            ("precondition", self._check_precondition),
            ("equity_buy_sell", self._check_equity_buy_sell),
            ("equity_extended_hours", self._check_equity_extended_hours),
            ("equity_short", self._check_equity_short),
            ("equity_opg", self._check_equity_opg),
            ("equity_cls", self._check_equity_cls),
            ("option_l2_long", self._check_option_l2_long),
            ("option_l3_spread", self._check_option_l3_spread),
            ("option_short_put", self._check_option_short_put),
            ("option_naked_call", self._check_option_naked_call),
            ("option_l1_covered", self._check_option_l1_covered),
            ("fractional", self._check_fractional),
        )
        for name, fn in steps:
            if name not in wanted and name not in ("broker_flags", "precondition"):
                continue
            self._begin(name)
            try:
                await fn()
            except asyncio.CancelledError:
                self._row(name, "INFO", "aborted")
                raise
            except Exception as exc:  # noqa: BLE001
                self._row(name, "FAIL", f"unexpected: {broker_message(exc)}")
            if self._state.get("trading_blocked_abort"):
                break

    # ---- broker helpers ------------------------------------------------

    async def _call(self, fn, *args, **kwargs):
        return await self.alpaca.call(fn, *args, retries=0, **kwargs)

    async def _quote(self, symbol: str) -> dict | None:
        from alpaca.data.requests import StockLatestQuoteRequest

        try:
            res = await self._call(self.alpaca.stock_data.get_stock_latest_quote,
                                   StockLatestQuoteRequest(symbol_or_symbols=symbol))
            q = res[symbol] if isinstance(res, dict) else res
            bid, ask = float(getattr(q, "bid_price", 0) or 0), float(getattr(q, "ask_price", 0) or 0)
            if bid <= 0 and ask <= 0:
                return None
            mid = (bid + ask) / 2 if bid and ask else (bid or ask)
            return {"bid": bid, "ask": ask, "mid": mid}
        except Exception as exc:  # noqa: BLE001
            log.warning("probe quote %s failed: %s", symbol, broker_message(exc))
            return None

    async def _asset(self, symbol: str) -> dict:
        try:
            a = await self._call(self.alpaca.trading.get_asset, symbol)
            return {"tradable": bool(getattr(a, "tradable", False)),
                    "shortable": bool(getattr(a, "shortable", False)),
                    "easy_to_borrow": bool(getattr(a, "easy_to_borrow", False)),
                    "fractionable": bool(getattr(a, "fractionable", False)),
                    "options": bool(getattr(a, "attributes", None) and "options_enabled" in getattr(a, "attributes"))}
        except Exception as exc:  # noqa: BLE001
            return {"tradable": False, "error": broker_message(exc)}

    async def _submit(self, check: str, request) -> tuple[Any | None, str | None]:
        """(order, None) on acceptance, (None, verbatim) on rejection."""
        try:
            order = await self._call(self.alpaca.trading.submit_order, request)
        except Exception as exc:  # noqa: BLE001
            return None, broker_message(exc)
        oid = str(getattr(order, "id", "") or "")
        if oid:
            self._orders[oid] = check
        sym = getattr(request, "symbol", None)
        if sym:
            self._symbols.add(str(sym))
        for leg in getattr(request, "legs", None) or []:
            self._symbols.add(str(leg.symbol))
        return order, None

    async def _order_status(self, order_id: str) -> str:
        try:
            o = await self._call(self.alpaca.trading.get_order_by_id, order_id)
            st = getattr(o, "status", "")
            return str(st.value if hasattr(st, "value") else st).lower()
        except Exception as exc:  # noqa: BLE001
            return f"error:{broker_message(exc)}"

    async def _wait_terminal(self, order_id: str, seconds: float, want: str = "filled") -> str:
        """Poll until the order reaches `want` or ANY terminal state (a filled
        order is as final as a cancelled one - waiting the full window on it
        would burn the cleanup budget)."""
        deadline = time.monotonic() + seconds
        status = ""
        while time.monotonic() < deadline:
            status = await self._order_status(order_id)
            terminal = any(t in status for t in ("filled", "cancel", "rejected", "expired"))                 and "partially" not in status
            if want in status or terminal:
                return status
            await asyncio.sleep(1.0)
        return status

    async def _cancel(self, order_id: str) -> None:
        try:
            await self.alpaca.call(self.alpaca.trading.cancel_order_by_id, order_id, retries=2)
        except Exception as exc:  # noqa: BLE001
            msg = broker_message(exc).lower()
            if not any(t in msg for t in ("filled", "cancel", "not found", "404", "422")):
                raise

    async def _accept_then_cancel(self, check: str, request, ok_detail: str) -> None:
        order, err = await self._submit(check, request)
        if order is None:
            self._row(check, "FAIL", err or "rejected", symbol=str(getattr(request, "symbol", "")))
            return
        oid = str(order.id)
        await self._cancel(oid)
        status = await self._wait_terminal(oid, 5.0, want="cancel")
        if "filled" in status and "partially" not in status:
            self._row(check, "PASS", f"{ok_detail}; FILLED before cancel (flattened in cleanup)",
                      symbol=str(getattr(request, "symbol", "")), order_ids=[oid])
        else:
            self._row(check, "PASS", ok_detail, symbol=str(getattr(request, "symbol", "")), order_ids=[oid])

    @staticmethod
    def _tick(x: float) -> float:
        return max(round(x, 2), 0.01)

    # ---- checks ----------------------------------------------------------

    async def _check_broker_flags(self) -> None:
        b = await self.refresh_broker()
        if b.get("error") and not b.get("status"):
            self._row("broker_flags", "FAIL", b["error"])
            return
        if b.get("trading_blocked"):
            self._state["trading_blocked_abort"] = True
            self._row("broker_flags", "FAIL", "trading_blocked=True - nothing else can be probed")
            return
        cfg = b.get("config") or {}
        self._row("broker_flags", "INFO",
                  f"approved L{b.get('options_approved_level')} effective L{b.get('options_trading_level')} "
                  f"shorting={b.get('shorting_enabled')} no_shorting={cfg.get('no_shorting')} "
                  f"mult={b.get('multiplier')} fractional={cfg.get('fractional_trading')} "
                  f"pdt={b.get('pattern_day_trader')} dt={b.get('daytrade_count')}")
        c = self._ctx
        c.margin = (b.get("multiplier") or 1) > 1
        c.equity = float(b.get("equity") or 0)
        c.cash = float(b.get("cash") or 0)
        c.settled_bp = float(b.get("non_marginable_buying_power") or b.get("buying_power") or 0)
        c.daytrade_count = int(b.get("daytrade_count") or 0)
        c.fractional = cfg.get("fractional_trading")

    async def _check_precondition(self) -> None:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        c = self._ctx
        c.now_et = datetime.now(ET)
        c.session = equity_session(datetime.now(timezone.utc))
        c.weekend = c.session is None
        try:
            c.rth = bool(await self.clock.is_open())
        except Exception:  # noqa: BLE001
            c.rth = c.session == "rth"
        # Stale probe orders from a crashed run.
        try:
            open_orders = await self._call(self.alpaca.trading.get_orders,
                                           GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
        except Exception as exc:  # noqa: BLE001
            self._row("precondition", "FAIL", f"cannot list open orders: {broker_message(exc)}")
            return
        stale = 0
        for o in open_orders or []:
            cid = str(getattr(o, "client_order_id", "") or "")
            if cid.startswith(PROBE_PREFIX):
                await self._cancel(str(o.id))
                stale += 1
            else:
                c.open_order_symbols.add(str(o.symbol))
        try:
            positions = await self._call(self.alpaca.trading.get_all_positions)
            c.held = {str(p.symbol) for p in positions or []}
        except Exception as exc:  # noqa: BLE001
            self._row("precondition", "FAIL", f"cannot list positions: {broker_message(exc)}")
            return
        try:
            plans = await self.trade.risk.open_plans()
            c.plan_symbols = {leg["symbol"] for p in plans for leg in (p.legs or [])}
        except Exception:  # noqa: BLE001
            c.plan_symbols = set()
        # Pick the probe symbols from live quotes.
        assets: dict[str, dict] = {}
        prices: dict[str, float] = {}
        for sym in dict.fromkeys(EQUITY_CANDIDATES + SHORT_CANDIDATES):
            assets[sym] = await self._asset(sym)
            q = await self._quote(sym) if assets[sym].get("tradable") else None
            if q:
                prices[sym] = q["ask"] or q["mid"]
        c.equity_symbol = pick_equity_symbol(EQUITY_CANDIDATES, prices, assets, c.held, c.plan_symbols,
                                             c.open_order_symbols)
        c.equity_price = prices.get(c.equity_symbol or "", 0.0)
        c.short_symbol = pick_equity_symbol(SHORT_CANDIDATES, prices, assets, c.held, c.plan_symbols,
                                            c.open_order_symbols, need_short=True)
        c.short_price = prices.get(c.short_symbol or "", 0.0)
        self._row("precondition", "INFO",
                  f"session={c.session} rth={c.rth} stale_probe_orders_cancelled={stale} "
                  f"equity={c.equity_symbol}@{c.equity_price:.2f} short={c.short_symbol}@{c.short_price:.2f} "
                  f"held={len(c.held)} plan_symbols={len(c.plan_symbols)}")

    def _equity_req(self, symbol: str, side: str, limit: float, *, tif="DAY", ext=False, cid: str):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        return LimitOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=getattr(TimeInForce, tif), limit_price=self._tick(limit),
            extended_hours=ext, client_order_id=cid,
        )

    async def _check_equity_buy_sell(self) -> None:
        c = self._ctx
        if c.weekend:
            self._row("equity_buy_sell", "SKIP", "weekend - no session to trade in")
            return
        if not c.equity_symbol:
            self._row("equity_buy_sell", "SKIP", "no probe symbol (all candidates held/unquoted)")
            return
        if c.margin and c.equity < 25_000 and c.daytrade_count >= 2:
            self._row("equity_buy_sell", "SKIP", f"margin account under $25k with {c.daytrade_count} day trades - a round trip would be a PDT strike")
            return
        if c.settled_bp < c.equity_price * 1.01:
            self._row("equity_buy_sell", "SKIP", f"settled buying power ${c.settled_bp:.0f} below one share (${c.equity_price:.2f})")
            return
        ext = not c.rth
        sym = c.equity_symbol
        buy, err = await self._submit("equity_buy_sell", self._equity_req(
            sym, "buy", c.equity_price * (1.005 if ext else 1.002), ext=ext,
            cid=f"{PROBE_PREFIX}buy-{uuid4().hex[:8]}"))
        if buy is None:
            self._row("equity_buy_sell", "FAIL", err or "buy rejected", symbol=sym)
            return
        st = await self._wait_terminal(str(buy.id), FILL_WAIT_S)
        if "filled" not in st or "partially" in st:
            await self._cancel(str(buy.id))
            self._row("equity_buy_sell", "INFO", f"buy accepted but not filled in {FILL_WAIT_S:.0f}s ({st}) - cancelled; acceptance proven, execution not",
                      symbol=sym, order_ids=[str(buy.id)])
            return
        q = await self._quote(sym) or {"bid": c.equity_price, "mid": c.equity_price}
        sell, err = await self._submit("equity_buy_sell", self._equity_req(
            sym, "sell", (q["bid"] or q["mid"]) * (0.995 if ext else 0.998), ext=ext,
            cid=f"{PROBE_PREFIX}sell-{uuid4().hex[:8]}"))
        if sell is None:
            self._row("equity_buy_sell", "FAIL", f"bought 1 {sym} but the sell was rejected: {err} (cleanup will flatten)", symbol=sym)
            return
        st2 = await self._wait_terminal(str(sell.id), FILL_WAIT_S)
        if "filled" in st2 and "partially" not in st2:
            self._row("equity_buy_sell", "PASS", f"BUY 1 {sym} filled; SELL filled - round trip complete",
                      symbol=sym, order_ids=[str(buy.id), str(sell.id)])
        else:
            self._row("equity_buy_sell", "INFO", f"bought 1 {sym}; sell not filled in {FILL_WAIT_S:.0f}s ({st2}) - cleanup flattens",
                      symbol=sym, order_ids=[str(buy.id), str(sell.id)])

    async def _check_equity_extended_hours(self) -> None:
        c = self._ctx
        if c.weekend or not c.equity_symbol:
            self._row("equity_extended_hours", "SKIP", "weekend or no probe symbol")
            return
        await self._accept_then_cancel("equity_extended_hours", self._equity_req(
            c.equity_symbol, "buy", c.equity_price * 0.5, ext=True,
            cid=f"{PROBE_PREFIX}ext-{uuid4().hex[:8]}"), "extended-hours limit accepted")

    async def _check_equity_short(self) -> None:
        c = self._ctx
        if c.weekend:
            self._row("equity_short", "SKIP", "weekend")
            return
        if not c.short_symbol:
            self._row("equity_short", "SKIP", "no shortable, easy-to-borrow candidate that is not held")
            return
        await self._accept_then_cancel("equity_short", self._equity_req(
            c.short_symbol, "sell", c.short_price * 1.05, ext=True,
            cid=f"{PROBE_PREFIX}short-{uuid4().hex[:8]}"), f"SELL 1 {c.short_symbol} (not held) accepted = shorting allowed")

    async def _check_equity_opg(self) -> None:
        c = self._ctx
        if c.weekend or not c.equity_symbol:
            self._row("equity_opg", "SKIP", "weekend or no probe symbol")
            return
        if not opg_window_open(c.now_et):
            self._row("equity_opg", "SKIP", "OPG orders are refused 09:28-19:00 ET - re-run outside that window")
            return
        await self._accept_then_cancel("equity_opg", self._equity_req(
            c.equity_symbol, "buy", c.equity_price * 0.5, tif="OPG",
            cid=f"{PROBE_PREFIX}opg-{uuid4().hex[:8]}"), "limit-on-open accepted")

    async def _check_equity_cls(self) -> None:
        c = self._ctx
        if c.weekend or not c.equity_symbol:
            self._row("equity_cls", "SKIP", "weekend or no probe symbol")
            return
        if not cls_window_open(c.now_et):
            self._row("equity_cls", "SKIP", "CLS orders are refused 15:50-19:00 ET - re-run outside that window")
            return
        await self._accept_then_cancel("equity_cls", self._equity_req(
            c.equity_symbol, "buy", c.equity_price * 0.5, tif="CLS",
            cid=f"{PROBE_PREFIX}cls-{uuid4().hex[:8]}"), "limit-on-close accepted")

    async def _contracts(self, underlying: str, right: str) -> tuple[list[dict], float, dict]:
        """Active contracts 7-30 DTE for one underlying with quotes."""
        from alpaca.data.requests import OptionLatestQuoteRequest
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        spot_q = await self._quote(underlying)
        spot = spot_q["mid"] if spot_q else 0.0
        if not spot:
            return [], 0.0, {}
        today = date.today()
        lo = spot * (0.80 if right == "P" else 1.0)
        hi = spot * (1.0 if right == "P" else 1.25)
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying], status=AssetStatus.ACTIVE,
            expiration_date_gte=today + timedelta(days=7), expiration_date_lte=today + timedelta(days=30),
            type=ContractType.PUT if right == "P" else ContractType.CALL,
            strike_price_gte=f"{lo:.2f}", strike_price_lte=f"{hi:.2f}", limit=100,
        )
        res = await self._call(self.alpaca.trading.get_option_contracts, req)
        raw = getattr(res, "option_contracts", None) or (res if isinstance(res, list) else [])
        contracts = [{"symbol": str(x.symbol), "strike": float(x.strike_price),
                      "expiry": str(x.expiration_date), "type": str(getattr(x.type, "value", x.type))}
                     for x in raw]
        if not contracts:
            return [], spot, {}
        quotes: dict[str, dict] = {}
        try:
            qres = await self._call(self.alpaca.option_data.get_option_latest_quote,
                                    OptionLatestQuoteRequest(symbol_or_symbols=[c["symbol"] for c in contracts],
                                                             feed=getattr(self.alpaca, "option_feed", None)))
            for sym, q in (qres or {}).items():
                quotes[str(sym)] = {"bid": float(getattr(q, "bid_price", 0) or 0),
                                    "ask": float(getattr(q, "ask_price", 0) or 0)}
        except Exception as exc:  # noqa: BLE001
            log.warning("probe option quotes failed: %s", broker_message(exc))
        return contracts, spot, quotes

    def _option_req(self, symbol: str, side: str, limit: float, intent: str, cid: str):
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        return LimitOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=self._tick(limit),
            position_intent=getattr(PositionIntent, intent), client_order_id=cid,
        )

    def _rth_skip(self, name: str) -> bool:
        if not self._ctx.rth:
            self._row(name, "SKIP", "options trade in RTH only - re-run 09:30-16:00 ET")
            return True
        return False

    async def _check_option_l2_long(self) -> None:
        if self._rth_skip("option_l2_long"):
            return
        contracts, spot, quotes = await self._contracts(OPTION_UNDERLYING, "P")
        leg = pick_far_otm(contracts, spot, quotes, right="P")
        if not leg:
            self._row("option_l2_long", "SKIP", f"no quotable far-OTM {OPTION_UNDERLYING} put found")
            return
        self._ctx_long_put = leg
        self._ctx_contracts = (contracts, quotes)
        await self._accept_then_cancel("option_l2_long", self._option_req(
            leg["symbol"], "buy", leg["bid"] * 0.5, "BUY_TO_OPEN", f"{PROBE_PREFIX}l2-{uuid4().hex[:8]}"),
            f"BUY_TO_OPEN {leg['symbol']} accepted = level >= 2")

    async def _check_option_l3_spread(self) -> None:
        from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        if self._rth_skip("option_l3_spread"):
            return
        leg = getattr(self, "_ctx_long_put", None)
        contracts, quotes = getattr(self, "_ctx_contracts", ([], {}))
        if not leg:
            self._row("option_l3_spread", "SKIP", "no long leg from option_l2_long")
            return
        short = pick_vertical(contracts, leg, quotes)
        if not short:
            self._row("option_l3_spread", "SKIP", "no adjacent strike for a vertical")
            return
        debit = max((leg["bid"] - short["ask"]) * 0.5, 0.01)
        req = LimitOrderRequest(
            qty=1, time_in_force=TimeInForce.DAY, order_class=OrderClass.MLEG,
            limit_price=self._tick(debit), client_order_id=f"{PROBE_PREFIX}l3-{uuid4().hex[:8]}",
            legs=[OptionLegRequest(symbol=leg["symbol"], side=OrderSide.BUY, ratio_qty=1,
                                   position_intent=PositionIntent.BUY_TO_OPEN),
                  OptionLegRequest(symbol=short["symbol"], side=OrderSide.SELL, ratio_qty=1,
                                   position_intent=PositionIntent.SELL_TO_OPEN)],
        )
        await self._accept_then_cancel("option_l3_spread", req,
                                       f"debit vertical {leg['strike']}/{short['strike']} accepted = level 3")

    async def _check_option_short_put(self) -> None:
        if self._rth_skip("option_short_put"):
            return
        c = self._ctx
        for underlying in CHEAP_OPTION_UNDERLYINGS:
            contracts, spot, quotes = await self._contracts(underlying, "P")
            leg = pick_far_otm(contracts, spot, quotes, right="P", min_bid=0.02)
            if leg and leg["strike"] * 100 <= max(c.cash, c.settled_bp) * 0.25:
                order, err = await self._submit("option_short_put", self._option_req(
                    leg["symbol"], "sell", leg["ask"] * 1.5, "SELL_TO_OPEN",
                    f"{PROBE_PREFIX}csp-{uuid4().hex[:8]}"))
                if order is None:
                    low = (err or "").lower()
                    if "buying power" in low or "insufficient" in low:
                        self._row("option_short_put", "INFO", f"inconclusive (collateral, not entitlement): {err}")
                    else:
                        self._row("option_short_put", "FAIL", err or "rejected", symbol=leg["symbol"])
                    return
                await self._cancel(str(order.id))
                await self._wait_terminal(str(order.id), 5.0, want="cancel")
                self._row("option_short_put", "PASS", f"SELL_TO_OPEN {leg['symbol']} accepted (cash-secured put)",
                          symbol=leg["symbol"], order_ids=[str(order.id)])
                return
        self._row("option_short_put", "SKIP", "no cheap far-OTM put with strike*100 <= 25% of cash")

    async def _check_option_naked_call(self) -> None:
        if self._rth_skip("option_naked_call"):
            return
        contracts, spot, quotes = await self._contracts(OPTION_UNDERLYING, "C")
        leg = pick_far_otm(contracts, spot, quotes, right="C")
        if not leg:
            self._row("option_naked_call", "SKIP", f"no quotable far-OTM {OPTION_UNDERLYING} call found")
            return
        order, err = await self._submit("option_naked_call", self._option_req(
            leg["symbol"], "sell", leg["ask"] * 1.5, "SELL_TO_OPEN", f"{PROBE_PREFIX}nc-{uuid4().hex[:8]}"))
        if order is None:
            self._row("option_naked_call", "FAIL", err or "rejected", symbol=leg["symbol"])
            return
        await self._cancel(str(order.id))
        await self._wait_terminal(str(order.id), 5.0, want="cancel")
        self._row("option_naked_call", "PASS", f"naked SELL_TO_OPEN {leg['symbol']} ACCEPTED - uncovered calls allowed",
                  symbol=leg["symbol"], order_ids=[str(order.id)])

    async def _check_option_l1_covered(self) -> None:
        if self._rth_skip("option_l1_covered"):
            return
        c = self._ctx
        try:
            positions = await self._call(self.alpaca.trading.get_all_positions)
        except Exception as exc:  # noqa: BLE001
            self._row("option_l1_covered", "SKIP", f"positions unavailable: {broker_message(exc)}")
            return
        lot = next((p for p in positions or []
                    if float(p.qty) >= 100 and str(p.symbol) not in c.plan_symbols
                    and len(str(p.symbol)) <= 5), None)
        if lot is None:
            self._row("option_l1_covered", "SKIP", "no 100-share lot held outside a managed plan")
            return
        contracts, spot, quotes = await self._contracts(str(lot.symbol), "C")
        leg = pick_far_otm(contracts, spot, quotes, right="C", min_bid=0.02)
        if not leg:
            self._row("option_l1_covered", "SKIP", f"no quotable far-OTM call on {lot.symbol}")
            return
        await self._accept_then_cancel("option_l1_covered", self._option_req(
            leg["symbol"], "sell", leg["ask"] * 1.5, "SELL_TO_OPEN", f"{PROBE_PREFIX}cc-{uuid4().hex[:8]}"),
            f"covered call on {lot.symbol} accepted")

    async def _check_fractional(self) -> None:
        f = self._ctx.fractional
        self._row("fractional", "INFO", f"fractional_trading={f} (account configuration; no order placed)")

    # ---- cleanup ---------------------------------------------------------

    async def _cleanup(self) -> None:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        self._begin("cleanup")
        cancelled = 0
        for oid in list(self._orders):
            try:
                await self._cancel(oid)
                await self._wait_terminal(oid, 5.0, want="cancel")
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("probe cleanup cancel %s: %s", oid, broker_message(exc))
        try:
            open_orders = await self._call(self.alpaca.trading.get_orders,
                                           GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200))
            for o in open_orders or []:
                if str(getattr(o, "client_order_id", "") or "").startswith(PROBE_PREFIX):
                    await self._cancel(str(o.id))
                    cancelled += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("probe cleanup sweep: %s", broker_message(exc))
        leftovers = await self._flatten_probe_positions()
        if leftovers:
            self._state["manual_action"] = "FLATTEN MANUALLY: " + ", ".join(leftovers)
            self._row("cleanup", "FAIL", self._state["manual_action"])
        else:
            self._row("cleanup", "INFO", f"{cancelled} probe order(s) cancelled, no probe position left")

    async def _flatten_probe_positions(self) -> list[str]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        leftovers: list[str] = []
        if not self._symbols:
            return leftovers
        try:
            positions = await self._call(self.alpaca.trading.get_all_positions)
        except Exception as exc:  # noqa: BLE001
            return [f"positions unavailable ({broker_message(exc)})"]
        for p in positions or []:
            sym = str(p.symbol)
            if sym not in self._symbols:
                continue
            qty = float(p.qty)
            if qty == 0:
                continue
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            q = await self._quote(sym) or {}
            ref = float(getattr(p, "current_price", 0) or 0) or q.get("mid") or float(p.avg_entry_price)
            rth = self._ctx.rth
            for mult in ((0.998, 1.002), (0.99, 1.01)):
                px = ref * (mult[0] if qty > 0 else mult[1])
                try:
                    o = await self._call(self.alpaca.trading.submit_order, LimitOrderRequest(
                        symbol=sym, qty=abs(qty), side=side, time_in_force=TimeInForce.DAY,
                        limit_price=self._tick(px), extended_hours=not rth,
                        client_order_id=f"{PROBE_PREFIX}flat-{uuid4().hex[:8]}"))
                    st = await self._wait_terminal(str(o.id), FLATTEN_WAIT_S)
                    if "filled" in st and "partially" not in st:
                        break
                    await self._cancel(str(o.id))
                except Exception as exc:  # noqa: BLE001
                    log.warning("probe flatten %s: %s", sym, broker_message(exc))
            else:
                if rth:
                    try:
                        await self._call(self.alpaca.trading.close_position, sym)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        leftovers.append(f"{sym} x{qty:g} ({broker_message(exc)})")
                        continue
                try:
                    await self._call(self.alpaca.trading.submit_order, LimitOrderRequest(
                        symbol=sym, qty=abs(qty), side=side, time_in_force=TimeInForce.DAY,
                        limit_price=self._tick(ref * (0.97 if qty > 0 else 1.03)), extended_hours=True,
                        client_order_id=f"{PROBE_PREFIX}flat-{uuid4().hex[:8]}"))
                except Exception:  # noqa: BLE001
                    pass
                leftovers.append(f"{sym} x{qty:g} (resting ext-hours limit left)")
        return leftovers
