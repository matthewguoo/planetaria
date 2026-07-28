# planetaria

A disciplined 0-3 DTE options trading terminal: FastAPI backend against Alpaca
(paper), React/canvas frontend. The core design rule: **every position has a
server-enforced exit plan** (TP / SL / hard time stop) that survives restarts.

## Architecture

```
frontend (Vite/React, canvas chart + payoff designer)
   │  REST /api/*        WebSocket /ws/stream (snapshot-then-stream)
backend (FastAPI, single process)
   ├─ MarketDataService   one stock + one option stream, ref-counted subs,
   │                      background REST backfill, reconnect gap-fill
   ├─ ChainService        0-3 DTE chains, IV solve/interpolation, 5s cache
   ├─ TradeService        validated placement -> DB plan -> Alpaca order
   ├─ PlanStateMachine    FIX-style FSM governing every plan's lifecycle
   ├─ ExitEnforcer        quote-driven TP/SL + time stops; escalation ladder
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

Copy `.env.example` to `.env` and add Alpaca **paper** keys. Without keys the
app runs on a clearly-badged synthetic demo feed so the whole UI is testable.

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
