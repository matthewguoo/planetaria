# planetaria

A FastAPI backend against Alpaca (paper) with a React ops console over its
API. The core design rule: **every position has a server-enforced exit
plan** that survives restarts.

`/` is the **ops console** — a portal for running a book of strategies. Five
destinations, and they are peers:

| page | what it answers |
|---|---|
| FUND | the allocator's view — allocation envelopes per strategy, what each one holds, and the account-level exposure and correlation rollup |
| ACCOUNT | the money — balances, exposure, equity curve, open and closed P&L, and which paper account the engine trades |
| STRATEGIES | the book — instances, their capital, decision journals, performance, and the paper twin for strategies that place nothing yet |
| MARKET | what the engine can see — session clock, the majors, the earnings calendar, the news tape |
| SYSTEM | whether the machine works — subsystem health, background tasks, signal feeds, the event bus, and the live call flow |

SYSTEM earns equal billing because an engine that trades unattended fails
silently by default: a dead feed and a quiet market produce the same empty
screen everywhere else.

Nothing in the console knows what a particular strategy *does*. A new strategy
appears there by being registered in `app/strategies/__init__.py`, with no
frontend change. Each instance declares what it needs from the platform
(`requires = {"sip", "shorts", ...}`) and the console shows whether it has it,
so an engine journalling "no fresh quote" all night is visible rather than
indistinguishable from a quiet market.

### Risk is bounded per strategy, not per position

Each instance carries two dials:

- **allocation** — a percent of equity or a dollar ceiling. `ctx.account()`
  reports it as the strategy's equity, so a strategy sizes against its own
  book rather than the account, and an intent past what is left is refused.
- **circuit breaker** — a drawdown against that strategy's own high-water
  mark. When it trips, the book is flattened and the instance paused.

That pair replaced the per-position stop-loss requirement. Plans may now carry
no TP and no SL at all, because the PEAD research measures a stop inside the
whipsaw band of a name that just moved 5% as where most of the edge goes
(38.3bp per trade with a bracket against 138.6bp without). The core invariant
is unchanged — every position still has a server-enforced exit plan, and a
hard time stop is one. A stop bounds one trade and costs edge on every trade;
a breaker bounds the strategy and costs nothing until it fires.

The discretionary options terminal — the chart, the payoff designer, the
chain, the mobile shell — was retired on 2026-08-07 and lives in git history.
Its backend survives where the engine still needs it: the FSM, the exit
enforcer, and the options math that prices collateral and model-value stops.
`docs/screenshots/` is that UI's historical walkthrough.

`research/` holds studies — code that produces evidence, kept out of
`backend/` so the dependency runs one way: research reads the app, the app
never reads research. See [research/README.md](research/README.md).

## Architecture

```
frontend (Vite/React — the ops console; polls REST, no WebSocket)
   │  REST /api/*
backend (FastAPI, single process)
   ├─ MarketDataService   one stock + one option stream, ref-counted subs,
   │                      background REST backfill, reconnect gap-fill,
   │                      overnight (Blue Ocean) poller, no-SIP AH prints
   ├─ TradeService        validated placement -> DB plan -> Alpaca order
   ├─ PlanStateMachine    FIX-style FSM governing every plan's lifecycle
   ├─ ExitEnforcer        bracket engine: TP rests AT THE BROKER as a live
   │                      limit; SL is software with a trigger hierarchy
   │                      (option ticks -> underlying-tick model checks ->
   │                      adaptive REST polling); escalation ladder
   │                      (limit @ mid-2% -> mid-6% -> market -> verify loop)
   ├─ RiskService         BP cap, gross exposure, max positions, daily
   │                      circuit breaker — server-side, not advisory.
   │                      Per-trade max loss applies only to BRACKETED
   │                      plans; unbracketed strategies are bounded by
   │                      their allocation + circuit breaker instead
   ├─ StrategyRunner      one supervised task per enabled instance, with
   │                      per-strategy allocation and drawdown breakers
   ├─ EventBus + feeds    timer / Alpaca news / EDGAR 8-K / earnings
   │                      calendar, journaled to the signals store
   └─ Postgres/Redis      SQLite/in-memory graceful fallbacks for dev
```

### The trade object and its state machine

Plan lifecycle is governed by an explicit finite state machine
(`app/services/plan_fsm.py`), modeled on the FIX protocol's order state
matrices (FIX Appendix D) — the same pattern production engines such as
NautilusTrader use. A declarative transition table maps every
(event, state) pair to a single legal target state:

```
planned → submitted → [partially_filled →] filled → exiting → closed
                 └→ cancelled / rejected        (terminal states absorb all)
```

Events (broker fills/cancels, exit submissions, reconcile findings) are the
ONLY way state changes. Illegal, duplicate, or out-of-order broker events
land on absent table entries and are ignored by construction — a closed plan
can never be resurrected by a late fill notification. Each transition is
applied under a per-plan asyncio lock with an atomic compare-and-set UPDATE
(`WHERE status = expected`), so concurrent writers cannot corrupt the
lifecycle. The full state × event matrix is exhaustively unit-tested.
Partial fills are first-class: a partially filled entry that gets cancelled
shrinks the plan to the filled quantity and keeps managing it.

A multi-leg structure is ONE `TradePlan` row: legs (with per-leg ratio),
contract sets, signed net premium (positive = debit, negative = credit), TP/SL
premiums on the same signed axis, and a UTC time stop. The plan is committed
to the DB *before* the entry order reaches Alpaca; the enforcer is rebuilt
from these rows on startup, so a restart can never orphan a live position.
Entries and exits use Alpaca MLEG orders (max 4 legs), so multi-leg
structures fill and close atomically.

Broker positions with no plan (placed elsewhere, or predating the DB) are
surfaced as **UNTRACKED** on the ACCOUNT page and can be **adopted**:
grouped per underlying into a managed plan with exit enforcement.

Plans link to the strategy that placed them by `strategy_id` (the FK); the
24-char `strategy` column is the display label only.

### Options math (what survived the terminal)

`app/services/options_math.py` keeps the BSM core — pricing, implied-vol
solve, probability-of-touch, structural max loss — because the engine still
prices with it: the enforcer's underlying-tick model-value stop checks,
portfolio greeks for `/api/account/risk`, and the collateral estimates the
allocation gate charges option structures.

## Run (dev)

```powershell
./dev.ps1          # docker infra (postgres:5433, redis:6380) + uvicorn + vite
```

or by hand:

```bash
docker compose -f docker-compose.dev.yml up -d
cd backend  && uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev
```

Copy `.env.example` to `.env` (repo root or `backend/` — discovery is
anchored to the source tree, so the launch directory doesn't matter) and add
Alpaca **paper** keys. With keys, prices are Alpaca's live feed (IEX
real-time on the free tier; history blends SIP for anything older than
16 minutes). Without keys the engine boots but cannot price or trade.

Preview on a phone (the console talks same-origin through the vite proxy):

```bash
cd frontend && npm run phone     # builds + serves on your LAN, port 4173
```

## Checks

```powershell
./check.ps1        # ruff (backend+research), backend pytest, frontend
                   # typecheck + lint + vitest — all failures listed at the end
./check.ps1 -Fast  # lint + typecheck only (~15s; what the pre-commit hook runs)
./check.ps1 -Paper # also rebuild the pead paper and fail if report.html moves
```

The pre-commit hook is enabled with `git config core.hooksPath .githooks`
(bypass in an emergency with `git commit --no-verify`). Backend pytest runs
under `asyncio_mode = auto`, so an async test without a marker cannot skip
silently. `backend/requirements.lock.txt` pins the exact venv the tests and
the paper's byte-identical rebuild gate run on.

### Engine API & headless mode

The execution engine (streams → FSM → exit enforcer → reconcile → risk)
is architecturally separate from the UI-serving surface, and can run
alone: `HEADLESS=true` boots the full engine plus only its **command/ops
API** — no quote endpoint:

- Commands: `POST /api/orders`, `POST /api/positions/{id}/close`,
  `/flatten`, `PATCH /api/positions/{id}/exits` (tighten-only),
  `POST /api/positions/adopt`
- Ops: `GET /api/system/state`, `GET /api/positions/{id}/events`
  (lifecycle journal), `GET/PUT /api/settings/risk` and `/feed`,
  `GET /api/health`

The command endpoints are operational escape hatches — no UI calls them
today; strategies reach the market through `ctx.submit()` and the same
TradeService. This is the future split seam: run the engine supervised on
an always-on host (systemd/docker restart-always — restarts are safe, the
reconcile machinery rebuilds all monitors from the DB) and keep the UI
wherever you like. Until real capital or multi-user needs force a true
two-process split, one supervised process is the more reliable shape.

### Exit trigger hierarchy (how fast is the bracket?)

The take-profit RESTS AT THE BROKER as a live limit close the moment the
entry fills: zero software latency, and it fills even if the engine is
down. (Idempotent per (plan, level): a crash between broker-accept and
the DB write recovers the same order, never doubles it. Any other exit
first pulls the resting TP down — and because broker cancels are
asynchronous, the takedown polls the order to a TERMINAL verdict: a fill
that beats the cancel is absorbed as the close, and an unconfirmable
cancel refuses to proceed rather than risk two live closes.)

Keyless mode closes triggered plans with a SIMULATED fill at the current
mark (through the normal FSM path, so history and slippage read like a
real exit) — and position-truth checks never force-close anything
without keys, so an engine accidentally booted without its `.env`
cannot torch the records of positions still live at the broker.

The stop-loss cannot rest (no broker stop orders for options), so it is
software with a layered trigger chain, fastest first:

1. **Option stream ticks** — every leg tick re-evaluates the position mid
   (milliseconds, the normal RTH path).
2. **Underlying stock ticks** — SPY quotes stream constantly even when
   your specific contracts don't. Each tick re-marks the position with a
   BSM model value from the plan's leg IVs; at ~15% of the TP-SL span
   from a threshold it forces an immediate REST option-quote refresh and
   evaluates real mids. Sub-second reaction to the thing that actually
   moves.
3. **Adaptive REST polling** — 1s cadence while the mid sits near a
   threshold, 15s when far: the floor for a totally silent market, not
   the reaction time.
4. **Time stop** — quote-independent, always.

If a mid is ever uncomputable (a leg with no quote even via REST), the
monitor says so: throttled log naming the legs, per-plan health in
`/api/system/state`, and the enforcer block on the SYSTEM page.

### Fair-value stop trigger (no shakeouts, no blowups)

Raw option mids are chaotic — a one-tick spread blowout can print a mid
below the stop for 200ms and vanish. Triggers therefore never act on the
raw mid: they act on the estimator stack production desks run
(`app/services/fair_value.py`):

- **Microprice, not mid** (Stoikov) — the size-weighted quote price
  `I·ask + (1−I)·bid` with `I = bid_size/(bid_size+ask_size)` leans
  toward resting book pressure and predicts the next trade better than
  the midpoint. Quotes without sizes fall back to the mid.
- **Spread-gated trust** — each observation enters a 1-D Kalman filter
  with measurement variance = (position half-spread)². A tight two-sided
  market snaps the fair value; a blown-out, one-sided, or crossed quote
  barely moves it — so a junk print *cannot* fire the stop, even with
  the dwell at zero. Persistently wide markets still converge the
  estimate (information beats suspicion), just slowly.
- **Theo-drift prediction** — between option quotes, the fair value is
  moved by the *change* in the BSM model value driven by underlying
  ticks (which stream constantly). Differencing cancels model level
  bias; only its dynamics are trusted.

On top of the estimator, the trigger-decision layer:

- **Confirmation dwell** — a fair-value breach must persist `sl_confirm_s`
  seconds (risk setting, default 3s, 0 = instant) before the exit ladder
  starts. While confirming, the monitor re-checks every 0.5s with forced
  quote refreshes and reports `sl-confirming (Ns)` in its health.
- **Deep-breach override** — a fair value ≥25% of the TP-SL span past the
  stop fires immediately; so does a *raw* microprice that deep when the
  quote is quality (half-spread ≤10% of the span): a tight market
  printing a crash is real, don't wait for the filter or the dwell.
- **Hysteresis** — the confirmation timer only resets once the fair value
  recovers 2% of the span back above the stop, so a value oscillating
  exactly on the line can't reset the clock forever.

The ACCOUNT page's EXIT QUALITY panel tracks realized stop slippage
(specified SL vs actual exit) so you can verify the cap holds in practice.

### SYSTEM page & lifecycle journal

- **State** — live health of every subsystem: market data (source + stream
  age), subscriptions, broker/account, trading stream, DB (engine +
  latency), redis, the exit enforcer (active monitors, unresolved ghost
  orders), the reconcile loop with its last-run age, the signal feeds with
  per-feed ages, and the event bus with its drop count.
- **Feed / API settings** — runtime knobs persisted in the DB:
  positions/account poll cadences (applied live) and the Alpaca
  stock/option feed tiers (IEX/SIP, INDICATIVE/OPRA — applied at next
  restart, flagged ↻).

Every event that reaches the plan FSM — including DROPPED ones (illegal
in state, lost compare-and-set, stale order guard) — is journaled to an
append-only `plan_events` table in the SAME transaction as the state
change, and served per plan at `/api/positions/{id}/events`: the audit
trail for "why did this position do that". Exit execution is additionally
serialized per plan (a manual close racing a monitor trigger queues
instead of interleaving orders), and the reconcile loop runs under the
same supervisor as the market streams, so a fatal error restarts it with
backoff instead of silently ending truth-sync.

### Portfolio risk (account page)

`GET /api/account/risk` + a PORTFOLIO RISK panel on the account page:

- **AT RISK @ SL** — account $ and % lost if every open position exits at
  its stop, shown as a stat card, per-position, and per-underlying.
- **Correlation-adjusted risk** — `sqrt(r'ρr)` over pairwise daily-return
  correlations of the open underlyings (60d): three correlated index ETF
  positions are priced as ~one bet, not three. Unknown correlations
  default to ρ=1 (no diversification credit), and the panel says when
  history was unreachable. Plus a concentration readout and the full
  correlation matrix (|ρ|>0.7 highlighted).
- **Factor exposures** — aggregate net Δ$, SPY-beta-weighted Δ$ (each
  underlying's empirical 60d beta), vega per vol point, theta per trading
  day, and rho: P/L per +1% interest rates, alongside each underlying's
  empirical correlation to daily 10y-yield changes (^TNX).

## Safety

- v1 is hard-locked to paper trading (`ALPACA_PAPER=false` refuses to boot).
- Every plan carries a server-enforced exit plan. Bracketless plans (no
  TP/SL) are legal only under a strategy bounded by its allocation and
  circuit breaker; a hard time stop still applies.
- Exits may only be tightened after entry, never widened.
- Daily loss circuit breaker blocks new entries.
- MFT leak-pluggers, all server-enforced: stale/absent market data blocks
  entries; per-leg half-spread cap rejects illiquid structures; PDT guard
  on sub-$25k accounts; overtrading breaker (max trades/day); duplicate
  guard against double-submits; unfilled entries auto-cancel after a TTL
  instead of chasing stale prices.
- All lifecycle mutations flow through the FSM; there is no code path that
  sets a plan's status directly.

### Execution reliability (flaky-broker hardening)

Real market APIs time out, drop connections, and lose events. The execution
stack assumes that:

- **Nothing can hang.** Every broker REST call has a socket-level timeout
  (10s, stamped onto the SDK session, which ships with none) plus a
  coroutine-level ceiling (15s `wait_for`) — a wedged API can never freeze
  an exit monitor. Reads/cancels retry transient failures (timeout,
  connection drop, 429/5xx) with backoff.
- **Submits are idempotent.** Every order carries a deterministic
  `client_order_id` (entry: `<plan>-e`; exits: one per escalation rung).
  An ambiguous failure (timed out but maybe landed) recovers the SAME
  order by client id instead of double-submitting. "Ghost" exits — submits
  that errored but landed late — are found by their key, then cancelled if
  live or adopted-and-closed if filled; a sweep on close cancels any
  stragglers so no stray closing order can fill into a reversed position.
- **Fills don't depend on the stream.** The TradingStream is the fast
  path; a periodic REST reconcile (45s) is the truth-sync that catches
  fills landing during stream gaps, re-arms any open plan missing its
  monitor, and closes exiting plans straight from order status. The exit
  verify loop reads REST directly too.
- **Stale verdicts can't clobber live orders.** Events about a specific
  order (exit dead/filled) are guarded on that order id at the CAS layer —
  a reconcile pass that observed rung N cannot wipe rung N+1's live order
  (the exiting→exiting self-loop makes a status-only CAS blind to this).
- **Vanished positions resolve.** If closes keep getting rejected and the
  broker reports no remaining position (expiry liquidation, manual close in
  their UI), the plan is force-closed rather than resubmitting forever.
- **The DB can't stall order management.** SQLite fallback runs WAL +
  NullPool (pool exhaustion under concurrent monitors was reproducible);
  Postgres gets a bounded pool with a fast timeout.

All of this is pressure-tested in `backend/tests/test_chaos.py`: a flaky
broker fake (latency, transient errors, hung submits that land late, lost
fill events, double-close rejection) driven through the REAL enforcer/FSM,
including a 12-plan concurrent storm asserting every plan closes exactly
once with the right exit reason. Two real bugs were found and fixed by this
harness before it ever met a live market.
