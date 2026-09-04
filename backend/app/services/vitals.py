"""Vitals for the administration window: what the server is doing, in
numbers a glance can read.

- REQUESTS: every HTTP request's latency and status in one ring (an ASGI
  middleware records; the admin's own polling is tagged so it can be
  excluded from the averages it is looking at).
- BROKER: latency and rate of the Alpaca REST calls, from the call log.
- CACHE: which tickers hold bars, how many, how long the backfill took;
  the contract / chain caches' sizes.
- MONITORS: every exit monitor the enforcer runs, one row each, with what
  it is guarding and whether it is alive — the "automations on live".

Module-level ring for requests (the logging-module precedent, as
services/call_log.py): the middleware must reach it without a handle.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

MAX_REQUESTS = 2_000
ADMIN_PREFIXES = ("/api/admin", "/api/monitor")


@dataclass(slots=True)
class RequestRecord:
    ts: float
    method: str
    path: str
    status: int
    ms: float
    admin: bool


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(int(round(q * (len(sorted_vals) - 1))), len(sorted_vals) - 1)
    return sorted_vals[idx]


def latency_summary(ms: list[float]) -> dict:
    """count / avg / p50 / p95 / max of a latency sample."""
    if not ms:
        return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    s = sorted(ms)
    return {
        "count": len(s),
        "avg_ms": round(sum(s) / len(s), 1),
        "p50_ms": round(_percentile(s, 0.5) or 0, 1),
        "p95_ms": round(_percentile(s, 0.95) or 0, 1),
        "max_ms": round(s[-1], 1),
    }


class RequestStats:
    def __init__(self, maxlen: int = MAX_REQUESTS):
        self._buf: deque[RequestRecord] = deque(maxlen=maxlen)
        self.total = 0
        self.errors = 0

    def record(self, method: str, path: str, status: int, ms: float, now: float | None = None) -> None:
        try:
            self._buf.append(RequestRecord(
                ts=now if now is not None else time.time(),
                method=method, path=path[:120], status=int(status), ms=float(ms),
                admin=path.startswith(ADMIN_PREFIXES),
            ))
            self.total += 1
            if status >= 500:
                self.errors += 1
        except Exception:
            pass  # observability must never break the observed

    def window(self, seconds: float, now: float | None = None, include_admin: bool = False) -> dict:
        """Latency + throughput over the trailing window, by default without
        the admin window's own polling (which would otherwise dominate)."""
        t = now if now is not None else time.time()
        cutoff = t - seconds
        rows = [r for r in self._buf if r.ts >= cutoff and (include_admin or not r.admin)]
        out = latency_summary([r.ms for r in rows])
        out["window_s"] = seconds
        out["rps"] = round(len(rows) / seconds, 2) if seconds > 0 else None
        out["errors_5xx"] = sum(1 for r in rows if r.status >= 500)
        out["errors_4xx"] = sum(1 for r in rows if 400 <= r.status < 500)
        return out

    def top_routes(self, seconds: float, limit: int = 8, now: float | None = None) -> list[dict]:
        t = now if now is not None else time.time()
        cutoff = t - seconds
        by: dict[str, list[float]] = {}
        for r in self._buf:
            if r.ts < cutoff or r.admin:
                continue
            by.setdefault(f"{r.method} {r.path}", []).append(r.ms)
        rows = [
            {"route": k, "count": len(v), "avg_ms": round(sum(v) / len(v), 1), "max_ms": round(max(v), 1)}
            for k, v in by.items()
        ]
        rows.sort(key=lambda r: (-r["count"], r["route"]))
        return rows[:limit]

    def summary(self, now: float | None = None) -> dict:
        return {
            "total": self.total,
            "errors_5xx_total": self.errors,
            "last_60s": self.window(60, now),
            "last_5m": self.window(300, now),
            "admin_last_60s": self.window(60, now, include_admin=True),
            "top_routes_5m": self.top_routes(300, now=now),
        }


REQUESTS = RequestStats()


def request_timing_middleware(stats: RequestStats | None = None):
    """Pure-ASGI middleware factory: wraps the app, records every HTTP
    request's status and latency. WebSocket and lifespan scopes pass
    through untouched."""
    sink = stats or REQUESTS

    def wrap(app):
        async def timed(scope, receive, send):
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return
            started = time.perf_counter()
            status = {"code": 500}

            async def send_wrapper(message):
                if message.get("type") == "http.response.start":
                    status["code"] = int(message.get("status", 500))
                await send(message)

            try:
                await app(scope, receive, send_wrapper)
            finally:
                sink.record(
                    scope.get("method", "?"), scope.get("path", "?"),
                    status["code"], (time.perf_counter() - started) * 1000,
                )

        return timed

    return wrap


def broker_vitals(call_log, seconds: float = 300.0, now: float | None = None) -> dict:
    """Latency and rate of the broker REST calls over the trailing window,
    plus the same for the data sources — from the call log ring."""
    t = now if now is not None else time.time()
    cutoff = t - seconds
    out: dict = {"window_s": seconds}
    for category in ("broker", "data"):
        rows = [r for r in call_log.recent(category, 600) if r["ts"] >= cutoff]
        ms = [r["ms"] for r in rows if r["ms"] is not None]
        summary = latency_summary(ms)
        summary["per_min"] = round(len(rows) / (seconds / 60), 1) if seconds > 0 else None
        summary["errors"] = sum(1 for r in rows if not r["ok"])
        slow = sorted((r for r in rows if r["ms"] is not None), key=lambda r: -r["ms"])[:3]
        summary["slowest"] = [{"name": r["name"], "ms": r["ms"]} for r in slow]
        out[category] = summary
    return out


def monitors_status(enforcer, plans: list, now_mono: float | None = None, now: float | None = None) -> list[dict]:
    """One row per OPEN plan: the exit monitor guarding it (or the lack of
    one), its heartbeat age, health, parked / ghost state, and the rules it
    is holding. The enforcer's live attributes are read, never mutated."""
    t_mono = now_mono if now_mono is not None else time.monotonic()
    t = now if now is not None else time.time()
    monitors = getattr(enforcer, "_monitors", {}) or {}
    beats = getattr(enforcer, "monitor_beat", {}) or {}
    health = getattr(enforcer, "monitor_health", {}) or {}
    parked = getattr(enforcer, "_parked", set()) or set()
    ghosts = getattr(enforcer, "_ghost_keys", {}) or {}
    rows: list[dict] = []
    for plan in plans:
        pid = plan.id
        task = monitors.get(pid)
        if task is None:
            task_state = "NOT ARMED"
        elif task.cancelled():
            task_state = "cancelled"
        elif task.done():
            exc = task.exception()
            task_state = f"DEAD ({exc})" if exc else "finished"
        else:
            task_state = "running"
        beat = beats.get(pid)
        beat_age = round(t_mono - beat, 1) if beat is not None else None
        stop = getattr(plan, "time_stop_utc", None)
        stop_in = None
        if stop is not None:
            from app.models.trade import as_utc

            stop_in = round(as_utc(stop).timestamp() - t)
        legs = getattr(plan, "legs", None) or []
        label = f"{plan.underlying} " + " ".join(
            f"{'+' if (l.get('side', 1) or 1) > 0 else '−'}{l.get('ratio', 1) or 1}{l.get('right') or 'SH'}{l.get('strike') or ''}"
            for l in legs
        ) if legs else plan.underlying
        rows.append({
            "plan_id": pid,
            "label": label.strip(),
            "asset_class": getattr(plan, "asset_class", None),
            "status": plan.status,
            "qty": getattr(plan, "filled_qty", None) or plan.qty,
            "task": task_state,
            "beat_age_s": beat_age,
            "health": health.get(pid, "—" if task is None else "ok"),
            "parked": pid in parked,
            "ghost_keys": len(ghosts.get(pid, [])),
            "tp": plan.tp_premium,
            "sl": plan.sl_premium,
            "time_stop_utc": as_utc(stop).isoformat() if stop is not None else None,
            "time_stop_in_s": stop_in,
            "tp_resting": bool(getattr(plan, "tp_order_id", None)),
            "partial": getattr(plan, "partial_exit", None),
            "ok": task_state == "running" and not (health.get(pid, "") or "").startswith("no-mid")
                  and (beat_age is None or beat_age < 90),
        })
    return rows


def cache_vitals(market, contracts=None, chains=None) -> dict:
    """Tickers held in the bar cache with their sizes and load times, plus
    the sizes of the contract and chain caches."""
    out: dict = {"tickers": [], "contracts": None, "chains": None}
    try:
        out["tickers"] = market.cache_status()
    except Exception as exc:  # noqa: BLE001
        out["tickers_error"] = str(exc)[:120]
    if contracts is not None:
        out["contracts"] = {"cached": len(getattr(contracts, "_cache", {}) or {}),
                            "inflight": len(getattr(contracts, "_inflight", {}) or {})}
    if chains is not None:
        out["chains"] = {"cached": len(getattr(chains, "_cache", {}) or {})}
    loads = [t["backfill_ms"] for t in out["tickers"] if t.get("backfill_ms") is not None]
    out["tickers_cached"] = len(out["tickers"])
    out["bars_1m_total"] = sum(t.get("bars_1m", 0) for t in out["tickers"])
    out["backfill_avg_ms"] = round(sum(loads) / len(loads), 0) if loads else None
    out["backfill_max_ms"] = round(max(loads), 0) if loads else None
    return out
