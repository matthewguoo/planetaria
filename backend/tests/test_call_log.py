"""CallLog ring + the three instrumentation paths (data / broker / engine)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

import app.services.call_log as call_log_mod
from app.services.call_log import CallLog, http_hooks
from app.services.signals.events import Event, EventBus


def test_ring_bounded_counts_and_filters():
    log = CallLog(maxlen=5)
    for i in range(8):
        log.record("engine", f"n{i}", ok=(i % 2 == 0))
    log.record("broker", "submit_order", ms=12.34)

    assert log.status()["buffered"] == 5  # ring dropped the oldest
    assert log.status()["counts"] == {"engine": 8, "broker": 1}
    assert log.status()["errors"] == {"engine": 4}
    broker = log.recent("broker")
    assert broker[0]["name"] == "submit_order" and broker[0]["ms"] == 12.3
    assert log.recent(limit=2).__len__() == 2
    assert log.recent()[0]["name"] == "submit_order"  # newest first


@pytest.mark.asyncio
async def test_http_hooks_record_data_calls():
    log = CallLog()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if "bad" in str(request.url) else 200,
                              text="x")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 event_hooks=http_hooks("data", log)) as http:
        await http.get("https://www.sec.gov/cgi-bin/browse-edgar?x=1")
        await http.get("https://finnhub.io/bad")

    calls = log.recent("data")
    assert len(calls) == 2
    assert calls[1]["name"] == "GET www.sec.gov/cgi-bin/browse-edgar"
    assert calls[1]["ok"] is True and calls[1]["ms"] is not None
    assert calls[0]["detail"] == "HTTP 500" and calls[0]["ok"] is False


@pytest.mark.asyncio
async def test_alpaca_call_records_broker_success_and_failure(monkeypatch):
    fresh = CallLog()
    monkeypatch.setattr(call_log_mod, "CALL_LOG", fresh)
    from app.config import Settings
    from app.services.alpaca import AlpacaService

    alpaca = AlpacaService(Settings(alpaca_api_key="", alpaca_secret_key=""))

    def get_clock():
        return "clock"

    def explode():
        raise RuntimeError("broker down")

    assert await alpaca.call(get_clock) == "clock"
    with pytest.raises(RuntimeError):
        await alpaca.call(explode)

    calls = fresh.recent("broker")
    assert [c["name"] for c in calls] == ["explode", "get_clock"]
    assert calls[1]["ok"] is True and calls[0]["ok"] is False
    assert "broker down" in calls[0]["detail"]


def test_bus_publish_records_engine_but_not_timer(monkeypatch):
    fresh = CallLog()
    monkeypatch.setattr(call_log_mod, "CALL_LOG", fresh)
    bus = EventBus()
    bus.subscribe("news")
    bus.subscribe("timer")

    def ev(type_: str) -> Event:
        return Event(type=type_, ts=datetime.now(timezone.utc), source="t",
                     key=None, symbols=("AMD",), payload={})

    bus.publish(ev("news"))
    bus.publish(ev("timer"))
    calls = fresh.recent("engine")
    assert len(calls) == 1
    assert calls[0]["name"] == "bus:news" and "AMD" in calls[0]["detail"]


@pytest.mark.asyncio
async def test_monitor_route_shape(monkeypatch):
    fresh = CallLog()
    monkeypatch.setattr(call_log_mod, "CALL_LOG", fresh)
    import app.api.routes.admin as monitor_mod

    monkeypatch.setattr(monitor_mod, "CALL_LOG", fresh)
    fresh.record("data", "GET x", ms=1.0)
    out = await monitor_mod.calls(category="data", limit=10)
    assert out["status"]["counts"] == {"data": 1}
    assert out["calls"][0]["name"] == "GET x"
    assert SimpleNamespace(**out["calls"][0]).category == "data"
