# Discretionary edge, day one: four specs

Scope: features that give the *manual* trader an edge the moment the live
server is up, without opening a single automated entry path. The gate in
`docs/live-server.md` is about entries; everything here works on the
**exit/execution side of a position the human opened**, or produces
**information** before the human clicks. Nothing below can originate an
order. Each spec names the real seams in the code it hooks into.

Styles served: (a) short in-and-out intraday trades (0–3 DTE options,
leveraged-ETF scalps) and (b) 1–3 day holds. Presets are given per style.

Build order (edge per hour of work): **1 → 2 → 4 → 3**. Stepped stops and
the walk-in are pure execution-quality wins measurable on the existing
`exec_quality` ledger from the first trade; the OI/volume rail is a cheap
data+drawing job that also feeds spec 3; the LLM preflight is the biggest
build and needs the other three's data to say anything a rule can't.

---

## 1. Stepped stops (breakeven ratchet + vol/R trail)

### 1.1 What it does
A plan carries a **stop policy**. As the position moves in its favor the
enforcer *tightens* the SL on a schedule — to breakeven at +1R, then
trailing a fixed distance behind the high-water mark. It never widens. It
runs inside the existing per-plan `_monitor` task, so the stop it moves is
the one the enforcer already fires.

### 1.2 Definitions
- **R** = initial risk per unit at fill: `initial_r = |fill_premium − sl_premium_at_fill|`. Persisted at fill time (not recomputed — a later tighten must not shrink R).
- **fv** = the Kalman fair value the monitor already computes (`exit_enforcer.py:972-977`), never raw micro. A trail keyed on raw prints would chase spread noise.
- **high_water** = max fv seen since fill (persisted; see 1.4).
- **σ_hold** = expected move over the remaining hold. Equity: `daily_sigma × sqrt(hold_days)` from realized vol; options: the underlying's σ projected through the position (the same math as `suggestSlPctFromUnderlying`, `frontend/src/lib/analytics.ts:29-54`). **Server-side realized vol does not exist today** (only `frontend/src/lib/indicators.ts:109`) — add `app/services/vol.py: realized_vol(bars, lookback, tf_minutes)` ported 1:1 from the frontend, fed by `BarStore.get_bars`.

### 1.3 Policy shape
New JSON column `trade_plans.stop_policy` (migration `0009_stop_policy.py`, `down_revision="0008"`, `batch_alter_table` idiom from `0007`). `notes` is Text and not in `to_dict()`; `exec_quality` is the wrong home. Shape:

```jsonc
{
  "kind": "stepped",
  "steps": [                       // evaluated in order, each fires once
    {"at_r": 1.0, "sl_to": {"r": 0.0}},          // breakeven at +1R
    {"at_r": 1.5, "sl_to": {"r": 0.5}},          // lock +0.5R at +1.5R
    {"at_r": 2.0, "trail": {"r": 0.75}}          // from +2R, trail 0.75R behind high-water
  ],
  "trail_unit": "r" | "sigma",     // sigma: distance = k * σ_hold instead of R multiples
  "sigma_k": 1.5,
  "eval": "tick" | "bar_close_5m", // swing preset uses bar closes: no intraday-noise ratchets
  "min_step_ticks": 1,             // ignore sub-tick moves
  "cooldown_s": 5,                 // at most one ratchet per cooldown
  "state": {                       // written by the engine, survives restart
    "initial_r": 0.42, "high_water": 1.71, "fired": [0, 1], "trailing": true,
    "last_step_ts": "2026-09-03T14:31:05Z"
  }
}
```

**Presets** (UI picker, stored expanded so a preset change later never silently rewrites open plans):

| preset | style | steps |
|---|---|---|
| `NONE` | — | today's behaviour |
| `BE@1R` | either | breakeven at +1R, nothing else |
| `SCALP` | intraday | BE at +1R · +0.5R locked at +1.5R · trail 0.75R from +2R · `eval=tick`, `cooldown_s=3` |
| `SWING` | 1–3 day | BE at +1R **evaluated on 5m closes** · trail `1.5·σ_daily·sqrt(remaining days)` from +1.5R · `eval=bar_close_5m` |

### 1.4 Engine
In `_monitor` (`exit_enforcer.py:727-1065`), after the fv update and before the TP/SL trigger sites (`:989`), add one call:

```python
new_sl = step_stop(plan, fv, now, sigma_hold)   # pure function, app/services/stop_policy.py
if new_sl is not None:
    await self._apply_stop_step(plan, new_sl, step_detail)
```

`step_stop` is a **pure function** over `(policy, fill_premium, sl_premium, fv, now)` returning `(new_sl, new_state, detail) | None`. Rules it enforces: monotone (only up on the signed-premium axis, matching `tighten_exits`), `new_sl < effective TP − 1 tick`, respects `min_step_ticks` and `cooldown_s`, `eval=bar_close_5m` only fires when the 5m bar timestamp advanced. High-water updates every evaluation; steps fire on `(fv − fill) / initial_r ≥ at_r`.

`_apply_stop_step` = the existing tighten path with journaling:
1. Take `self._exit_locks[plan_id]` non-blocking; if held (exit in flight) skip this tick — the same rule `tighten_exits` applies (`:1394-1396`).
2. `await self.trade._update_plan(plan_id, sl_premium=new_sl, stop_policy=policy_with_state)`.
3. **Journal it.** Today `PlanStateMachine.update_fields` (`plan_fsm.py:236-260`) writes no `plan_events` row — a manual tighten leaves no trail either. Add `journal: tuple[PlanEvent, str] | None` to `update_fields` and a `PlanEvent.STOP_STEPPED` (and use it from `tighten_exits` too, as `STOP_TIGHTENED`). The lifecycle journal in `ChartHud` then shows `stop → BE 1.29 (+1.0R)`.
4. Broadcast rides on `_update_plan` (`plan_fsm.py:259`); the monitor re-reads `plan` on the `"plans"` topic, so the new SL is live on the next iteration. Add a vox cue `"stop moved to breakeven"` (existing audio bus).
5. Resting TP is untouched (SL is software-enforced; only `tp_premium` changes touch the broker).

Restart: `state` is on the row, so `_reconcile_plan` → `arm` (`:365`) resumes with the same high-water and fired steps. Nothing about the policy lives in monitor-local variables.

Adopted positions: `adopt_positions` accepts a `stop_policy`; `initial_r` seeds from `entry − sl` at adoption. This is how the four Roth ETFs get a ratchet on day one.

### 1.5 API / UI
- `OrderIn.stop_policy: dict | None` (validated against the shape; presets expanded server-side from `app/services/stop_policy.py: PRESETS`). `AdoptIn` gets the same. `PATCH /api/positions/{id}/exits` accepts `stop_policy` (replace; `state` is never client-writable).
- Ticket: a 4-way `STOP` selector (NONE / BE@1R / SCALP / SWING) next to the SL row in both `OrderPanel` and `EquityTicket`; the confirm summary restates it ("BE at +1R, trail 0.75R from +2R").
- Chart: the SL line already redraws from the plan; add a small `BE`/`TRAIL` tag on the SL badge when `state.fired` is non-empty (`drawExitLevels`, `CandlePane.tsx:1591-1650`).
- Positions drawer: a `+1.3R · BE` cell per plan (R progress and current step).

### 1.6 Safety and failure modes
- Only tightens. A bug can only stop you out earlier — bounded, never a wider loss.
- Skips while the exit lock is held; never races the ladder.
- Trail on fv, with hysteresis inherited from the SL trigger: a one-tick wick cannot ratchet.
- Swing preset evaluates on bar closes so an overnight synthetic quote (10bps half-spread from the last 1m bar, `:1080-1111`) cannot ratchet in the dark.
- Live server: no new order path — the SL still fires through the existing exit ladder.

### 1.7 Tests
`test_stop_policy.py`: table-driven `step_stop` cases (BE fires at exactly +1R; not at +0.99R; monotone; cooldown; min tick; TP ceiling; bar-close gating; trail follows high-water and never retreats). `test_exit_enforcer` addition: fake quotes drive a monitor through the SCALP preset, assert `sl_premium` sequence and journal rows; disarm/re-arm mid-trail resumes from persisted state.

---

## 2. Entry walk-in ladder

### 2.1 What it does
Instead of one DAY limit at a single price, the entry **starts at the mid
and walks toward the natural price on a schedule**, never past the limit
the human confirmed. The exit side already does exactly this
(`ESCALATION` at `exit_enforcer.py:34-38`: mid ± 2% → ± 6% → market); the
entry side today is a single order with a 5-minute TTL (`entry_ttl_min`,
`risk.py:28`, enforced at `exit_enforcer.py:816-828`).

Why it pays: options tickets submit at **mid** (`useDesigner.ts:103`) and
then sit; equity tickets submit at the **natural** (`EquityTicket.tsx:124-126`)
and pay the whole half-spread instantly. `InstantFillRow` already shows
that give-up (`OrderPanel.tsx:21-49`). The ladder captures the part of the
spread that patience buys, and `exec_quality.entry.spread_capture` (which
is written today, `trade_service.py:648-654`) measures it per fill.

### 2.2 Policy shape
`trade_plans.entry_policy` JSON (same migration as 1.3):

```jsonc
{
  "kind": "walk_in",
  "rungs": [                        // offset in half-spreads from the CURRENT mid at rung time
    {"after_s": 0,  "offset_hs": 0.0},
    {"after_s": 8,  "offset_hs": 0.5},
    {"after_s": 20, "offset_hs": 1.0}   // = natural
  ],
  "cap": 1.15,                      // the user's confirmed entry_limit; never exceeded (signed, +debit/−credit)
  "partial": "hold" | "cancel_rest",// on a partial fill: stop walking and hold the rung, or cancel the remainder
  "skip_if_hs_ticks_lte": 1,        // liquid names (1-tick spread): submit at natural, no ladder
  "state": {"rung": 1, "rung_ts": "...", "order_ids": ["…-e0", "…-e1"], "mids": [1.10, 1.11]}
}
```

Presets: `SCALP`: 0s/6s/15s (speed matters more than a cent); `SWING`:
0s/20s/60s then hold at natural until TTL; `PATIENT`: 0s/30s/90s, cap at mid+0.5hs (may not fill — for entries where price matters more than getting in).

### 2.3 Engine
Runs in the monitor's `submitted` branch (today only the TTL check lives there), as a small state machine persisted in `entry_policy.state` so a restart resumes at the right rung:

1. Rung 0 is the normal `_submit_entry` with `client_order_id = f"{plan.id}-e0"`.
2. When `now − rung_ts ≥ next.after_s`: re-read plan; if not `submitted` stop. Recompute `mid, half_spread` from `position_quote_stats` (the same numbers `_quality_snapshot` uses). `limit = clamp(mid + offset_hs × half_spread, cap)` on the signed axis; if `limit` equals the current order's limit (market moved *toward* us) skip the rung.
3. Cancel the current entry (`cancel_entry`), **poll it to a terminal verdict** exactly as the TP takedown does (`cancel_resting_tp`) — a fill that beats the cancel is the fill, not a reprice. If `partially_filled`, apply `partial`.
4. Submit the next rung with `client_order_id = f"{plan.id}-e{rung}"` via `_submit_idempotent` (crash between accept and DB write recovers the same order). Register the key in a new `_entry_ghost_keys[plan_id]` first, resolve after the id lands — the same three-layer double-fill protection the exit ladder has (`_handle_ghost`, `:396-432`).
5. Record `exec_quality.entry.rungs[] = {rung, fair, half_spread, limit, ts}`; the fill attributes to the rung it landed on.

**FSM change (required):** `ENTRY_SUBMITTED` has no self-loop (`plan_fsm.py:76-98`; only `EXIT_SUBMITTED` does, `:89`). Add `E.ENTRY_SUBMITTED: {S.PLANNED: S.SUBMITTED, S.SUBMITTED: S.SUBMITTED}` and pass `guard={"entry_order_id": previous_id}` so a stale reprice cannot clobber a newer one. `on_trade_update` currently skips the guard for entries ("entry_order_id never changes", `trade_service.py:850`) — it must now guard on the order id in the update, and ignore fills for superseded rung ids **unless** they are fills (a superseded rung that filled is the position).

TTL: unchanged, still cancels the whole thing from `created_at`. Session: only walk during a session the order can trade in (`_session_open`, `:1113-1120`); outside it the order rests at rung 0.

### 2.4 UI
- Ticket: `ENTRY` selector (NOW / SCALP / SWING / PATIENT) next to the limit; the limit field becomes the **cap**. `InstantFillRow` shows `walk mid → nat over 15s · cap 1.15`.
- Equity ticket: default switches from natural to `SCALP` walk-in for names with `half_spread > 1 tick`; 1-tick names submit at natural as today (the skip rule).
- Positions drawer while `submitted`: `rung 2/3 @ 1.12` with a countdown; the lifecycle journal gets one `ENTRY_REPRICED` row per rung.
- Chart: the entry line (`drawHeatmap` vline `ENTRY`) moves with the rung.

### 2.5 Safety
- Never beyond the cap the human confirmed; the confirm overlay states the cap.
- Cancel-verify before resubmit: the same terminal-verdict discipline as the TP takedown; no rung is submitted while the previous is `pending_cancel`.
- Idempotent ids per rung; ghost keys; guard on `entry_order_id`.
- Live server: this re-prices an entry the human already confirmed; it cannot create one. Still refused: `strategy_id`, level-2 shapes.

### 2.6 Tests
Pure `next_rung(policy, state, mid, hs, now)`; FSM self-loop + guard; a fake broker that fills during a cancel (fill wins, no second order); partial-fill `hold` vs `cancel_rest`; restart mid-ladder resumes the rung; 1-tick skip; `exec_quality.entry.rungs` shape.

---

## 3. LLM pre-trade research: warnings only

### 3.1 Contract
A staged trade (the ticket's payload before submit) goes to
`POST /api/research/preflight`; back comes **only warnings**:

```jsonc
{"warnings": [{"severity": "info|caution|danger", "code": "earnings_48h", "text": "...", "evidence": {...}}],
 "checked": ["earnings", "news", "vol", "liquidity", "structure", "account", "session"],
 "llm": {"model": "...", "latency_ms": 4100, "signal_id": 8812} | null}
```

Hard rules: the endpoint is read-only; the response never carries a
price, size, side, or "go" — the server does not accept any field of the
LLM output into `place_trade`, and the UI renders warnings as text in the
existing `designer.warnings` / `reasons` strips (`OrderPanel.tsx:307-311`,
`EquityTicket.tsx:389-395`). A `danger` warning adds one extra acknowledge
click on the confirm overlay; it does not block. A timeout renders
`research unavailable` and the ticket proceeds — research can never hold a
human's order hostage.

### 3.2 Two layers: rules first, LLM second
**Layer A — deterministic checks (free, <200 ms, always run).** Assembled from what the server already has (`app.state.market/chain/trade/risk/portfolio_risk`, `SignalStore`):

| code | source | fires when |
|---|---|---|
| `stale_quote` | `market.equity_tape_age_s`, `stream_age_s` | quote older than N s for the leg(s) |
| `wide_spread` | `position_quote_stats` | half-spread > x% of premium (the illiquidity gate's number, surfaced early) |
| `stop_inside_noise` | σ_hold (spec 1) vs SL distance | the "inside Nd noise" check the equity ticket does client-side, now for options too |
| `earnings_48h` | Finnhub `/calendar/earnings?symbol=` (**new** `next_earnings(symbol)`, daily cache — today only `reporters_for(date)` exists, `store.py:97-109`, a 2-day window keyed by date) | earnings inside the hold |
| `edgar_8k_48h` / `news_burst` | `SignalStore.recent(type="news", symbol=…)` | fresh 8-K, or > k headlines in 24h |
| `leveraged_etf` | asset name from `/v2/assets` (already fetched once for the Roth) | "2X"/"Daily Target"/"-1x" in the name → daily-reset decay warning with hold length |
| `dte_pin` / `early_assign` / `overnight_gap` | the strings `useDesigner` already builds (`:135-158`), moved server-side so equity and adoption get them too |
| `oi_wall_between` | spec 4 `levels` | a call/put wall or gamma flip sits between entry and TP (or just past the SL) |
| `session_edge` | broker clock | inside the first/last 5 minutes; auction entries near the OPG cutoff |
| `account_daytrades` / `daily_loss_used` / `correlated_open` | `trade.get_account`, `risk.todays_realized_pnl`, `portfolio_risk.snapshot` | PDT count, % of daily cap used, β/ρ to an open position on the same underlying |

**Layer B — the LLM, over the packet, not the web.** One `LLMAnalyst.analyze()` call (`llm.py:173-187`) with `task = "What is the single most likely reason this specific trade loses? Return warnings only."`, `data = the Layer-A packet + last 10 headlines (untrusted, inside <data>)`, and a strict schema (`warnings[]` with `severity/code/text/evidence`, `max 3`). `effort="low"`, `max_tokens=600`, `timeout_s=15`. Its value over Layer A is *synthesis* ("you are long a 2x AVGO ETF into NVDA's print with a stop inside one day's noise") — so it only runs when Layer A produced ≥1 warning or the user pressed RESEARCH explicitly; a clean packet gets no LLM call (cost, latency).

Construction: `LLMAnalyst` is built only inside `StrategyRunner` today (`strategy_runner.py:185`), which the live server never constructs. Add `app.state.research = LLMAnalyst(settings, signal_store_or_none)` in bootstrap **before** the strategy-plane cutoff — read-only, allowed on live. Backend `api` (key), not `claude-cli` (4–19 s + 33k-token overhead per call, `llm.py:82-90`).

Cache key `(symbols, structure hash, 5 min)`; journal every call as an `analysis` event (`_journal`, `:259-287`) so warnings can be scored against outcomes later — *did warned trades lose more?* That score is the feature's own edge test.

### 3.3 UI
- Auto-runs Layer A on every designer/ticket change (debounced 500 ms); Layer B on the confirm step or a `RESEARCH` button. A spinner on the confirm button for ≤15 s, then proceed regardless.
- Warnings strip: `⚠ code — text` with severity colour; hover shows evidence. `danger` → confirm overlay gains "I've read the warnings" checkbox.

### 3.4 Prompt-injection posture
Headlines and 8-K text are untrusted; they only ever appear inside `<data>` under `HARDENED_SYSTEM` (`llm.py:42-50`), and the output schema has no free-form field that reaches an order. This is the same posture the PEAD strategy already uses in production.

---

## 4. Options volume / OI overlays on the price axis

### 4.1 What it does
For the selected expiry (and optionally all ≤ `dte_max`), draw per-strike
**open interest and today's volume** as horizontal bars anchored at the
right edge of the plot, calls one colour, puts another, plus axis badges
for the derived levels: largest call wall, largest put wall, **max pain**,
and the **gamma flip** — the levels that behave as magnets/walls in the
0–3 DTE names this terminal trades. Same badge idiom as TP/SL/BE
(`drawLevel`, `CandlePane.tsx:1591-1650`).

### 4.2 Data — a second fetch path
The chain endpoint (`options_chain.py`) is built from
`OptionHistoricalDataClient.get_option_chain` **snapshots**, which carry
quote/IV/greeks and **no OI or volume**; `gamma` is fetched but dropped on
the wire (`_contract_row` keeps it, the frontend `Contract` type doesn't,
`strategyStore.ts:24-35`). OI lives on the *trading* API's contracts
(`OptionContract.open_interest / open_interest_date`, vendored SDK
`alpaca/trading/models.py:667-668`); volume comes from option **daily bars**
(`get_option_bars`, batch of symbols, 1 request per ~100 contracts).

New `ChainService.get_strike_profile(underlying, expiry | None)`:
- OI: `trading.get_option_contracts(underlying, expiry window)` — paginated 100/page, so 2–3 calls per underlying; **cache 1 h** (OI is a once-daily figure).
- Volume: `option_data.get_option_bars(symbols, Day, start=today)` — 1–2 calls; **cache 5 min**.
- Both through `alpaca.call(..., retries=1)` (idempotent reads). Budget: well inside 200 req/min; single-flight coalescing exactly like `_cache/_inflight` (`:55-78`).
- Wire: extend the chain envelope with

```jsonc
"strike_profile": [{"strike": 765, "call_oi": 12400, "put_oi": 8100, "call_vol": 3300, "put_vol": 5100, "gamma_call": 0.031, "gamma_put": 0.029}],
"levels": {"call_wall": 770, "put_wall": 760, "max_pain": 765, "gamma_flip": 763.5, "asof": "...", "oi_date": "2026-09-02"}
```

Derivations (server, pure functions in `app/services/strike_profile.py`):
- `call_wall` / `put_wall`: argmax OI within ±3% of spot.
- `max_pain`: strike minimising Σ intrinsic × OI over calls+puts (standard).
- `gamma_flip`: net dealer gamma `Σ γ·OI·100·spot²·0.01·(calls − puts)` per strike, cumulative sign change nearest spot. **Label as an approximation**: it assumes the usual dealer-short-calls/long-puts convention; the UI tooltip says so.

### 4.3 Drawing
- Toggle: add `oi` to `IndicatorToggles` (`tradingStore.ts:40-45`) and a `TOGGLES` row `{key:"oi", label:"OI", title:"Open interest / volume by strike + walls"}` (`ChartHud.tsx:34-41`). Equity mode keeps it available (an ETF has a chain).
- In `render` after `drawStrikes`, before `drawExitLevels`: for each profile strike with `y = priceToY(strike)` inside `[0, volTop]`, draw two bars from `x = layout.plotW − w` to `layout.plotW` (calls above the strike line, puts below, 3 px each), `w = min(0.12 × plotW, oi / maxOi × 0.12 × plotW)`; volume as a brighter inner bar of the same scale. Alpha 0.35 so candles stay readable.
- Levels via the existing `drawLevel` idiom with tags `CW`, `PW`, `MP`, `GF` in a muted colour; off-scale clamp with arrows exactly as TP/SL.
- Hover (existing crosshair path): when the mouse is within 6 px of a strike row, the tooltip appends `OI 12.4k/8.1k · vol 3.3k/5.1k`.
- Data flow: `loadChain` already polls; the profile piggybacks on the same response, so no new poll. Draw is synchronous over a ~40-strike array — trivial per frame.

### 4.4 How it feeds the others
- Spec 1: the `SWING` preset's first target can snap to the nearest wall (opt-in).
- Spec 3: `oi_wall_between` and "stop just below the put wall" warnings.
- Positions: the drawer shows distance to the nearest wall per open plan.

### 4.5 Tests
Pure-function tests for max pain / walls / gamma flip on a hand-built profile; ChainService cache/TTL behaviour with a fake client; a wire-shape test. Drawing is verified by the browser workflow (toggle on, badges present, tooltip text).

---

## Cross-cutting

- **Migrations:** one, `0009_plan_policies.py`, adding `stop_policy` and `entry_policy` JSON columns (nullable). No backfill.
- **Journal:** `PlanEvent.STOP_STEPPED`, `STOP_TIGHTENED`, `ENTRY_REPRICED`; `update_fields(journal=…)`. This also fixes the existing gap where manual tightens leave no `plan_events` row.
- **Measuring the edge:** every one of these lands in data the system already keeps — `exec_quality.entry.spread_capture` (walk-in), realized P/L per plan vs the same plan's "no-ratchet" counterfactual (log the would-have-been SL fill in `stop_policy.state.counterfactual_exit`), the `analysis` journal vs outcomes (preflight). Add a `RESEARCH` tab to the fund page later; the numbers exist from trade one.
- **Live gate:** none of the four adds an entry path. Spec 2 re-prices within a human-confirmed cap; specs 1 and 4 are exit-side/information; spec 3 is read-only by construction.
