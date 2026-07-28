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
is unavailable. The heatmap, TP/SL contours, and hover P/L all share this
pricing function, and the expiry payoff itself is intrinsic
value — model-free — so the surface converges to exact P/L at expiry.
Remaining known assumptions (risk-neutral drift, no jumps, no early
assignment) are surfaced in the probability panel's assumptions note.

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

## Safety

- v1 is hard-locked to paper trading (`ALPACA_PAPER=false` refuses to boot).
- Orders without TP, SL, and a time stop are rejected server-side.
- Exits may only be tightened after entry, never widened.
- Daily loss circuit breaker blocks new entries.
- All lifecycle mutations flow through the FSM; there is no code path that
  sets a plan's status directly.
