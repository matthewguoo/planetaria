# Strategy authoring — how a session builds one

Written 2026-08-10. The standing answer to "build me a strategy": what to
read, what the platform owes you, what you owe it, and the ladder from idea
to live. A fresh session pointed at this file plus a one-line strategy idea
should be able to do the whole job without archaeology.

## 0. Standing rules (unchanged, non-negotiable)

- Commit locally per step, **never push** without asking Matthew.
- Paper-lock is hard (`ALPACA_PAPER=false` refuses to boot). Keys live in
  the gitignored root `.env` only.
- `app/` never reads anything under `research/`.
- `pytest` (backend) and `vitest` (frontend) stay green; `ruff check`
  clean. The suite count only goes up.
- ET from `zoneinfo`, never from shell `TZ` tricks (Git Bash lies).

## 1. Read these, in order, before writing code

1. `app/strategies/base.py` — the whole contract: `Strategy`,
   `TradeIntent`, `StrategyContext` (`ctx.submit / note / account /
   analyze`). It is ~180 lines and it is the API.
2. `app/strategies/pead_flagship.py` — the flagship exemplar: watchlist,
   re-anchor, LLM call, fade branch, slot state that survives restarts.
3. `app/services/strategy_runner.py` — what `ctx.submit` actually does:
   stale-event guard → dedupe → circuit breaker → allocation/collateral →
   per-strategy budget → global risk → `place_trade`. You inherit ALL of
   it; write none of it.
4. `app/api/routes/strategies.py` — the control plane the console uses:
   create/enable/pause/flatten, allocation, breaker, capital, decisions,
   performance, twin, catalog + source view.
5. The evidence for your idea under `research/` — see §2.

## 2. Study first, or don't build

No strategy class without a dated note under `research/<study>/notes/`
that states the effect, its size, its t-stat, its cost sensitivity, and
its year-by-year stability. The scan of 2026-08-10
(`research/notes/alpha_scan_20260810.md`) is the template and the current
map of what is alive, parked, and dead — check the anti-queue before
re-deriving a dead idea. Two data traps documented there (§4) will
otherwise eat your first day: adjusted-vs-raw price bases, and `anchor`
being post-release tape for early acceptances.

**Pre-register before the first live decision**: the exact params, the
allocation and breaker, the metric, and the stopping rule, committed as
`docs/pre-registration-<name>.md`. A number chosen after that commit is a
new hypothesis, not a result.

### 2b. The ML testing bar (set 2026-08-11, the gff gate is the exemplar)

Any FITTED model (gate, ranker, classifier) clears ALL of these before it
is believed, in this order — each answers a distinct failure mode:

1. **Walk-forward, expanding by year** — the deployment claim. Nothing
   from a test year touches the model that scores it.
2. **Primary cell pre-declared in the script docstring** before results;
   every swept threshold/config reported as a FULL family with the
   multiplicity haircut named.
3. **Selection-shuffle null** — the pre-declared cell must beat random
   selection at the same keep-rate (2,000 within-year shuffles).
4. **Train-label placebo** — refit the whole pipeline on shuffled labels;
   if it still shows lift, the pipeline manufactures edge and everything
   downstream is void.
5. **Seed + ablation** — the result must survive seeds and must not die
   with one feature column (concentration is fine if the feature is
   economically legible; report it).
6. **Paired block bootstrap** of the daily OOS series — CI on the Sharpe
   difference and drawdown, because point estimates of both are noisy.
7. **Combinatorial year-block CV** — the lift as a DISTRIBUTION, with the
   walk-forward number located inside it; PLAN on the CV-central value,
   not the walk-forward draw (the gate: +8.8 WF was the 89th percentile
   of a +5.2-centered distribution).
8. **Live forward test** (journal-only) before any dollar routes through
   the model; tau/threshold changes are pre-reg amendments.

Deliberately NOT run: synthetic price paths (GBM worlds) — a market
without our microstructure cannot confirm or deny an edge that lives in
it. Scripts: `research/open-window/scripts/research_gff_gate*.py` are the
working exemplars of every step.

## 3. Fleet rules

Every experimental strategy runs inside an envelope the operator sets in
the console (STRATEGIES → instance → CAPITAL):

- **Allocation** — pct of equity or a dollar ceiling. `ctx.account()`
  reports it as your `equity`; sizing against anything else gets refused
  at `execute_intent`. A $10k experiment is `{"mode": "usd", "value":
  10000}` and the runtime holds you to it.
- **Circuit breaker** — drawdown vs the strategy's own high-water mark
  that flattens and pauses it. Never run an unbracketed strategy with the
  breaker off.
- **Note-mode ladder** — `live: false` first (a param on kinds that
  support it; every new kind should): decisions journal, nothing places,
  `GET /api/strategies/{id}/twin` is your equity curve. Then one-share /
  one-set proof. Then small live. Scale on journaled evidence, not on the
  backtest that got you here.

### Options: defined-risk only, and the engine now enforces it

Empirical broker facts (do not re-derive): naked short CALLS are rejected
at this account level; short PUTS are CASH-SECURED (~strike × 100 held —
$70k for one 700P). Inside a fleet allocation both are unusable, so:

- Structures must be **defined-risk**: every short leg covered by a long
  wing of the same right. Debit spreads, credit spreads, flies, condors.
- `options_required_capital` (strategy_runner) prices what the broker
  HOLDS — width × 100 per covered short, full collateral for a CSP, plus
  any net debit — and `execute_intent` refuses intents past the
  allocation's `available`. Uncovered short calls are refused outright
  with the fix in the message. Deployed capital of open options plans is
  counted the same way, so a book of flies shrinks `available` honestly.
- Consequence for design: a $10k allocation runs ~10-30 defined-risk
  structures depending on width. Size by `ctx.account()["available"]`,
  not by premium.
- No options overnight; index options are RTH/paper-only; MLEG has no
  bracket — the ExitEnforcer IS the bracket, and a hard time stop is a
  valid exit plan (tp/sl both None).

### Equities

- Shorts exist behind `equity_long_only` (default on) + per-symbol
  ETB/shortable checks; `equity_short_overnight` stays false. A strategy
  that needs shorts declares `requires = frozenset({"shorts"})` so the
  console shows the gap instead of you debugging silence.
- After-hours entries need SIP (`requires={"sip"}`) — without it, IEX
  quotes are hours stale after 17:00 ET and the freshness gates refuse
  you, correctly. 24/5 equity is LIMIT-only; never submit market outside
  RTH.

## 4. The build recipe

1. `app/strategies/<name>.py`: subclass `Strategy`; set `kind`,
   `subscriptions` (event types), `default_params` (include `live:
   False`), `requires`, and `event_timeout_s` if `ctx.analyze` is in the
   loop. Implement `on_event`; journal every non-action with `ctx.note`
   (silence is the enemy of replay). Persist any cross-restart state in
   params or derive it from the plan table — a restart that forgets its
   book will double it.
2. Register in `app/strategies/__init__.py` (explicit import, no magic).
3. `tests/test_<name>.py`, mirroring `tests/test_pead_flagship.py`:
   the happy-path intent, the refusal paths you rely on, the restart
   behaviour of any state you keep. The options-collateral cases are
   covered engine-side (`tests/test_options_collateral.py`) — don't
   re-test the runtime, test YOUR logic.
4. `ruff check` + full `pytest`. Frontend needs nothing: the console is
   kind-agnostic (catalog, params editor, journals, capital, performance,
   twin all come free).
5. Create the instance (console STRATEGIES → NEW, or
   `POST /api/strategies {kind, name, params}`), set allocation +
   breaker, enable, and let it journal a real stretch.
6. Read the outcome where the operator will: DECISION JOURNAL for every
   skip/refusal, PERFORMANCE for plans + realized P&L once live, TWIN for
   the note-mode curve. `GET /api/strategies/catalog/<kind>/source` shows
   the machine's actual code next to its journal — what you shipped is
   what is shown.

## 5. Skeleton

```python
from datetime import datetime, timedelta, timezone

from app.services.signals.events import Event
from app.strategies.base import Strategy, StrategyContext, TradeIntent


class ExampleFly(Strategy):
    """One-line doc — the console shows it in the catalog."""

    kind = "example_fly"
    subscriptions = ("clock_tick",)          # or news/earnings/manual
    default_params = {"live": False, "width_pct": 1.0}
    requires = frozenset({"options"})

    async def on_event(self, event: Event, ctx: StrategyContext) -> None:
        acct = await ctx.account()           # YOUR allocation, not the account
        if not self._setup_ok(event):
            await ctx.note({"skip": "conditions not met"})
            return
        intent = TradeIntent(
            asset_class="option", underlying="QQQ",
            legs=[...],                      # defined-risk: wings, always
            qty=1, entry_limit=-2.50,        # signed: negative = credit
            tp=None, sl=None,                # time-stop-only is legal
            time_stop_utc=datetime.now(timezone.utc) + timedelta(hours=2),
            reason="…", dedupe_key=f"{self.kind}:{event.key}",
        )
        if not ctx.params.get("live"):
            await ctx.note({"would_place": intent.reason})
            return
        await ctx.submit(intent)             # every guard fires in here
```

## 6. What "ready" means before `live: true`

- A pre-registration commit exists and is named in the instance notes.
- The journal shows the strategy seeing, skipping, and would-placing for
  a meaningful stretch (an earnings strategy: a season; a daily one: 2-4
  weeks), and the twin's curve is consistent with the study that
  motivated it.
- Allocation and breaker are set to numbers you would defend out loud.
- The capability row in the catalog shows every `requires` met — a
  strategy running against an unmet requirement is a journal, not a
  trade, and that is the design working.
