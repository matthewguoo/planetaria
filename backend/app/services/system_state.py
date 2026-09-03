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
    "stock_feed": "iex",       # iex | sip (needs Alpaca data sub; restart)
    "option_feed": "indicative",  # indicative | opra (restart)
}

# Settings that only take effect after a backend restart.
RESTART_REQUIRED = {"stock_feed", "option_feed"}

_RANGES = {
    "chain_refresh_s": (2, 120),
    "positions_poll_s": (2, 60),
    "account_poll_s": (5, 300),
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


ACCOUNT_KEY = "account"
ACCOUNT_ENV_RE = r"^ALPACA_ACCOUNT_(.+)_API_KEY$"


class AccountService:
    """Named Alpaca PAPER accounts: .env-held keys, DB-held selection.

    Keys never touch the DB (standing rule: secrets live only in the
    gitignored .env). Extra accounts register by naming convention:

        ALPACA_ACCOUNT_<NAME>_API_KEY=PK...
        ALPACA_ACCOUNT_<NAME>_SECRET_KEY=...

    plus the legacy unnamed pair as 'default'. TWO structural locks:
    only PK-prefixed (paper) keys may enter the pool — a live key is
    refused at registration, so account switching can never defeat the
    paper lock — and selection is refused while ANY plan is open (a
    switch would strand the old account's plans outside enforcement).
    Selection persists in app_settings and applies at BOOT (streams and
    clients are constructed once); the UI shows restart_required until
    the restart happens.

    TRADING_MODE=live_manual inverts the pool gate: ONLY live_*-named
    AK-key pairs are admitted, the account is pinned by LIVE_ACCOUNT_NAME
    (no DB selection, no fallback — a miss kills the boot), and select()
    is refused at runtime."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.applied_name: str | None = None

    def _env_sources(self) -> dict[str, str]:
        """Process env over the same .env files config.py reads. A method
        so tests can stub it — reading the REAL .env in a test run leaks
        actual keys into assertion output."""
        import os

        from dotenv import dotenv_values

        from app.config import _ENV_FILES

        merged: dict[str, str] = {}
        for env_file in reversed(_ENV_FILES):
            try:
                merged.update({k: v for k, v in dotenv_values(env_file).items()
                               if v is not None})
            except Exception:
                pass
        merged.update(os.environ)
        return merged

    def registry(self) -> dict[str, dict]:
        """name -> {api_key, secret_key}. First source wins per name."""
        import re

        merged = self._env_sources()

        out: dict[str, dict] = {}
        # Default pair from the ENV SOURCES, not live settings: apply()
        # mutates settings to the selected account's keys, which would make
        # 'default' silently mirror whatever is active.
        default_key = merged.get("ALPACA_API_KEY", "") or \
            getattr(self.settings, "alpaca_api_key", "")
        default_secret = merged.get("ALPACA_SECRET_KEY", "") or \
            getattr(self.settings, "alpaca_secret_key", "")
        if default_key:
            out["default"] = {"api_key": default_key,
                              "secret_key": default_secret}
        for var, value in merged.items():
            m = re.match(ACCOUNT_ENV_RE, var)
            if not m or not value:
                continue
            name = m.group(1).lower()
            secret = merged.get(f"ALPACA_ACCOUNT_{m.group(1)}_SECRET_KEY", "")
            if not secret:
                log.error("account %r has an API key but no secret - skipped", name)
                continue
            out[name] = {"api_key": value, "secret_key": secret}
        if getattr(self.settings, "trading_mode", "paper") == "live_manual":
            # The live server's inverse gate: ONLY live_-named accounts with
            # AK-prefixed (live) keys. 'default' and every paper pair are
            # dropped, so the live process can never quietly run on paper
            # keys and fake confidence — nor can a PK pair reach the live
            # endpoint (Alpaca would 401 it anyway).
            for name in list(out):
                if not name.startswith("live_") or \
                        not out[name]["api_key"].startswith("AK"):
                    # Expected on the live server (the shared .env carries
                    # paper pairs); not an error, so debug — the ERROR level
                    # is reserved for the paper gate catching a live key.
                    log.debug("account %r not admitted to the LIVE pool "
                              "(need live_* name + AK... key)", name)
                    del out[name]
            return out
        # The paper gate: PK-prefixed keys only, no exceptions.
        for name in list(out):
            if not out[name]["api_key"].startswith("PK"):
                log.error("account %r key does not look like a PAPER key "
                          "(PK...) - refused from the pool", name)
                del out[name]
        return out

    async def selected_name(self) -> str:
        async with self.db.session() as session:
            row = await session.get(AppSetting, ACCOUNT_KEY)
        return (row.value or {}).get("selected", "default") if row else "default"

    async def apply(self) -> str:
        """Boot-time: point settings at the selected account's keys BEFORE
        any client is constructed. Falls back loudly to 'default' — except
        in live_manual mode, where the account is env-pinned and a miss is
        fatal (a fallback would leave paper keys active with paper=False)."""
        if getattr(self.settings, "trading_mode", "paper") == "live_manual":
            name = self.settings.live_account_name
            reg = self.registry()
            if name not in reg:
                raise RuntimeError(
                    f"live account {name!r} has no live keys in .env "
                    f"(need ALPACA_ACCOUNT_{name.upper()}_API_KEY=AK... "
                    "+ _SECRET_KEY) - refusing to boot the live server")
            self.settings.alpaca_api_key = reg[name]["api_key"]
            self.settings.alpaca_secret_key = reg[name]["secret_key"]
            self.applied_name = name
            self.settings.alpaca_account_name = name
            log.info("LIVE alpaca account %r pinned by env (key ...%s)",
                     name, reg[name]["api_key"][-4:])
            return name
        name = await self.selected_name()
        reg = self.registry()
        if name not in reg:
            if name != "default":
                log.error("selected account %r has no keys in .env - "
                          "falling back to default", name)
            name = "default"
        if name in reg:
            self.settings.alpaca_api_key = reg[name]["api_key"]
            self.settings.alpaca_secret_key = reg[name]["secret_key"]
            log.info("alpaca account %r selected (key ...%s)",
                     name, reg[name]["api_key"][-4:])
        self.applied_name = name
        # Rides on settings so TradeService can stamp plans without a new
        # dependency edge.
        self.settings.alpaca_account_name = name
        return name

    async def list_accounts(self) -> dict:
        if getattr(self.settings, "trading_mode", "paper") == "live_manual":
            # Env-pinned: the DB selection does not exist on the live server.
            selected = self.applied_name or self.settings.live_account_name
        else:
            selected = await self.selected_name()
        return {
            "accounts": [
                {"name": name, "key_masked": f"...{cfg['api_key'][-4:]}",
                 "active": name == self.applied_name,
                 "selected": name == selected}
                for name, cfg in sorted(self.registry().items())
            ],
            "selected": selected,
            "applied": self.applied_name,
            "restart_required": selected != self.applied_name,
            "mode": getattr(self.settings, "trading_mode", "paper"),
            "paper_only": getattr(self.settings, "trading_mode",
                                  "paper") == "paper",
        }

    async def select(self, name: str, open_plans: int) -> dict:
        if getattr(self.settings, "trading_mode", "paper") == "live_manual":
            raise ValueError(
                "the live server's account is pinned by LIVE_ACCOUNT_NAME "
                "in its service environment - switching is not a runtime "
                "operation on live")
        if name not in self.registry():
            raise ValueError(f"no keys for account {name!r} in .env "
                             f"(add ALPACA_ACCOUNT_{name.upper()}_API_KEY/"
                             f"_SECRET_KEY)")
        if open_plans > 0 and name != self.applied_name:
            raise ValueError(
                f"{open_plans} open plan(s) - close/flatten everything before "
                "switching accounts (a switch would strand their enforcement)")
        async with self.db.session() as session:
            row = await session.get(AppSetting, ACCOUNT_KEY)
            if row is None:
                session.add(AppSetting(key=ACCOUNT_KEY, value={"selected": name}))
            else:
                row.value = {**(row.value or {}), "selected": name}
            await session.commit()
        return await self.list_accounts()


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
            "paper": bool(getattr(alpaca.settings, "alpaca_paper", True)),
            "mode": getattr(alpaca.settings, "trading_mode", "paper"),
            "account_status": account_status,
            # Broker market clock as the enforcer sees it (cached snapshot;
            # {"known": False} until the first successful fetch).
            "market_clock": enforcer.clock.status(),
        },
        "db": {"ok": db_ok, "latency_ms": db_ms,
               "engine": "postgres" if db.url.startswith("postgres") else "sqlite"},
        "redis": {"ok": redis.healthy},
        "enforcer": {
            "monitors": len(enforcer._monitors),
            "monitored_plan_ids": sorted(enforcer._monitors.keys()),
            "ghost_keys": sum(len(v) for v in enforcer._ghost_keys.values()),
            "last_reconcile_age_s": reconcile_age,
            # Monitors that currently CANNOT evaluate TP/SL (no quote for
            # some leg even after REST polling) — must be zero in health.
            # Only genuine no-mid states: "sl-confirming" is the dwell
            # working as designed, not a data outage.
            "monitors_without_mid": {
                pid: status
                for pid, status in enforcer.monitor_health.items()
                if status.startswith("no-mid")
            },
            # Exits parked against a closed market (one resting limit each;
            # the ladder resumes at the open).
            "parked_exits": sorted(enforcer._parked),
        },
        # Strategy data plane: a dead feed shows as task state + climbing
        # last_event_age, never as quiet no-trading. Bus drop counters must
        # stay zero — a lossless drop means a wedged consumer.
        "signals": {
            "feeds": {
                feed.name: {
                    "task": _task_state(task),
                    **feed.status(),
                }
                for feed, task in zip(
                    getattr(app_state, "feeds", ()),
                    getattr(app_state, "feed_tasks", ()),
                )
            },
            "event_bus": (
                app_state.event_bus.status()
                if getattr(app_state, "event_bus", None)
                else {}
            ),
        },
        "strategies": (
            app_state.strategy_runner.status()
            if getattr(app_state, "strategy_runner", None)
            else {}
        ),
        "tasks": {
            "trading_stream": _task_state(getattr(app_state, "trading_stream_task", None)),
            "reconcile_loop": _task_state(getattr(app_state, "reconcile_loop_task", None)),
            "startup_reconcile": _task_state(getattr(app_state, "reconcile_task", None)),
            "keep_awake": _task_state(getattr(app_state, "keep_awake_task", None)),
            "wake_watchdog": _task_state(getattr(app_state, "wake_watchdog_task", None)),
        },
        "power": {
            "keep_awake_supported": getattr(
                getattr(app_state, "keep_awake", None), "supported", False
            ),
            # True while the OS idle-sleep timer is inhibited (open plans).
            "keep_awake_active": getattr(
                getattr(app_state, "keep_awake", None), "active", False
            ),
        },
    }
