"""Rolling call log for the monitor page: who is talking to whom, live.

Three categories, one ring buffer:
- "data"   — outbound HTTP to market-data sources (EDGAR, Finnhub, ...)
- "broker" — every Alpaca REST call (all of them route through
  AlpacaService.call; websocket traffic is counted by the feeds' own
  status(), not here)
- "engine" — internal chokepoints: journal writes, bus publishes,
  strategy decisions, intent submissions

Module-level singleton on purpose (the logging-module precedent):
observability must be reachable from feeds, services, and the runner
without threading a handle through every constructor. Tests build private
instances. Recording never raises and costs ~a dict append; the buffer is
bounded so it cannot grow without limit.
"""

import time
from collections import Counter, deque
from dataclasses import dataclass

MAX_RECORDS = 600


@dataclass(slots=True)
class CallRecord:
    ts: float             # epoch seconds
    category: str         # data | broker | engine
    name: str             # e.g. "GET data.sec.gov/cgi-bin/browse-edgar"
    detail: str
    ms: float | None
    ok: bool

    def to_dict(self) -> dict:
        return {"ts": self.ts, "category": self.category, "name": self.name,
                "detail": self.detail, "ms": self.ms, "ok": self.ok}


class CallLog:
    def __init__(self, maxlen: int = MAX_RECORDS):
        self._buf: deque[CallRecord] = deque(maxlen=maxlen)
        self.counts: Counter = Counter()
        self.errors: Counter = Counter()

    def record(self, category: str, name: str, detail: str = "",
               ms: float | None = None, ok: bool = True) -> None:
        try:
            self._buf.append(CallRecord(
                ts=time.time(), category=category, name=str(name)[:120],
                detail=str(detail)[:200],
                ms=round(ms, 1) if ms is not None else None, ok=ok,
            ))
            self.counts[category] += 1
            if not ok:
                self.errors[category] += 1
        except Exception:
            pass  # observability must never break the observed

    def recent(self, category: str | None = None, limit: int = 100) -> list[dict]:
        out = []
        for rec in reversed(self._buf):
            if category and rec.category != category:
                continue
            out.append(rec.to_dict())
            if len(out) >= min(limit, MAX_RECORDS):
                break
        return out

    def status(self) -> dict:
        return {
            "counts": dict(self.counts),
            "errors": dict(self.errors),
            "buffered": len(self._buf),
        }


# The process-wide instance every instrumentation point records into.
CALL_LOG = CallLog()


def http_hooks(category: str, log: CallLog | None = None) -> dict:
    """httpx event_hooks that record every request/response on a client —
    how the data-source feeds get instrumented without touching their
    request code. Usage: httpx.AsyncClient(event_hooks=http_hooks('data'))."""
    sink = log or CALL_LOG

    async def on_request(request) -> None:
        request.extensions["call_log_t0"] = time.monotonic()

    async def on_response(response) -> None:
        t0 = response.request.extensions.get("call_log_t0")
        ms = (time.monotonic() - t0) * 1000 if t0 else None
        url = response.request.url
        sink.record(
            category,
            f"{response.request.method} {url.host}{url.path}",
            detail=f"HTTP {response.status_code}",
            ms=ms,
            ok=response.status_code < 400,
        )

    return {"request": [on_request], "response": [on_response]}
