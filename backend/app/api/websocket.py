"""Multiplexed client WebSocket: snapshot-then-stream protocol.

Client -> server:
    {"op": "subscribe",   "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    {"op": "unsubscribe", "channel": "bars",    "symbol": "SPY", "tf": "1m"}
    {"op": "subscribe",   "channel": "quote",   "symbol": "SPY"}

Server -> client: on subscribe, the full current state (snapshot), then deltas.
Reconnect is therefore always safe — no client-side gap logic needed.
A status frame is pushed every 5s so the UI can show STALE/DISCONNECTED.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.broadcast import QUEUE_SIZE

log = logging.getLogger("app.ws")

router = APIRouter()


@router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    app = ws.app
    market = app.state.market
    broadcaster = app.state.broadcaster
    await ws.accept()

    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    topics: set[str] = set()
    stock_subs: set[str] = set()

    async def pump() -> None:
        while True:
            msg = await queue.get()
            await ws.send_json(msg)

    async def heartbeat() -> None:
        while True:
            try:
                await ws.send_json(market.status())
            except Exception:
                return  # socket gone; the receive loop is tearing down
            await asyncio.sleep(5)

    pump_task = asyncio.create_task(pump())
    hb_task = asyncio.create_task(heartbeat())

    def join(topic: str) -> None:
        if topic not in topics:
            topics.add(topic)
            broadcaster.subscribe(topic, queue)

    def leave(topic: str) -> None:
        if topic in topics:
            topics.discard(topic)
            broadcaster.unsubscribe(topic, queue)

    async def handle(msg: dict) -> None:
        op, channel = msg.get("op"), msg.get("channel")

        if channel == "bars":
            symbol = (msg.get("symbol") or "").upper()
            tf = msg.get("tf") or "1m"
            if not symbol:
                return
            topic = f"bars:{symbol}:{tf}"
            if op == "subscribe":
                join(topic)
                if symbol not in stock_subs:
                    stock_subs.add(symbol)
                    await market.subscribe_stock(symbol)
                # Cached snapshot immediately; backfill (running in the
                # background) pushes a fresh bars_snapshot when it lands.
                await ws.send_json(
                    {"t": "bars_snapshot", "symbol": symbol, "tf": tf,
                     "bars": market.bars.get_bars(symbol, tf)}
                )
            elif op == "unsubscribe":
                leave(topic)

        elif channel == "quote":
            symbol = (msg.get("symbol") or "").upper()
            if not symbol:
                return
            topic = f"quote:{symbol}"
            if op == "subscribe":
                join(topic)
                quote = market.latest_quote(symbol) or await market.fetch_latest_stock_quote(symbol)
                if quote:
                    await ws.send_json(quote)
            elif op == "unsubscribe":
                leave(topic)

        elif channel == "plans":
            if op == "subscribe":
                join("plans")
                plans = await app.state.risk.open_plans()
                await ws.send_json(
                    {"t": "plans_snapshot", "plans": [p.to_dict() for p in plans]}
                )
            elif op == "unsubscribe":
                leave("plans")

    try:
        while True:
            # One malformed frame must not kill the connection — the client
            # would reconnect with backoff and the feed looks "spotty".
            try:
                msg = await ws.receive_json()
            except (ValueError, TypeError) as exc:
                log.warning("ws bad frame ignored: %s", exc)
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await handle(msg)
            except (WebSocketDisconnect, RuntimeError):
                raise
            except Exception:
                log.exception("ws message handling failed (op=%s channel=%s)",
                              msg.get("op"), msg.get("channel"))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws error: %s", exc)
    finally:
        pump_task.cancel()
        hb_task.cancel()
        for topic in topics:
            broadcaster.unsubscribe(topic, queue)
        for symbol in stock_subs:
            await market.unsubscribe_stock(symbol)
