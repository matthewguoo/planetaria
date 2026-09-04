"""Account capabilities: pure derivation + ceiling, the persisted blob, the
RiskService ceiling merge, the live gate reading it, and the probe runner
against a scripted fake broker (paper-margin PASS everywhere; live-IRA
short + spread refused; RTH gating; cleanup on failure)."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.session import Database
from app.services import capabilities as capmod
from app.services.capabilities import (
    CapabilitiesService,
    ProbeRunning,
    broker_message,
    ceiling_from,
    cls_window_open,
    derive,
    opg_window_open,
    pick_equity_symbol,
    pick_far_otm,
    pick_vertical,
)
from app.services.risk import RiskService

ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------ pure layer


class TestPure:
    def test_broker_message_prefers_api_error_text(self):
        exc = SimpleNamespace(message='{"code":40310000,"message":"account is not allowed to short"}')
        assert "not allowed to short" in broker_message(exc)
        assert broker_message(ValueError("plain")) == "plain"

    def test_opg_cls_windows(self):
        assert opg_window_open(datetime(2026, 9, 4, 7, 0, tzinfo=ET))
        assert not opg_window_open(datetime(2026, 9, 4, 10, 0, tzinfo=ET))
        assert opg_window_open(datetime(2026, 9, 4, 19, 30, tzinfo=ET))
        assert cls_window_open(datetime(2026, 9, 4, 10, 0, tzinfo=ET))
        assert not cls_window_open(datetime(2026, 9, 4, 15, 55, tzinfo=ET))

    def test_pick_equity_symbol_respects_every_exclusion(self):
        assets = {s: {"tradable": True, "shortable": s != "F", "easy_to_borrow": True} for s in ("SPLG", "XLF", "F")}
        prices = {"SPLG": 71.0, "XLF": 45.0, "F": 11.0}
        assert pick_equity_symbol(("SPLG", "XLF", "F"), prices, assets, set(), set(), set()) == "F"  # only one <= 40
        assert pick_equity_symbol(("F",), prices, assets, {"F"}, set(), set()) is None            # held
        assert pick_equity_symbol(("F",), prices, assets, set(), {"F"}, set()) is None            # plan leg
        assert pick_equity_symbol(("F",), prices, assets, set(), set(), {"F"}) is None            # open order
        assert pick_equity_symbol(("F",), prices, assets, set(), set(), set(), need_short=True) is None
        assert pick_equity_symbol(("XLF",), prices, assets, set(), set(), set(), max_price=50, need_short=True) == "XLF"

    def test_pick_far_otm_and_vertical(self):
        contracts = [
            {"symbol": "SPY_P600", "strike": 600.0, "expiry": "2026-09-18", "type": "put"},
            {"symbol": "SPY_P590", "strike": 590.0, "expiry": "2026-09-18", "type": "put"},
            {"symbol": "SPY_P640", "strike": 640.0, "expiry": "2026-09-18", "type": "put"},
            {"symbol": "SPY_C750", "strike": 750.0, "expiry": "2026-09-18", "type": "call"},
        ]
        quotes = {"SPY_P600": {"bid": 0.40, "ask": 0.45}, "SPY_P590": {"bid": 0.25, "ask": 0.30},
                  "SPY_P640": {"bid": 2.0, "ask": 2.1}, "SPY_C750": {"bid": 0.30, "ask": 0.35}}
        long = pick_far_otm(contracts, 670.0, quotes, right="P")
        assert long["symbol"] == "SPY_P590"  # cheapest far-OTM with a bid
        short = pick_vertical(contracts, {**long, "strike": 600.0}, quotes)
        assert short["symbol"] == "SPY_P590"
        assert pick_far_otm(contracts, 670.0, quotes, right="C")["symbol"] == "SPY_C750"
        assert pick_far_otm(contracts, 670.0, {}, right="P") is None

    def test_derive_precedence_probe_over_broker_over_default(self):
        broker = {"options_trading_level": 3, "shorting_enabled": True, "multiplier": 2.0,
                  "config": {"no_shorting": False, "fractional_trading": True}}
        d, s = derive(broker, [], "paper")
        assert (d["options_level"], s["options_level"]) == (3, "broker")
        assert (d["equity_shorts"], s["equity_shorts"]) == (True, "broker")
        assert d["fractional"] is True and d["cash_account"] is False
        checks = [{"name": "option_l2_long", "status": "PASS"},
                  {"name": "option_l3_spread", "status": "FAIL"},
                  {"name": "equity_short", "status": "FAIL"},
                  {"name": "equity_opg", "status": "SKIP"}]
        d, s = derive(broker, checks, "paper")
        assert (d["options_level"], s["options_level"]) == (2, "probe")
        assert (d["equity_shorts"], s["equity_shorts"]) == (False, "probe")
        assert d["opg"] is None and s["opg"] == "unknown"   # SKIP never overrides
        d, s = derive({}, [], "live_manual")
        assert (d["options_level"], s["options_level"]) == (2, "default")
        assert d["equity_shorts"] is False
        d, s = derive({}, [], "paper")
        assert d["options_level"] == 3 and d["equity_shorts"] is None

    def test_ceiling(self):
        assert ceiling_from({"options_level": 3, "equity_shorts": None}, {"options_level": "default", "equity_shorts": "default"}, "paper") is None
        assert ceiling_from({"options_level": 2, "equity_shorts": False}, {"options_level": "default", "equity_shorts": "default"}, "live_manual") == {"options_level": 2, "equity_shorts": False}
        assert ceiling_from({"options_level": 3, "equity_shorts": True}, {"options_level": "probe", "equity_shorts": "probe"}, "live_manual") == {"options_level": 3, "equity_shorts": True}


# ------------------------------------------------------------ fake broker


class APIError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class FakeOrder:
    def __init__(self, oid, symbol, status="accepted", client_order_id=""):
        self.id = oid
        self.symbol = symbol
        self.status = status
        self.client_order_id = client_order_id


class FakeTrading:
    """Scripted broker: `reject` maps a shape key to the verbatim refusal,
    `fills` is the set of shape keys that fill instantly."""

    def __init__(self, *, account=None, config=None, reject=None, fills=(), positions=(), shortable=True):
        self.account = account or SimpleNamespace(
            status="ACTIVE", trading_blocked=False, options_approved_level=3, options_trading_level=3,
            shorting_enabled=True, pattern_day_trader=False, daytrade_count=0, multiplier="2",
            equity="50000", cash="20000", non_marginable_buying_power="20000", buying_power="40000")
        self.config = config or SimpleNamespace(no_shorting=False, fractional_trading=True,
                                                max_margin_multiplier="2", max_options_trading_level=3,
                                                ptp_no_exception_entry=False)
        self.reject = reject or {}
        self.fills = set(fills)
        self.orders: dict[str, FakeOrder] = {}
        self.submitted: list = []
        self.cancelled: list[str] = []
        self.positions = list(positions)
        self.shortable = shortable
        self._n = 0
        self.closed: list[str] = []

    @staticmethod
    def shape(req) -> str:
        legs = getattr(req, "legs", None)
        if legs:
            return "mleg"
        intent = str(getattr(req, "position_intent", "") or "")
        side = str(getattr(req, "side", ""))
        tif = str(getattr(req, "time_in_force", ""))
        if "sell_to_open" in intent.lower():
            return "sto"
        if "buy_to_open" in intent.lower():
            return "bto"
        if "opg" in tif.lower():
            return "opg"
        if "cls" in tif.lower():
            return "cls"
        if "sell" in side.lower():
            return "sell"
        return "buy"

    def get_account(self):
        return self.account

    def get_account_configurations(self):
        return self.config

    def get_asset(self, symbol):
        return SimpleNamespace(tradable=True, shortable=self.shortable, easy_to_borrow=self.shortable,
                               fractionable=True, attributes=["options_enabled"])

    def get_orders(self, req):
        return [o for o in self.orders.values() if o.status in ("accepted", "new")]

    def get_all_positions(self):
        return list(self.positions)

    def submit_order(self, req):
        shape = self.shape(req)
        self.submitted.append((shape, req))
        if shape in self.reject:
            raise APIError(self.reject[shape])
        self._n += 1
        sym = getattr(req, "symbol", None) or "MLEG"
        status = "filled" if shape in self.fills else "accepted"
        o = FakeOrder(f"o{self._n}", sym, status, getattr(req, "client_order_id", ""))
        self.orders[o.id] = o
        if status == "filled" and shape == "buy":
            self.positions.append(SimpleNamespace(symbol=sym, qty="1", avg_entry_price="10", current_price="10"))
        if status == "filled" and shape == "sell":
            self.positions = [p for p in self.positions if str(p.symbol) != sym]
        return o

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        o = self.orders.get(oid)
        if o and o.status != "filled":
            o.status = "canceled"

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.positions = [p for p in self.positions if str(p.symbol) != symbol]

    def get_option_contracts(self, req):
        right = "put" if "PUT" in str(req.type).upper() else "call"
        u = req.underlying_symbols[0]
        strikes = (560, 570, 580) if right == "put" else (720, 730)
        return SimpleNamespace(option_contracts=[
            SimpleNamespace(symbol=f"{u}_{right[0].upper()}{k}", strike_price=str(k),
                            expiration_date="2026-09-18", type=right) for k in strikes])


class FakeStockData:
    def __init__(self, prices):
        self.prices = prices

    def get_stock_latest_quote(self, req):
        sym = req.symbol_or_symbols
        p = self.prices.get(sym, 0)
        return {sym: SimpleNamespace(bid_price=p * 0.999, ask_price=p * 1.001)}


class FakeOptionData:
    def get_option_latest_quote(self, req):
        return {s: SimpleNamespace(bid_price=0.30, ask_price=0.35) for s in req.symbol_or_symbols}


class FakeAlpaca:
    def __init__(self, trading, prices=None):
        self.configured = True
        self.trading = trading
        self.stock_data = FakeStockData(prices or {"SPLG": 20.0, "SCHX": 25.0, "XLF": 30.0, "SCHD": 28.0,
                                                    "F": 11.0, "SOFI": 9.0, "SPY": 650.0, "NIO": 5.0})
        self.option_data = FakeOptionData()
        self.option_feed = None

    async def call(self, fn, *args, **kwargs):
        kwargs.pop("retries", None)
        kwargs.pop("timeout", None)
        return fn(*args, **kwargs)


class FakeRisk:
    async def open_plans(self):
        return []


def _service(db, trading, mode="paper", rth=True, session="rth", prices=None):
    settings = SimpleNamespace(trading_mode=mode, alpaca_account_name="acct")
    clock = SimpleNamespace(is_open=lambda: _coro(rth))
    svc = CapabilitiesService(db, FakeAlpaca(trading, prices), SimpleNamespace(risk=FakeRisk()), clock, settings)
    return svc


async def _coro(v):
    return v


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/caps.db")
    yield database
    await database.close()


async def _run(svc, **kw):
    await svc.start_probe(**kw)
    await svc._task
    return {c["name"]: c for c in svc.status()["checks"]}


# ------------------------------------------------------------ probe scenarios


@pytest.mark.asyncio
async def test_paper_margin_everything_passes(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 2.0)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    trading = FakeTrading(fills={"buy", "sell"})
    svc = _service(db, trading)
    rows = await _run(svc)
    assert rows["equity_buy_sell"]["status"] == "PASS"
    assert rows["equity_short"]["status"] == "PASS"
    assert rows["option_l2_long"]["status"] == "PASS"
    assert rows["option_l3_spread"]["status"] == "PASS"
    assert rows["option_naked_call"]["status"] == "PASS"
    assert rows["cleanup"]["status"] == "INFO"
    d = svc.status()["derived"]
    assert d["options_level"] == 3 and d["equity_shorts"] is True and d["cash_account"] is False
    assert svc.ceiling() == {"options_level": 3, "equity_shorts": True}
    # every accepted order was cancelled; no probe position left
    accepted = [o for o in trading.orders.values()]
    assert all(o.status in ("canceled", "filled") for o in accepted)
    assert not [p for p in trading.positions if str(p.symbol) in svc._symbols]
    assert svc.status()["probed_at"] is not None and not svc.running


@pytest.mark.asyncio
async def test_live_ira_records_verbatim_refusals(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 2.0)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    account = SimpleNamespace(
        status="ACTIVE", trading_blocked=False, options_approved_level=2, options_trading_level=2,
        shorting_enabled=False, pattern_day_trader=False, daytrade_count=0, multiplier="1",
        equity="10900", cash="6000", non_marginable_buying_power="6000", buying_power="6000")
    config = SimpleNamespace(no_shorting=True, fractional_trading=True, max_margin_multiplier="1",
                             max_options_trading_level=3, ptp_no_exception_entry=False)
    trading = FakeTrading(account=account, config=config, fills={"buy", "sell"},
                          reject={"sell": "account is not allowed to short",
                                  "mleg": "options level 3 required for multi-leg orders",
                                  "sto": "account not eligible to trade uncovered option contracts"})
    # the round trip's SELL must still work: reject only sells of NOT-held symbols
    real_submit = trading.submit_order

    def submit(req):
        if FakeTrading.shape(req) == "sell" and any(str(p.symbol) == req.symbol for p in trading.positions):
            trading.reject.pop("sell", None)
            try:
                return real_submit(req)
            finally:
                trading.reject["sell"] = "account is not allowed to short"
        return real_submit(req)

    trading.submit_order = submit
    svc = _service(db, trading, mode="live_manual")
    with pytest.raises(ValueError, match="confirm"):
        await svc.start_probe()
    rows = await _run(svc, confirm="LIVE")
    assert rows["equity_buy_sell"]["status"] == "PASS"
    assert rows["equity_short"]["status"] == "FAIL" and "not allowed to short" in rows["equity_short"]["detail"]
    assert rows["option_l2_long"]["status"] == "PASS"
    assert rows["option_l3_spread"]["status"] == "FAIL" and "level 3" in rows["option_l3_spread"]["detail"]
    assert rows["option_naked_call"]["status"] == "FAIL"
    d = svc.status()["derived"]
    assert d["options_level"] == 2 and d["equity_shorts"] is False and d["cash_account"] is True
    assert svc.ceiling() == {"options_level": 2, "equity_shorts": False}
    assert svc.options_ceiling() == 2 and "verified by probe" in svc.level_provenance()


@pytest.mark.asyncio
async def test_closed_market_skips_option_checks_but_runs_equity_acceptance(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 0.5)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "postmarket")
    trading = FakeTrading()
    svc = _service(db, trading, rth=False)
    rows = await _run(svc)
    for name in ("option_l2_long", "option_l3_spread", "option_short_put", "option_naked_call", "option_l1_covered"):
        assert rows[name]["status"] == "SKIP" and "RTH" in rows[name]["detail"]
    assert rows["equity_extended_hours"]["status"] == "PASS"
    assert rows["equity_buy_sell"]["status"] == "INFO"  # accepted, never filled, cancelled
    assert not trading.submitted or all(
        getattr(req, "extended_hours", False) or FakeTrading.shape(req) in ("opg", "cls")
        for _, req in trading.submitted)


@pytest.mark.asyncio
async def test_weekend_skips_every_order_check(db, monkeypatch):
    monkeypatch.setattr(capmod, "equity_session", lambda *_: None)
    trading = FakeTrading()
    svc = _service(db, trading, rth=False)
    rows = await _run(svc)
    assert not trading.submitted
    assert all(rows[n]["status"] == "SKIP" for n in ("equity_buy_sell", "equity_short", "equity_opg", "equity_cls"))


@pytest.mark.asyncio
async def test_mid_run_exception_still_cleans_up(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 0.5)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    trading = FakeTrading()
    boom = {"n": 0}
    real = trading.get_order_by_id

    def get_order_by_id(oid):
        boom["n"] += 1
        if boom["n"] == 2:
            raise RuntimeError("broker hiccup")
        return real(oid)

    trading.get_order_by_id = get_order_by_id
    svc = _service(db, trading)
    rows = await _run(svc)
    assert not svc.running and rows["cleanup"]["status"] in ("INFO", "FAIL")
    assert all(o.status in ("canceled", "filled") for o in trading.orders.values())


@pytest.mark.asyncio
async def test_unfillable_probe_position_sets_manual_action(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 0.3)
    monkeypatch.setattr(capmod, "FLATTEN_WAIT_S", 0.3)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    trading = FakeTrading(fills={"buy"})            # buys fill, sells never do
    trading.close_position = lambda sym: (_ for _ in ()).throw(APIError("market closed"))
    svc = _service(db, trading)
    rows = await _run(svc)
    assert rows["cleanup"]["status"] == "FAIL"
    assert svc.status()["manual_action"].startswith("FLATTEN MANUALLY")


@pytest.mark.asyncio
async def test_stale_probe_orders_are_swept_and_second_start_refused(db, monkeypatch):
    monkeypatch.setattr(capmod, "equity_session", lambda *_: None)
    trading = FakeTrading()
    trading.orders["stale"] = FakeOrder("stale", "SPLG", "accepted", "probe-buy-deadbeef")
    trading.orders["mine"] = FakeOrder("mine", "AVGG", "accepted", "abc-e")
    svc = _service(db, trading, rth=False)
    await svc.start_probe()
    with pytest.raises(ProbeRunning):
        await svc.start_probe()
    await svc._task
    assert "stale" in trading.cancelled and "mine" not in trading.cancelled
    assert "AVGG" in svc._ctx.open_order_symbols


@pytest.mark.asyncio
async def test_persistence_round_trip_and_apply(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 0.5)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    trading = FakeTrading(fills={"buy", "sell"})
    svc = _service(db, trading)
    await _run(svc)
    fresh = _service(db, FakeTrading())
    await fresh.load()
    assert fresh.status()["derived"]["options_level"] == 3
    assert fresh.status()["checks"][-1]["name"] == "cleanup"
    other = CapabilitiesService(db, FakeAlpaca(FakeTrading()), SimpleNamespace(risk=FakeRisk()), None,
                                SimpleNamespace(trading_mode="paper", alpaca_account_name="other"))
    await other.load()
    assert other.status()["probed_at"] is None            # per-account isolation

    risk = RiskService(db)
    await risk.update_settings({"options_level": 2, "equity_long_only": True})
    risk.capabilities = fresh
    st = fresh.status(stored_risk=await risk.get_stored_settings())
    assert st["apply_pending"] is True
    patch = await fresh.apply_to_risk(risk)
    assert patch == {"options_level": 3, "equity_long_only": False}
    eff = await risk.get_settings()
    assert eff["options_level"] == 3 and eff["equity_long_only"] is False
    assert fresh.status(stored_risk=await risk.get_stored_settings())["apply_pending"] is False


# ------------------------------------------------------------ the ceiling


@pytest.mark.asyncio
async def test_risk_settings_are_capped_by_the_ceiling(db):
    risk = RiskService(db)
    await risk.update_settings({"options_level": 3, "equity_long_only": False})
    assert (await risk.get_settings())["options_level"] == 3   # no capabilities: unchanged
    risk.capabilities = SimpleNamespace(ceiling=lambda: {"options_level": 2, "equity_shorts": False})
    eff = await risk.get_settings()
    assert eff["options_level"] == 2 and eff["equity_long_only"] is True
    assert eff["capability_ceiling"] == {"options_level": 2, "equity_shorts": False}
    with pytest.raises(ValueError):
        await risk.update_settings({"capability_ceiling": {}})
    stored = await risk.get_stored_settings()
    assert stored["options_level"] == 3 and stored["equity_long_only"] is False


@pytest.mark.asyncio
async def test_abort_cancels_and_cleans(db, monkeypatch):
    monkeypatch.setattr(capmod, "FILL_WAIT_S", 30.0)
    monkeypatch.setattr(capmod, "equity_session", lambda *_: "rth")
    trading = FakeTrading()      # buy accepted, never fills -> the runner waits
    svc = _service(db, trading)
    await svc.start_probe()
    await asyncio.sleep(0.3)
    assert svc.running
    assert await svc.abort() is True
    assert not svc.running
    rows = {c["name"]: c for c in svc.status()["checks"]}
    assert rows["probe"]["status"] == "INFO" and "aborted" in rows["probe"]["detail"]
    assert all(o.status == "canceled" for o in trading.orders.values())
