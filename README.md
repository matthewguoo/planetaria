# planetaria

A FastAPI backend against Alpaca (paper) with two React front ends over the
same API. The core design rule: **every position has a server-enforced exit
plan** (TP / SL / hard time stop) that survives restarts.

| entry | what it is |
|---|---|
| `/` | **ops console** — broker connection, account, and the strategy runners. Kind-agnostic: a new strategy appears here by being registered in `app/strategies/__init__.py`, with no frontend change. |
| `/terminal.html` | **options terminal** — the discretionary 0-3 DTE cockpit: chart, chain, payoff designer, sizing, order ticket. |

Neither front end holds a privileged path; both are clients of the same
`/api/*` routes the headless engine exposes.

`research/` holds studies — code that produces evidence, kept out of
`backend/` so the dependency runs one way: research reads the app, the app
never reads research. See [research/README.md](research/README.md).

## Screenshots

`docs/screenshots/` holds a phone + desktop walkthrough. Regenerate against
a running app with `node scripts/screenshots.mjs` from `frontend/` (needs
`npm i -D playwright && npx playwright install chromium`, or set
`PW_CHROMIUM` to an existing Chromium binary).

## Architecture

```
frontend (Vite/React — ops console at /, options terminal at /terminal.html)
   │  REST /api/*        WebSocket /ws/stream (snapshot-then-stream)
backend (FastAPI, single process)
   ├─ MarketDataService   one stock + one option stream, ref-counted subs,
   │                      background REST backfill, reconnect gap-fill
   ├─ ChainService        0-3 DTE chains, IV solve/interpolation, 5s cache
   ├─ TradeService        validated placement -> DB plan -> Alpaca order
   ├─ PlanStateMachine    FIX-style FSM governing every plan's lifecycle
   ├─ ExitEnforcer        bracket engine: TP rests AT THE BROKER as a live
   │                      limit; SL is software with a trigger hierarchy
   │                      (option ticks -> underlying-tick model checks ->
   │                      adaptive REST polling); escalation ladder
   │                      (limit @ mid-2% -> mid-6% -> market -> verify loop)
   ├─ RiskService         per-trade max loss, BP cap, max positions,
   │                      daily circuit breaker — server-side, not advisory
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
surfaced as **UNTRACKED** in the positions drawer and can be **adopted**:
grouped per underlying into a managed plan with TP/SL/time-stop enforcement.

### Account dashboard & position views

The ACCOUNT view is the broker dashboard for the connected (paper) account:
equity/cash/buying-power/day-P&L cards, the Alpaca equity curve
(1D/1W/1M/3M/1A), all positions (managed + untracked, with adopt/close),
live open orders (with cancel), and closed-trade history. Clicking any
position opens it ON THE CHART as a read-only position view: the P/L
surface anchors at the ENTRY bar (spanning entry -> expiry over the actual
price path and into the future), P/L is measured against the actual fill
premium, and the plan's TP/SL contours and time stop render as its real
boundaries. A toggle switches the basis between **ENTRY PROJ** (leg IVs
frozen from the fill — the projection you signed up for) and **LIVE
GREEKS** (legs re-marked from the latest chain smile, scenario shocks
apply — what it's worth now).

### Strategy presets

All common single-expiry structures are built in: long call/put, debit and
credit verticals, straddles/strangles (long and short), iron condor, iron
butterfly, call/put butterflies, cash-secured put. Presets are declarative
leg templates (strike offsets from ATM). Everything is edited ON the chart:
strikes are horizontal lines dragged directly or via handles on the vertical
rail, per-leg contract ratios via the −/+ zones on each strike chip, the
TP and SL exits by dragging their premium contour lines (synced with the %
inputs in the ORDER panel), and the force-exit time by dragging the TIME
STOP vertical. The region past the time stop is dimmed — a dead zone the
exit enforcer never lets the position reach. Every edit re-prices the P/L
heatmap and its contours live. The chart scales both ways: wheel/drag on
the price axis for vertical scale, vertical chart drag to pan price, and
double-click to restore auto-fit.

### Model accuracy beyond BSM

Plain BSM freezes each leg's IV, which misprices scenarios where the
underlying moves (the smile moves with spot in reality). Scenario pricing
here is **smile-aware**: leg IVs are re-read from the live chain smile
under a sticky-moneyness assumption (IV as a function of K/S) as the
scenario price moves, degrading gracefully to frozen-IV BSM when the smile
is unavailable. On top of that:

- **IV shock** control: a parallel relative vol shock (±50%) applied to
  every scenario vol — stress vega ("what if IV crushes after the event?")
  across the surface, contours, and simulation together.
- **Skew beta**: a directional vol response derived from the chain's OWN
  skew slope (dIV/dlnK near ATM), so selloffs raise scenario vols and
  rallies crush them — the empirical index behavior pure smile-riding
  misses. Toggleable.
- **Monte Carlo exit simulator** (worker, 2000 seeded GBM paths in trading
  time): applies the EXACT enforcer exit rules along each path — TP as a
  limit fill at the threshold, SL at the first observed breach including
  gap-through (realizing the worse price), hard time stop, expiry payoff —
  net of round-trip bid/ask friction. Reports realized-P/L EV, win rate,
  exit mix (TP/SL/time), percentiles, and average time in trade. This is
  the path-dependent truth the path-independent surface cannot show.
- **Friction accounting**: per-leg half-spreads (from live quotes) charged
  in and out; shown in sizing and baked into the MC distribution.
- **Discrete-risk warnings**: early-assignment risk on short ITM legs with
  no extrinsic value, pin risk at short strikes into expiry, and overnight
  gap-risk disclosure on multi-day positions.

The heatmap, TP/SL contours, hover P/L, exit-drag mapping, and MC engine
all share one scenario pricing function, and the expiry payoff itself is
intrinsic value — model-free — so the surface converges to exact P/L at
expiry. Remaining assumptions (risk-neutral drift, diffusion-only paths)
stay disclosed in the probability panel's assumptions note.

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
16 minutes). Without keys the app still shows **real prices**: a keyless
public feed (Yahoo chart API) supplies 1m history and a ~5s-polled live
quote, badged `PUBLIC DATA` in the header — the options chain is then
modeled from that real spot and trading stays disabled. Only if that
endpoint is unreachable does it drop to the synthetic random walk, badged
`DEMO DATA`.

## Tests

```bash
cd backend  && python -m pytest        # math core, FSM matrix, exits, adoption
cd frontend && npm test                # TS/Python parity + presets + sizing
cd frontend && npm run build           # typecheck + production build
```

### On-chart HUD, chain, and sim

The upper-left HUD lives on the chart itself: chips switch overlays
(HEAT · SIM · VWAP · EMA · BB), an IV% shock input and skew-β checkbox
drive the scenario model, and a stats line shows ATR / RV / IV / IV−RV.
With SIM on, the Monte Carlo exit simulator renders directly in the HUD —
EV after friction, win rate, exit-reason split (TP/SL/time/expiry),
P5·P50·P95 outcomes, average time-in-trade — alongside the analytic
P(profit), touch probabilities, and R:R. There is no separate probability
panel; everything reads in context over the candles.

The CHAIN toggle opens a live options chain for the active expiry
(calls | strike | puts, mid + IV per side, ATM row highlighted). Clicking
B or S on a contract adds it to the currently formulated position as a
long/short leg; clicking the same contract again stacks its ratio. Legs
compose freely across the preset templates (max 4, MLEG limit) — once
edited the strategy is tagged CUSTOM, and each leg can be removed from
the strategy panel. All strikes everywhere are the chain API's actual
tradeable contracts — rail drags and chain clicks snap to listed strikes
only, never interpolated prices.

### Phone layout

Below 640px a dedicated mobile shell (`frontend/src/components/Mobile/`)
replaces the desktop grid — the desktop files stay untouched. The candle
chart (with the full HUD, heatmap, contours, and drag interactions — the
canvas speaks pointer events, so touch drags strikes and TP/SL directly)
fills the screen under a slim symbol/price/status header and a
timeframe + zoom strip (+/−/FIT buttons reuse the desktop wheel/dblclick
paths via synthetic events). The dense data follows the
exchange-app pattern (Binance/TradingView mobile): the chart carries only
a chips legend, and a tab strip under it hosts SIM (Monte Carlo +
probabilities), THETA (templates + seller metrics), CHAIN (tap B/S to add
legs), and POS in a collapsible pane. A prominent pinned TRADE button
opens the full ticket sheet; ACCOUNT opens the dashboard. `?unlock` still
forces the desktop layout in a small window.

**Preview on your phone:** the app talks same-origin (`/api`, `/ws` are
proxied by the vite dev/preview server), so any host works:

```bash
cd frontend && npm run phone     # builds + serves on your LAN
# open http://<your-computer-ip>:4173 on the phone (same Wi-Fi)
```

or share it anywhere with a quick tunnel from your machine:

```bash
cloudflared tunnel --url http://localhost:4173
# open the https://…trycloudflare.com URL it prints
```

### Theta-sell system

The THETA chip in the HUD turns on the premium-seller's workspace:

- **Delta-targeted templates** — PUT/CALL credit spread 16Δ, iron condor
  16Δ and 25Δ, short strangle 16Δ. Short strikes are picked by |delta|
  from the LIVE chain (the way sellers actually choose strikes — by
  probability, not distance); wings go ~0.8% of spot further out on
  listed strikes. If the active expiry can't resolve (0DTE after the
  close has step-function deltas), the template rolls forward to the next
  expiry that can. One click builds the whole structure with standard
  mechanics pre-set: TP at 50% of the credit, stop at 100% of the credit
  (SL % field now accepts up to 300% for credit-style stops), time stop
  15:45 ET so nothing rides into the close.
- **Expected-move cone** — dashed ±1σ band from now to expiry drawn on
  the chart; short strikes inside the cone are the ones the market
  expects to touch. The HUD warns explicitly when a short strike sits
  inside the expected move.
- **Seller metrics** — credit per set, credit/width (the % of the wing
  you're paid — the seller's odds line), theta $/day per set, and the
  expected move in dollars. The MC simulator and P(profit) then price the
  exact TP-50%/SL-100%/time-stop mechanics path-dependently, net of
  spread friction — the honest number for whether the structure is worth
  selling after costs.

### Engine API & headless mode

The execution engine (streams → FSM → exit enforcer → reconcile → risk)
is architecturally separate from the UI-serving surface, and can run
alone: `HEADLESS=true` boots the full engine plus only its **command/ops
API** — no chain/bars endpoints, no browser WebSocket:

- Commands: `POST /api/orders`, `POST /api/positions/{id}/close`,
  `/flatten`, `PATCH /api/positions/{id}/exits` (tighten-only),
  `POST /api/positions/adopt`
- Ops: `GET /api/system/state`, `GET /api/positions/{id}/events`
  (lifecycle journal), `GET/PUT /api/settings/risk` and `/feed`,
  `GET /api/health`

This is the future split seam: run the engine supervised on an always-on
host (systemd/docker restart-always — restarts are safe, the reconcile
machinery rebuilds all monitors from the DB) and keep the UI wherever
you like. Until real capital or multi-user needs force a true two-process
split, one supervised process is the more reliable shape.

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
`/api/system/state`, and a NO-QUOTE warning in the SYSTEM menu.

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

The live conditionals are visible in the chart HUD: viewing a position
shows an ENFORCER block with monitor health, mark vs bracket, distance to
stop, whether TP is resting at the broker, and the last journal events.
The ACCOUNT tab's EXIT QUALITY panel tracks realized stop slippage
(specified SL vs actual exit) so you can verify the cap holds in practice.

### System menu & lifecycle journal

The ⚙ button (desktop and phone headers) opens the SYSTEM menu:

- **State** — live health of every subsystem: market data (source + stream
  age), subscriptions, broker/account, trading stream, DB (engine +
  latency), redis, the exit enforcer (active monitors, unresolved ghost
  orders), and the reconcile loop with its last-run age. Refreshes every
  5s while open.
- **Feed / API settings** — runtime knobs persisted in the DB: chain
  refresh, positions/account poll cadences, the keyless public feed's
  poll interval (all applied live), and the Alpaca stock/option feed
  tiers (IEX/SIP, INDICATIVE/OPRA — applied at next restart, flagged ↻).

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
  its stop, shown as a stat card, per-position (RISK% column in both
  positions tables and the ACCT RISK row in sizing before you place), and
  per-underlying.
- **Correlation-adjusted risk** — `sqrt(r'ρr)` over pairwise daily-return
  correlations of the open underlyings (60d, keyless public data): three
  correlated index ETF positions are priced as ~one bet, not three.
  Unknown correlations default to ρ=1 (no diversification credit), and
  the panel says when history was unreachable. Plus a concentration
  readout and the full correlation matrix (|ρ|>0.7 highlighted).
- **Factor exposures** — aggregate net Δ$, SPY-beta-weighted Δ$ (each
  underlying's empirical 60d beta), vega per vol point, theta per trading
  day, and rho: P/L per +1% interest rates, alongside each underlying's
  empirical correlation to daily 10y-yield changes (^TNX).

### Audio cues

MetaTrader/tastytrade-style trade audio, driven by real FSM transitions
pushed over the plans WebSocket channel: distinct WebAudio chimes plus
spoken announcements — "Order filled", "Partial fill", "Take profit",
"Stop loss", "Time stop", "Position closed" (profit and loss get different
chimes), "Order rejected/canceled", and "Connection lost/restored". Voice
clips are local synthesized WAVs (`frontend/public/audio/`, Piper neural TTS, deep male announcer) —
fully offline, nothing licensed. The header toggle cycles OFF → FX
(chimes only) → VOX (chimes + voice), persisted per browser. Snapshots on
reconnect prime state silently, so history is never replayed as audio;
manual closes don't announce (you just clicked them).

### Chart context (MFT layer)

Toggleable indicators (session-anchored VWAP, EMA 9/21, Bollinger 20×2σ)
plus an always-on readout of ATR(14), realized vol (30-bar, annualized per
timeframe), ATM implied vol, and the IV−RV spread — the "is premium rich
or cheap?" number, color-ticked. Model-free expiry breakevens render as
white dashed guides with BE price badges on the axis (basis-aware: a live
position's breakevens use its actual fill). The layout auto-sizes fluidly
down to small laptop widths (panels re-flow 2×2).

## Safety

- v1 is hard-locked to paper trading (`ALPACA_PAPER=false` refuses to boot).
- Orders without TP, SL, and a time stop are rejected server-side.
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
