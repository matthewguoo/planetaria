"""System introspection + runtime feed settings.

Feed settings are the knobs for how hard the app talks to its data
sources — poll cadences and Alpaca feed tiers — persisted in app_settings
and applied live where the consumer re-reads them (frontend poll loops,
the keyless public feed). Feed-tier changes need a restart (streams are
constructed once) and are flagged as such.

The system-state snapshot is the ops view: every subsystem's health in one
payload for the UI's SYSTEM menu.
"""

import logging
import time

from app.models.trade import AppSetting

log = logging.getLogger("app.system")

FEED_KEY = "feed"

DEFAULT_FEED: dict = {
    "chain_refresh_s": 10,     # frontend options-chain reload cadence
    "positions_poll_s": 5,     # frontend positions poll
    "account_poll_s": 30,      # frontend account poll
    "public_poll_s": 5,        # keyless public feed quote poll (live-applied)
    "stock_feed": "iex",       # iex | sip (needs Alpaca data sub; restart)
    "option_feed": "indicative",  # indicative | opra (restart)
}

# Settings that only take effect after a backend restart.
RESTART_REQUIRED = {"stock_feed", "option_feed"}

_RANGES = {
    "chain_refresh_s": (2, 120),
    "positions_poll_s": (2, 60),
    "account_poll_s": (5, 300),
    "public_poll_s": (2, 60),
}
_ENUMS = {
    "stock_feed": {"iex", "sip"},
    "option_feed": {"indicative", "opra"},
}


class FeedSettingsService:
    def __init__(self, db):
        self.db = db

    async def get(self) -> dict:
        async with self.db.session() as session:
            row = await session.get(AppSetting, FEED_KEY)
            merged = dict(DEFAULT_FEED)
            if row:
                merged.update(row.value)
            merged["restart_required_keys"] = sorted(RESTART_REQUIRED)
            return merged

    async def update(self, patch: dict) -> dict:
        clean: dict = {}
        for key, value in patch.items():
            if key not in DEFAULT_FEED:
                raise ValueError(f"unknown feed setting {key!r}")
            if key in _RANGES:
                lo, hi = _RANGES[key]
                value = float(value)
                if not (lo <= value <= hi):
                    raise ValueError(f"{key} must be between {lo} and {hi}")
                clean[key] = value
            elif key in _ENUMS:
                if value not in _ENUMS[key]:
                    raise ValueError(f"{key} must be one of {sorted(_ENUMS[key])}")
                clean[key] = value
        if not clean:
            raise ValueError("nothing to update")
        async with self.db.session() as session:
            row = await session.get(AppSetting, FEED_KEY)
            if row is None:
                session.add(AppSetting(key=FEED_KEY, value=clean))
            else:
                row.value = {**row.value, **clean}
            await session.commit()
        return await self.get()


def _task_state(task) -> str:
    if task is None:
        return "not started"
    if task.cancelled():
        return "cancelled"
    if task.done():
        exc = task.exception() if not task.cancelled() else None
        return f"DEAD ({exc})" if exc else "finished"
    return "running"


async def system_state(app_state) -> dict:
    """One payload with every subsystem's health for the SYSTEM menu."""
    market = app_state.market
    trade = app_state.trade
    enforcer = app_state.enforcer
    alpaca = app_state.alpaca
    db = app_state.db
    redis = app_state.redis

    # DB liveness + latency (a stalled DB stalls order management).
    db_ok, db_ms = False, None
    try:
        from sqlalchemy import text

        t0 = time.perf_counter()
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
        db_ms = round((time.perf_counter() - t0) * 1000, 1)
        db_ok = True
    except Exception as exc:
        log.warning("db health probe failed: %s", exc)

    account_status = "NO_KEYS"
    if alpaca.configured:
        try:
            account_status = (await trade.get_account())["status"]
        except Exception as exc:
            account_status = f"UNREACHABLE ({exc})"

    feed_status = market.status()
    reconcile_age = (
        round(time.time() - enforcer.last_reconcile_ts, 1)
        if enforcer.last_reconcile_ts
        else None
    )

    return {
        "t": "system",
        "asof": int(time.time() * 1000),
        "feed": {
            "configured": feed_status["configured"],
            "demo": feed_status["demo"],
            "sources": feed_status.get("sources", {}),
            "stream_age_s": feed_status["stream_age_s"],
            "stock_symbols": feed_status["stock_symbols"],
            "option_symbols": feed_status["option_symbols"],
        },
        "broker": {
            "configured": alpaca.configured,
            "paper": True,
            "account_status": account_status,
        },
        "db": {"ok": db_ok, "latency_ms": db_ms,
               "engine": "postgres" if db.url.startswith("postgres") else "sqlite"},
        "redis": {"ok": redis.healthy},
        "enforcer": {
            "monitors": len(enforcer._monitors),
            "monitored_plan_ids": sorted(enforcer._monitors.keys())[:20],
            "ghost_keys": sum(len(v) for v in enforcer._ghost_keys.values()),
            "last_reconcile_age_s": reconcile_age,
            # Monitors that currently CANNOT evaluate TP/SL (no quote for
            # some leg even after REST polling) — must be zero in health.
            "monitors_without_mid": {
                pid: status
                for pid, status in enforcer.monitor_health.items()
                if status != "ok"
            },
        },
        "tasks": {
            "trading_stream": _task_state(getattr(app_state, "trading_stream_task", None)),
            "reconcile_loop": _task_state(getattr(app_state, "reconcile_loop_task", None)),
            "startup_reconcile": _task_state(getattr(app_state, "reconcile_task", None)),
        },
    }
