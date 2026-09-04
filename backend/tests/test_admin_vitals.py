"""The admin window's vitals: request latency / throughput from the ASGI
middleware, broker call latency from the call log, the ticker cache, the
per-plan monitor rows, and the two routes."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_mod
from app.api.routes.admin_vitals import router
from app.services.call_log import CallLog
from app.services.vitals import (
    RequestStats,
    broker_vitals,
    cache_vitals,
    latency_summary,
    monitors_status,
    request_timing_middleware,
)


def test_latency_summary_and_windows():
    assert latency_summary([]) == {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    s = latency_summary([1, 2, 3, 4, 100])
    assert s["count"] == 5 and s["avg_ms"] == 22 and s["p50_ms"] == 3 and s["max_ms"] == 100
    stats = RequestStats()
    now = 1_000.0
    for i in range(6):
        stats.record("GET", "/api/positions", 200, 10 + i, now=now - i * 20)   # 6 over 100s
    stats.record("GET", "/api/admin/summary", 200, 1.0, now=now)               # admin: excluded
    stats.record("POST", "/api/trade", 500, 50.0, now=now - 5)
    w = stats.window(60, now=now)
    # 4 positions polls inside 60s (0, 20, 40, 60s ago: the edge is in) + the 500
    assert w["count"] == 5 and w["errors_5xx"] == 1 and w["rps"] == round(5 / 60, 2)
    assert stats.window(60, now=now, include_admin=True)["count"] == 6
    top = stats.top_routes(300, now=now)
    assert top[0]["route"] == "GET /api/positions" and top[0]["count"] == 6
    assert stats.summary(now=now)["errors_5xx_total"] == 1


def test_middleware_records_http_only():
    stats = RequestStats()
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("x")

    app.add_middleware(request_timing_middleware(stats))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/ping").status_code == 200
    assert client.get("/boom").status_code == 500
    rows = list(stats._buf)
    assert [(r.path, r.status) for r in rows] == [("/ping", 200), ("/boom", 500)]
    assert all(r.ms >= 0 for r in rows) and stats.errors == 1


def test_broker_vitals_from_the_call_log():
    log = CallLog()
    log.record("broker", "get_account", ms=80.0)
    log.record("broker", "get_orders", ms=120.0)
    log.record("broker", "submit_order", detail="422", ms=300.0, ok=False)
    log.record("data", "GET finnhub.io/x", ms=40.0)
    v = broker_vitals(log, seconds=300)
    assert v["broker"]["count"] == 3 and v["broker"]["errors"] == 1
    assert v["broker"]["slowest"][0]["name"] == "submit_order"
    assert v["broker"]["per_min"] == 0.6 and v["data"]["count"] == 1


def _plan(pid, **kw):
    base = dict(id=pid, underlying="NVDA", status="filled", qty=1, filled_qty=1, tp_premium=None,
                sl_premium=1.0, time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
                legs=[{"symbol": "NVDA260904P00230000", "side": 1, "ratio": 1, "right": "P", "strike": 230}],
                asset_class="option", tp_order_id=None, partial_exit=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_monitors_status_rows():
    async def forever():
        await asyncio.sleep(3600)

    running = asyncio.create_task(forever())

    async def dies():
        raise RuntimeError("wedged")

    dead = asyncio.create_task(dies())
    await asyncio.sleep(0)
    enforcer = SimpleNamespace(
        _monitors={"p1": running, "p2": dead}, monitor_beat={"p1": 100.0, "p2": 0.0},
        monitor_health={"p1": "ok", "p2": "no-mid: NVDA"}, _parked={"p2"}, _ghost_keys={"p3": ["k1", "k2"]},
    )
    plans = [_plan("p1"), _plan("p2"), _plan("p3", legs=[{"symbol": "AVGG", "side": 1, "ratio": 1}], asset_class="equity", underlying="AVGG")]
    rows = monitors_status(enforcer, plans, now_mono=105.0)
    by = {r["plan_id"]: r for r in rows}
    assert by["p1"]["task"] == "running" and by["p1"]["beat_age_s"] == 5.0 and by["p1"]["ok"]
    assert by["p1"]["label"] == "NVDA +1P230" and by["p1"]["time_stop_in_s"] > 7000
    assert by["p2"]["task"].startswith("DEAD") and by["p2"]["parked"] and not by["p2"]["ok"]
    assert by["p3"]["task"] == "NOT ARMED" and by["p3"]["ghost_keys"] == 2 and by["p3"]["label"] == "AVGG +1SH"
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass


def test_cache_vitals():
    market = SimpleNamespace(cache_status=lambda: [
        {"symbol": "NVDA", "bars_1m": 12000, "backfill_ms": 800},
        {"symbol": "SPY", "bars_1m": 9000, "backfill_ms": 1200},
        {"symbol": "QQQ", "bars_1m": 10, "backfill_ms": None},
    ])
    v = cache_vitals(market, SimpleNamespace(_cache={"a": 1}, _inflight={}), SimpleNamespace(_cache={"x": 1, "y": 2}))
    assert v["tickers_cached"] == 3 and v["bars_1m_total"] == 21010
    assert v["backfill_avg_ms"] == 1000 and v["backfill_max_ms"] == 1200
    assert v["contracts"] == {"cached": 1, "inflight": 0} and v["chains"] == {"cached": 2}


def test_routes(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    log = tmp_path / "engine.log"
    log.write_text("boot\nplan p1: monitor armed\nplan p2: exit\nplan p1: TP hit\n", encoding="utf-8")
    monkeypatch.setattr(admin_mod, "log_file_path", lambda: log)
    plan = _plan("p1")
    plan.to_dict = lambda: {"id": "p1"}

    class Risk:
        async def open_plans(self):
            return [plan]

    class Fsm:
        async def events_for(self, pid, limit):
            return [{"plan_id": pid, "event": "ENTRY_FILLED"}]

    class Trade:
        fsm = Fsm()

        async def get_plan(self, pid):
            return plan if pid == "p1" else None

    app.state.risk = Risk()
    app.state.trade = Trade()
    app.state.enforcer = SimpleNamespace(_monitors={}, monitor_beat={}, monitor_health={}, _parked=set(), _ghost_keys={})
    app.state.market = SimpleNamespace(cache_status=lambda: [], _stock_refs={})
    client = TestClient(app)
    v = client.get("/api/admin/vitals").json()
    assert set(v) == {"requests", "broker", "cache", "monitors", "monitors_ok"}
    assert v["monitors"][0]["task"] == "NOT ARMED" and v["monitors_ok"] == 0
    feed = client.get("/api/admin/plans/p1/feed").json()
    assert feed["plan"] == {"id": "p1"} and feed["events"][0]["event"] == "ENTRY_FILLED"
    assert feed["log"] == ["plan p1: monitor armed", "plan p1: TP hit"]
    assert client.get("/api/admin/plans/nope/feed").status_code == 404
