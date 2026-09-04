"""ContractsService: the option facts, snapshot and volume behind the
position sheet, with single-flight TTL caches; and the /api/holdings/{symbol}
+ /api/orders/open shapes (owner stamping, PATCH refusals)."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.trading import router
from app.services import contracts as cmod
from app.services.contracts import ContractsService

OCC = "NVDA260904P00230000"


class FakeAlpaca:
    def __init__(self):
        self.calls = 0
        self.option_feed = None
        self.trading = SimpleNamespace(get_option_contract=self._contract)
        self.option_data = SimpleNamespace(get_option_snapshot=self._snap, get_option_bars=self._bars)

    def _contract(self, occ):
        self.calls += 1
        return SimpleNamespace(style=SimpleNamespace(value="american"), size="100", open_interest="4635",
                               open_interest_date=date(2026, 9, 3), tradable=True, close_price="2.51",
                               underlying_symbol="NVDA")

    def _snap(self, req):
        self.calls += 1
        q = SimpleNamespace(bid_price=2.43, ask_price=2.47, bid_size=12, ask_size=9)
        t = SimpleNamespace(price=6.25, size=1, timestamp=datetime(2026, 9, 3, 19, 59, tzinfo=timezone.utc))
        g = SimpleNamespace(delta=-0.52, gamma=0.03, theta=-0.9, vega=0.05)
        return {OCC: SimpleNamespace(latest_quote=q, latest_trade=t, greeks=g, implied_volatility=0.61)}

    def _bars(self, req):
        self.calls += 1
        return SimpleNamespace(data={OCC: [SimpleNamespace(volume=3300)]})

    async def call(self, fn, /, *args, **kwargs):
        kwargs.pop("retries", None)
        return fn(*args, **kwargs)


@pytest.mark.asyncio
async def test_detail_shape_and_caching():
    alpaca = FakeAlpaca()
    svc = ContractsService(alpaca, None)
    d = await svc.detail(OCC)
    assert d["contract"]["open_interest"] == 4635 and d["contract"]["style"] == "american"
    assert d["contract"]["open_interest_date"] == "2026-09-03" and d["contract"]["size"] == 100.0
    assert d["quote"]["bid"] == 2.43 and d["quote"]["last"] == 6.25 and d["quote"]["volume"] == 3300
    assert d["quote"]["iv"] == 0.61 and d["quote"]["delta"] == -0.52 and d["quote"]["mid"] == 2.45
    first = alpaca.calls
    await svc.detail(OCC)                      # all three cached
    assert alpaca.calls == first
    # concurrent callers share one fetch
    svc2 = ContractsService(alpaca, None)
    before = alpaca.calls
    await asyncio.gather(svc2.snapshot(OCC), svc2.snapshot(OCC), svc2.snapshot(OCC))
    assert alpaca.calls == before + 1


@pytest.mark.asyncio
async def test_failed_fetch_is_null_not_an_error(monkeypatch):
    alpaca = FakeAlpaca()

    def boom(*_):
        raise RuntimeError("no such contract")

    alpaca.trading.get_option_contract = boom
    svc = ContractsService(alpaca, None)
    assert await svc.contract_meta(OCC) is None
    monkeypatch.setattr(cmod, "META_TTL_S", 0.0)
    assert await svc.contract_meta(OCC) is None


# ------------------------------------------------------------- routes


def _app(plans, holdings, orders):
    app = FastAPI()
    app.include_router(router)

    class Risk:
        async def open_plans(self):
            return plans

    class Trade:
        alpaca = SimpleNamespace(configured=True, trading=SimpleNamespace(
            get_orders=lambda req: orders, replace_order_by_id=lambda oid, req: SimpleNamespace(
                id=oid, status="accepted", limit_price=req.limit_price, qty=req.qty)))

        async def holdings(self, plans):
            return holdings

    class Market:
        def latest_quote(self, s):
            return {"bid": 22.9, "ask": 23.0, "mid": 22.95}

        def spot(self, s):
            return 22.95

    Trade.alpaca.call = _direct
    app.state.risk = Risk()
    app.state.trade = Trade()
    app.state.market = Market()
    app.state.contracts = ContractsService(FakeAlpaca(), None)
    app.state.symbols = None
    return app


async def _direct(fn, /, *args, **kwargs):
    kwargs.pop("retries", None)
    return fn(*args, **kwargs)


def _order(oid, symbol, submitted):
    return SimpleNamespace(id=oid, symbol=symbol, side=SimpleNamespace(value="sell"), qty="1", filled_qty="0",
                           order_type=SimpleNamespace(value="limit"), limit_price="33", status=SimpleNamespace(value="new"),
                           submitted_at=datetime(2026, 9, 3, 18, 50, submitted, tzinfo=timezone.utc), legs=[])


def test_open_orders_are_owner_stamped_and_sorted_and_replace_refuses_plan_orders():
    plan = SimpleNamespace(id="plan1", entry_order_id="e1", tp_order_id="tp1", exit_order_id=None,
                           partial_exit={"order_id": "px1"})
    orders = [_order("e1", "SPY", 1), _order("free", "AVGG", 5), _order("px1", "SPY", 3)]
    client = TestClient(_app([plan], [], orders))
    rows = client.get("/api/orders/open").json()["orders"]
    assert [r["id"] for r in rows] == ["free", "px1", "e1"]         # newest first
    assert rows[2]["plan_id"] == "plan1" and rows[2]["role"] == "entry"
    assert rows[1]["role"] == "partial" and rows[0]["plan_id"] is None
    assert client.patch("/api/orders/e1", json={"limit_price": 1.2}).status_code == 409
    assert client.patch("/api/orders/free", json={}).status_code == 422
    ok = client.patch("/api/orders/free", json={"limit_price": 32.0})
    assert ok.status_code == 200 and ok.json()["limit_price"] == 32.0


def test_holding_detail_option_and_stock():
    holdings = [
        {"symbol": OCC, "occ": {"underlying": "NVDA", "expiry": "2026-09-04", "right": "P", "strike": 230.0},
         "underlying": "NVDA", "qty": 1.0, "protection": "premium"},
        {"symbol": "AVGG", "occ": None, "underlying": "AVGG", "qty": 100.0, "protection": "none"},
    ]
    client = TestClient(_app([], holdings, []))
    d = client.get(f"/api/holdings/{OCC}").json()
    assert d["position"]["symbol"] == OCC and d["contract"]["open_interest"] == 4635
    assert d["quote"]["last"] == 6.25 and d["underlying"] == {"symbol": "NVDA", "spot": 22.95}
    s = client.get("/api/holdings/avgg").json()
    assert s["contract"] is None and s["quote"]["bid"] == 22.9
    assert client.get("/api/holdings/NOPE").status_code == 404
