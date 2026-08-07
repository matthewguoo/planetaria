"""Strategy contract. A strategy is a plugin that consumes Events and emits
TradeIntents; everything else — journaling, budgets, global risk, order
placement, exit enforcement — is the runtime's job. Strategies never talk to
the broker, the DB, or an LLM directly: market access goes through
ctx.submit(), LLM access through ctx.analyze() (structured-output-only,
injection-hardened, journaled — see services/llm.py).

Design rule: ctx.submit() is the ONLY path to the market, and it funnels into
the same TradeService.place_trade() a human click uses, so every automated
trade inherits the FSM, the plan journal, the exec-quality ledger, and the
exit enforcer with zero strategy-side code.
"""

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal

from app.services.signals.events import Event

# A wedged strategy must not stall its own event queue silently. This is the
# DEFAULT; a strategy that legitimately blocks longer per event (LLM analysis
# runs seconds-to-minutes) declares its own budget via the event_timeout_s
# ClassVar instead of racing this one.
EVENT_TIMEOUT_S = 30.0
# Stale-event guard default: an intent derived from events older than this is
# refused. A restart or a backed-up queue can never trade on old news.
MAX_EVENT_AGE_S = 300.0

# What a strategy needs from the platform before it can trade for real. These
# are DECLARATIONS, not gates: a strategy whose requirements are unmet still
# runs and still journals, because a shadow run against an unmet requirement
# is exactly how you find out what the requirement costs. The runtime surfaces
# the gap; it does not refuse the instance.
#
#   sip      real-time consolidated quotes. The free IEX feed is dead outside
#            08:00-17:00 ET, so anything entering after the close needs this.
#   shorts   sells with no position. Gated engine-wide by equity_long_only.
#   options  multi-leg option orders rather than shares.
#   llm      a configured model backend for ctx.analyze().
CAPABILITIES = frozenset({"sip", "shorts", "options", "llm"})


@dataclass(slots=True)
class TradeIntent:
    asset_class: Literal["option", "equity"]
    underlying: str
    # Options: the existing leg shape ({symbol, right, strike, expiry, side,
    # ratio, entry, iv, half_spread}). Equity: [{symbol, side: 1, ratio: 1,
    # entry: <share price>}].
    legs: list[dict]
    qty: int                      # contract sets / shares
    entry_limit: float            # signed net premium / share price
    # Position-value targets, per-share terms. BOTH None = a time-stop-only
    # plan: the enforcer evaluates no thresholds and the time stop is the exit.
    # One None alone is refused at placement — it is neither a target nor a
    # stop. Strategies that run unbracketed are bounded by their allocation and
    # their circuit breaker instead of by a per-position stop.
    tp: float | None
    sl: float | None
    time_stop_utc: datetime
    extended_hours: bool = False  # equity single-leg DAY limits only (Phase 4)
    reason: str = ""              # human-readable "why" for the journal
    signal_ids: tuple[int, ...] = ()   # provenance into the signals table
    dedupe_key: str | None = None      # idempotency across restarts/replays
    max_event_age_s: float = MAX_EVENT_AGE_S


@dataclass(slots=True)
class StrategyContext:
    """Injected capabilities. `market` and `clock` are live service handles
    (quotes via market.latest_quote / fetch_latest_stock_quote); submit and
    note round-trip through the runner."""

    market: object
    clock: object
    params: dict
    log: logging.Logger
    _runner: object = field(repr=False, default=None)
    _instance_id: str = field(repr=False, default="")

    async def submit(self, intent: TradeIntent) -> dict:
        """Full pipeline: stale-event guard -> dedupe -> per-strategy budget
        -> global risk -> place_trade. Raises ValueError with the violation
        list when refused; every outcome is journaled either way."""
        return await self._runner.execute_intent(self._instance_id, intent)

    async def note(self, action_detail: dict, signal_ids: tuple[int, ...] = ()) -> None:
        """Journal a decision that did NOT produce an intent (e.g. 'saw the
        event, conditions not met') — silence is the enemy of replay."""
        await self._runner.journal_note(self._instance_id, action_detail, signal_ids)

    async def account(self) -> dict:
        """THE STRATEGY'S OWN BOOK, not the broker account.

        `equity` here is this instance's allocation, so the ordinary sizing
        formula (risk_pct of equity, or a fraction per position) sizes against
        the capital the operator gave this strategy rather than against the
        whole account. Two strategies at 25% each cannot both size as if they
        owned everything.

        The real broker numbers are still present under `account_equity` and
        `account_cash` — scoping the view must not mean hiding the truth from
        a strategy that has a reason to look. `deployed` and `available` are
        this instance's own open notional and what is left of its allocation;
        execute_intent enforces the same figure, so a strategy that ignores
        `available` gets refused rather than silently over-committing.
        """
        return await self._runner.account(self._instance_id)

    async def analyze(
        self,
        *,
        task: str,
        data: str,
        schema: dict,
        system: str | None = None,
        symbols: tuple[str, ...] = (),
        signal_ids: tuple[int, ...] = (),
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ):
        """Structured LLM analysis of untrusted text (see services/llm.py for
        the guarantees). Returns an Analysis whose .signal_id should be
        included in TradeIntent.signal_ids so the verdict is part of the
        trade's provenance chain. Raises AnalysisError/AnalysisRefused —
        letting it propagate journals an error for this event, which is
        usually the right call. Budget on_event accordingly: declare a larger
        event_timeout_s on the strategy class than the analysis timeout."""
        return await self._runner.execute_analysis(
            self._instance_id, task=task, data=data, schema=schema,
            system=system, symbols=symbols, parent_signal_ids=signal_ids,
            model=model, effort=effort, max_tokens=max_tokens, timeout_s=timeout_s,
        )


class Strategy(abc.ABC):
    """Subclass, set the ClassVars, implement on_event. Register the class in
    app/strategies/__init__.py (explicit imports, no magic scanning)."""

    kind: ClassVar[str]                        # registry key
    subscriptions: ClassVar[tuple[str, ...]]   # event types to receive
    default_params: ClassVar[dict] = {}
    # Platform requirements, from CAPABILITIES above. Declared so the console
    # can show what a strategy needs and whether it currently has it — an
    # earnings strategy silently journalling "no fresh quote" every night
    # because the account has no SIP entitlement is the failure this exists
    # to make visible.
    requires: ClassVar[frozenset[str]] = frozenset()
    # Per-event wall-clock budget. Raise on strategies whose on_event
    # legitimately blocks (ctx.analyze) — the queue is per-instance, so a
    # longer budget only delays THIS strategy's own later events.
    event_timeout_s: ClassVar[float] = EVENT_TIMEOUT_S

    @classmethod
    def validate_params(cls, params: dict) -> dict:
        """Merge over defaults; raise ValueError on garbage. Override for
        stricter validation. The 'budget' key is reserved for the runner's
        per-strategy risk budget and passes through untouched."""
        for key in params:
            if key not in cls.default_params and key != "budget":
                raise ValueError(f"unknown param {key!r} for strategy {cls.kind!r}")
        return {**cls.default_params, **params}

    async def on_start(self, ctx: StrategyContext) -> None:  # noqa: B027
        """Optional warm-up (subscribe market data, load state)."""

    @abc.abstractmethod
    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        ...

    async def on_stop(self, ctx: StrategyContext) -> None:  # noqa: B027
        """Optional cleanup. The runner calls this on pause/shutdown; open
        plans are NOT closed here — the exit enforcer owns them regardless."""
