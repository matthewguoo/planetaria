"""Composition root: constructs and wires every service, owns startup and
shutdown order. main.py stays a thin ASGI entrypoint."""

import asyncio
import logging

from fastapi import FastAPI

from app.config import Settings
from app.db.session import Database
from app.services.alpaca import AlpacaService
from app.services.bar_store import BarStore
from app.services.broadcast import Broadcaster
from app.services.exit_enforcer import ExitEnforcer
from app.services.market_data import MarketDataService
from app.services.options_chain import ChainService
from app.services.redis_client import RedisFacade
from app.services.risk import RiskService
from app.services.signals.events import EventBus
from app.services.signals.feeds import AlpacaNewsFeed, TimerFeed
from app.services.signals.store import SignalStore
from app.services.supervision import supervise
from app.services.trade_service import TradeService

log = logging.getLogger("app.bootstrap")


def strategy_plane_enabled(settings: Settings) -> bool:
    """The strategy plane — bus, feeds, signal store, runner, breakers —
    exists only on the paper server. In live_manual mode NONE of it is
    constructed (not merely disabled): the only order-entry path in the
    live process is the human ticket, structurally."""
    return getattr(settings, "trading_mode", "paper") == "paper"


async def startup(app: FastAPI, settings: Settings) -> None:
    redis = RedisFacade(settings.redis_url)
    await redis.connect()

    db = Database()
    await db.connect(settings.database_url, fallback=settings.sqlite_fallback)

    # Persisted feed settings override env defaults for the feed tiers —
    # applied here because the stream clients are constructed once.
    from app.services.system_state import AccountService, FeedSettingsService

    feed_settings = FeedSettingsService(db)
    try:
        feed_cfg = await feed_settings.get()
        settings.alpaca_stock_feed = feed_cfg["stock_feed"]
        settings.alpaca_option_feed = feed_cfg["option_feed"]
    except Exception:
        log.exception("feed settings load failed - using env defaults")

    # Persisted ACCOUNT selection: point settings at the chosen paper
    # account's keys before any client exists (see AccountService for the
    # paper-only and no-open-plans locks).
    accounts = AccountService(db, settings)
    try:
        await accounts.apply()
    except Exception:
        if settings.trading_mode == "live_manual":
            # A live boot without its pinned account must die here — the
            # fallback below would leave paper keys in settings with the
            # client about to be constructed paper=False.
            raise
        log.exception("account selection failed - using default keys")
    app.state.accounts = accounts

    alpaca = AlpacaService(settings)
    broadcaster = Broadcaster()
    bars = BarStore(redis, max_1m_bars=settings.bar_cache_days * 3900)
    market = MarketDataService(settings, alpaca, redis, bars, broadcaster)
    risk = RiskService(db)
    trade = TradeService(db, alpaca, market, risk)
    enforcer = ExitEnforcer(db, market, trade)
    # Account capabilities: the verified ceiling every gate reads. Loaded
    # before any request; broker flags refreshed in the background (read
    # only - the probe itself is only ever a human click).
    from app.services.capabilities import CapabilitiesService

    capabilities = CapabilitiesService(db, alpaca, trade, enforcer.clock, settings)
    await capabilities.load()
    risk.capabilities = capabilities
    trade.capabilities = capabilities

    app.state.settings = settings
    app.state.redis = redis
    app.state.db = db
    app.state.alpaca = alpaca
    # Symbol picker's tradability universe (lazy: first search loads it).
    from app.services.symbols import AssetUniverse

    app.state.symbols = AssetUniverse(alpaca)
    app.state.broadcaster = broadcaster
    app.state.market = market
    app.state.chain = ChainService(alpaca, redis, market)
    from app.services.contracts import ContractsService

    app.state.contracts = ContractsService(alpaca, market)
    app.state.risk = risk
    app.state.trade = trade
    app.state.enforcer = enforcer
    app.state.capabilities = capabilities
    app.state.capabilities_refresh_task = (
        asyncio.create_task(capabilities.refresh_broker(), name="capabilities-refresh")
        if alpaca.configured else None
    )
    from app.services.portfolio_accounts import PortfolioAccounts
    from app.services.portfolio_risk import PortfolioRisk

    app.state.portfolio_risk = PortfolioRisk(trade, market)
    app.state.portfolio_accounts = PortfolioAccounts(accounts, db)
    app.state.feed_settings = feed_settings
    app.state.trading_stream = None
    app.state.trading_stream_task = None

    await market.start()

    if alpaca.configured:
        stream = alpaca.make_trading_stream()
        stream.subscribe_trade_updates(trade.on_trade_update)
        app.state.trading_stream = stream
        app.state.trading_stream_task = asyncio.create_task(
            supervise("trading-stream", stream._run_forever), name="trading-stream"
        )
    # Reconcile runs in BOTH modes (its broker calls are internally guarded):
    # keyless plans still need their monitors armed so TP/SL evaluates
    # against the synthesized quotes. Runs in the background: serial REST
    # calls during lifespan would hold uvicorn's accept loop closed — the
    # "server up but nothing connects" startup window.
    app.state.reconcile_task = asyncio.create_task(
        _reconcile_with_retry(enforcer), name="startup-reconcile"
    )
    # Periodic REST truth-sync: the TradingStream is the fast path for
    # fills, but a fill landing during a stream gap must not leave a live
    # position unmanaged. Also re-arms any open plan missing its monitor.
    # Supervised: a fatal error in the loop restarts it with backoff
    # instead of silently ending truth-sync forever.
    app.state.reconcile_loop_task = asyncio.create_task(
        supervise("reconcile-loop", enforcer.reconcile_loop), name="reconcile-loop"
    )
    # Power defenses: exits are software-enforced, so the machine sleeping on
    # an open position is an enforcement outage. Keep Windows awake while any
    # plan is open, and if the loop was suspended anyway (sleep/hibernate),
    # reconcile the moment it comes back instead of on the next interval.
    from app.services.power import KeepAwake, wake_watchdog

    # (not supervised: on non-Windows run() exits immediately by design, and
    # each pass already contains its own exception handling)
    app.state.keep_awake = KeepAwake(risk)
    app.state.keep_awake_task = asyncio.create_task(
        app.state.keep_awake.run(), name="keep-awake"
    )
    app.state.wake_watchdog_task = asyncio.create_task(
        supervise("wake-watchdog", lambda: wake_watchdog(enforcer)),
        name="wake-watchdog",
    )

    if not strategy_plane_enabled(settings):
        # LIVE SERVER: everything below this line is automation that can
        # originate orders. It is never constructed here — the enforcer,
        # streams, and reconcile loops above are the whole live runtime.
        log.info("live_manual: strategy plane NOT constructed "
                 "(no bus, no feeds, no runner, no breakers)")
        return

    # Strategy data plane: DataFeed adapters normalize external sources into
    # journaled events on the in-process bus; strategies subscribe by type.
    # Each feed is one supervised connection — a feed crash restarts with
    # backoff and can never take the engine down.
    bus = EventBus()
    signal_store = SignalStore(db)
    feeds = [TimerFeed(bus)]
    if alpaca.configured:
        feeds.append(AlpacaNewsFeed(alpaca, bus, signal_store))
    # EDGAR needs no key (free, authoritative 8-K full text). The Finnhub
    # earnings calendar only runs with a key; without it the reaction
    # strategy has no watchlist but nothing else degrades.
    from app.services.signals.edgar import EdgarFeed
    from app.services.signals.earnings_calendar import EarningsCalendarFeed

    feeds.append(EdgarFeed(bus, signal_store, settings))
    if settings.finnhub_api_key:
        feeds.append(EarningsCalendarFeed(bus, signal_store, settings))
    else:
        log.info("FINNHUB_API_KEY not set - earnings calendar feed disabled")
    app.state.event_bus = bus
    app.state.signal_store = signal_store
    app.state.feeds = feeds
    app.state.feed_tasks = [
        asyncio.create_task(supervise(f"feed-{feed.name}", feed.run),
                            name=f"feed-{feed.name}")
        for feed in feeds
    ]

    # Signal retention: EDGAR journals up to 20k chars of press-release text
    # per 8-K, so the table grows without bound unless pruned. plan_events —
    # the trade audit trail — is a different table and is never pruned.
    async def signal_prune_loop() -> None:
        while True:
            await signal_store.prune()
            await asyncio.sleep(24 * 3600)

    app.state.signal_prune_task = asyncio.create_task(
        supervise("signal-prune", signal_prune_loop), name="signal-prune"
    )

    # Strategy runtime: one supervised task per enabled instance, intents
    # funneled through the SAME place_trade path human clicks use. State
    # rebuilds from strategy_instances rows, the enforcer pattern.
    from app.services.strategy_runner import StrategyRunner

    runner = StrategyRunner(
        db, bus, signal_store, trade, risk, market, enforcer.clock, settings
    )
    app.state.strategy_runner = runner
    # KeepAwake watches the runner too: enabled strategies need the box
    # awake for feeds and timer ticks even with zero open plans (note-mode).
    app.state.keep_awake.runner = runner
    await runner.start()
    # Per-strategy circuit breakers. Supervised like every other loop: an
    # unsupervised breaker that dies is worse than none, because the console
    # would still show a limit that is no longer being enforced.
    app.state.breaker_loop_task = asyncio.create_task(
        supervise("strategy-breakers", runner.breaker_loop),
        name="strategy-breakers",
    )


async def _reconcile_with_retry(enforcer: ExitEnforcer, attempts: int = 5) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await enforcer.startup_reconcile()
            log.info("startup reconcile complete")
            return
        except Exception:
            log.exception("startup reconcile failed (attempt %d/%d)", attempt, attempts)
            await asyncio.sleep(min(2**attempt, 30))
    log.error("startup reconcile gave up after %d attempts", attempts)


async def shutdown(app: FastAPI) -> None:
    state = app.state
    # A running capabilities probe must finish its cleanup while the broker
    # client is still alive - before anything below stops.
    if getattr(state, "capabilities", None) is not None:
        try:
            await state.capabilities.abort()
        except Exception:  # noqa: BLE001
            log.exception("capabilities probe abort failed on shutdown")
    if getattr(state, "capabilities_refresh_task", None):
        state.capabilities_refresh_task.cancel()
    # Order matters: stop generating intents (runner), then stop event intake
    # (feeds), THEN the enforcer — in-flight exits must finish enforced.
    if getattr(state, "breaker_loop_task", None):
        state.breaker_loop_task.cancel()
    if getattr(state, "strategy_runner", None):
        await state.strategy_runner.shutdown()
    for task in getattr(state, "feed_tasks", ()):
        task.cancel()
    if getattr(state, "signal_prune_task", None):
        state.signal_prune_task.cancel()
    if getattr(state, "reconcile_task", None):
        state.reconcile_task.cancel()
    if getattr(state, "reconcile_loop_task", None):
        state.reconcile_loop_task.cancel()
    if getattr(state, "keep_awake_task", None):
        state.keep_awake_task.cancel()
    if getattr(state, "wake_watchdog_task", None):
        state.wake_watchdog_task.cancel()
    await state.enforcer.shutdown()
    if state.trading_stream_task:
        state.trading_stream_task.cancel()
    if state.trading_stream is not None:
        try:
            await state.trading_stream.close()
        except Exception:
            pass
    await state.market.stop()
    if getattr(state, "portfolio_risk", None):
        await state.portfolio_risk.close()
    await state.db.close()
    await state.redis.close()
    log.info("shutdown complete")
