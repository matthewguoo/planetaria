"""The data streams must never burn a core: no busy-wait while nothing is
subscribed, and capped backoff when the connection is REFUSED at auth
(the free plan's 'connection limit exceeded' while another process holds
the account's one websocket). Measured 2026-09-03 on the live box before
this fix: 100% CPU for ten hours."""

import asyncio

import pytest

from app.services import alpaca as alpaca_mod
from app.services.alpaca import PatientStockDataStream


def _stream() -> PatientStockDataStream:
    return PatientStockDataStream("PKTEST", "secret")


async def _handler(_quote) -> None:  # the SDK insists on a coroutine handler
    pass


class _Sleeps:
    """Record asyncio.sleep calls and end the test after N of them."""

    def __init__(self, stop_after: int):
        self.calls: list[float] = []
        self.stop_after = stop_after

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        if len(self.calls) >= self.stop_after:
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_idle_stream_sleeps_instead_of_spinning(monkeypatch):
    s = _stream()
    rec = _Sleeps(stop_after=3)
    monkeypatch.setattr(alpaca_mod.asyncio, "sleep", rec)
    with pytest.raises(asyncio.CancelledError):
        await s._run_forever()
    # Every idle iteration waited a real interval, never sleep(0).
    assert rec.calls == [alpaca_mod.IDLE_POLL_S] * 3


@pytest.mark.asyncio
async def test_refused_connection_backs_off_exponentially(monkeypatch):
    s = _stream()
    s.subscribe_quotes(_handler, "SPY")  # a handler: the idle wait is over

    async def refused():
        raise ValueError("connection limit exceeded")

    closes = []

    async def close():
        closes.append(1)

    monkeypatch.setattr(s, "_start_ws", refused)
    monkeypatch.setattr(s, "close", close)
    rec = _Sleeps(stop_after=8)
    monkeypatch.setattr(alpaca_mod.asyncio, "sleep", rec)
    with pytest.raises(asyncio.CancelledError):
        await s._run_forever()
    # Refusal sleeps: 2, 4, 8, 16 ... interleaved with the loop's sleep(0).
    backoffs = [d for d in rec.calls if d > 0]
    assert backoffs == [2.0, 4.0, 8.0, 16.0]
    assert len(closes) == 4              # the socket is closed before each retry
    assert all(d <= alpaca_mod.REFUSAL_BACKOFF_MAX_S for d in backoffs)


@pytest.mark.asyncio
async def test_insufficient_subscription_still_bails_out(monkeypatch):
    s = _stream()
    s.subscribe_quotes(_handler, "SPY")

    async def refused():
        raise ValueError("insufficient subscription")

    async def close():
        pass

    monkeypatch.setattr(s, "_start_ws", refused)
    monkeypatch.setattr(s, "close", close)
    await asyncio.wait_for(s._run_forever(), timeout=2)   # returns, no retry loop
