"""The browser WebSocket's option-quote channel: snapshot on subscribe (cached
or REST-refreshed), streamed deltas, the per-socket cap, and reference
release on unsubscribe and on disconnect."""

from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.websocket import OQUOTE_MAX_PER_SOCKET, router
from app.services.broadcast import Broadcaster

SYM = "SPY260904C00650000"


class FakeMarket:
    def __init__(self, broadcaster: Broadcaster):
        self.broadcast = broadcaster
        self.quotes: dict[str, dict] = {}
        self.option_refs: dict[str, int] = {}
        self.refreshed: list[list[str]] = []
        self.bars = SimpleNamespace(get_bars=lambda *a, **k: [])

    def status(self):
        return {"t": "status", "configured": True, "stream_age_s": 1.0}

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)

    async def subscribe_options(self, symbols):
        for s in symbols:
            self.option_refs[s] = self.option_refs.get(s, 0) + 1

    async def unsubscribe_options(self, symbols):
        for s in symbols:
            self.option_refs[s] = self.option_refs.get(s, 0) - 1

    async def refresh_option_quotes(self, symbols, max_age_s=30.0):
        self.refreshed.append(list(symbols))
        for s in symbols:
            self.quotes[s] = {"bid": 1.40, "ask": 1.44, "mid": 1.42, "ts": 1}

    async def subscribe_stock(self, symbol):
        pass

    async def unsubscribe_stock(self, symbol):
        pass

    async def subscribe_fast(self, symbol):
        pass

    async def unsubscribe_fast(self, symbol):
        pass

    def get_bars(self, symbol, tf):
        return []

    async def fetch_latest_stock_quote(self, symbol):
        return None


async def _no_plans():
    return []


def make_app():
    app = FastAPI()
    app.include_router(router)
    broadcaster = Broadcaster()
    market = FakeMarket(broadcaster)
    app.state.market = market
    app.state.broadcaster = broadcaster
    app.state.risk = SimpleNamespace(open_plans=_no_plans)
    return app, market, broadcaster


def _recv_until(ws, kind: str, tries: int = 10) -> dict:
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("t") == kind:
            return msg
    raise AssertionError(f"no {kind} frame")


def test_oquote_snapshot_via_rest_then_stream_then_release():
    app, market, broadcaster = make_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "oquote", "symbol": SYM.lower()})
            snap = _recv_until(ws, "oquote")
            assert snap["symbol"] == SYM and snap["mid"] == 1.42
            assert market.refreshed == [[SYM]]  # nothing cached: REST seeded it
            assert market.option_refs[SYM] == 1

            broadcaster.publish(f"oquote:{SYM}", {"t": "oquote", "symbol": SYM,
                                                  "bid": 1.41, "ask": 1.45, "mid": 1.43, "ts": 2})
            delta = _recv_until(ws, "oquote")
            assert delta["mid"] == 1.43

            ws.send_json({"op": "unsubscribe", "channel": "oquote", "symbol": SYM})
            # A follow-up round trip proves the unsubscribe was processed.
            ws.send_json({"op": "subscribe", "channel": "plans"})
            _recv_until(ws, "plans_snapshot")
            assert market.option_refs[SYM] == 0


def test_oquote_cap_and_disconnect_release():
    app, market, _ = make_app()
    syms = [f"SPY260904C00{650 + i:03d}000" for i in range(OQUOTE_MAX_PER_SOCKET + 1)]
    for s in syms:
        market.quotes[s] = {"bid": 1.0, "ask": 1.1, "mid": 1.05, "ts": 1}
    with TestClient(app) as client:
        with client.websocket_connect("/ws/stream") as ws:
            for s in syms[:-1]:
                ws.send_json({"op": "subscribe", "channel": "oquote", "symbol": s})
                _recv_until(ws, "oquote")
            ws.send_json({"op": "subscribe", "channel": "oquote", "symbol": syms[-1]})
            err = _recv_until(ws, "error")
            assert "per socket" in err["error"]
            assert syms[-1] not in market.option_refs
            assert market.refreshed == []  # cached quotes never hit REST
    # Disconnect released every reference this socket held.
    assert all(market.option_refs[s] == 0 for s in syms[:-1])
