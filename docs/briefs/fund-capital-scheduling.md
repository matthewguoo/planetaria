# Fund capital scheduling & the registered performance plane

Written 2026-08-10, late. Matthew's directive from the evening's design
conversation: one chunk of capital should work many strategies across the
trading day and the overnight; connected accounts are typed by what they
permit (taxable margin vs Roth IRA); strategies are tagged by WHEN they
occupy capital; and the console should show, for the day's build, the
projected book (from registered backtests) beside the live record. This
brief specs all of it so the build starts from edge cases, not vibes.

## 0. What this buys (the evidence that already exists)

The stacked book is measured but currently inexpressible. From the
2026-08-10 handoff (§3) and its notes: the STACKED config — gff and
day2@09:32 sharing the same dollars, delayed arm on its own slice — prices
at ~13-14%/yr around Sharpe ~1.1. day2's entry shift to 09:32 was scored
"free" (`day2_shift` note, +3.3bp t 0.97 for the shift itself) precisely
because gff's dollars release at 09:31:30 and day2 wants them at 09:32.
The delayed arm's dollar is busy 17:30 → next 15:55 (`delayed_account`).
Today's engine gives every instance an exclusive static envelope whether
in-window or not, so the measured composition cannot be built. That — not
elegance — is the reason to do this.

## 1. Four new objects

1. **CapitalPool** — belongs to an account; carries a currency
   (`shares` | `options_bp`) and a dollar size. Instances subscribe to a
   pool instead of owning an envelope. Back-compat is structural: an
   instance without a pool gets a private pool sized to its current
   allocation — ship day changes nothing for the running fleet.
2. **Time envelope** — declared on the strategy class beside `requires`:
   - `window(entry_et, exit_et)` — both known a priori (gff, fly, day2).
   - `event_window(entry_span_et, worst_exit)` — entries arrive inside a
     span; capital is planned to the WORST-case exit (nosip: entries
     16:15-19:45, worst exit T+1 15:55).
   - `overnight(entry_after_et, exit_by_et_next)` — the "last N minutes
     in, first N minutes out" shape (delayed arm: 17:30 → next 15:55).
   - `fallback` — an intraday soaker for otherwise-idle hours. The tag
     exists in the schema; the fund is under NO obligation to fill it.
     Every soaker candidate tested so far is dead (mean reversion, GHLZ),
     idle capital costs nothing, and CAGR targets are outputs, not inputs.
3. **Reservation** — created at decision time for the sized amount,
   released on CONFIRMED FLAT. Never on the scheduled exit time (§2.1).
4. **Account capability vector** — what the instance-side `requires`
   frozenset joins against: `shorts`, `options_level`, `leverage`,
   `ah_orders` (yes/no/UNKNOWN), `spreads`, `recycle`
   (instant | settled), `pdt_bound`. The console already renders unmet
   `requires` as a capability gap; this makes the account side explicit
   instead of implied by whichever account is selected.

The current fleet as envelopes (the worked example):

| instance | envelope | pool currency |
|---|---|---|
| gff-1 | window 09:27:30 → 09:31:30 | shares |
| day2 (unbuilt, shelved) | window 09:32 → 15:55 | shares |
| fly-1 | window 13:55 → 15:50 | **options_bp** |
| nosip-1 | event_window 16:15-19:45 → T+1 15:55 | shares |
| delayed arm (unbuilt) | overnight 17:30 → next 15:55 | shares |

One $10k share pool can serve gff → day2 → nosip in sequence: that IS the
stacked book. The fly never competes — it spends a different currency
(options buying power, priced by `options_required_capital`,
strategy_runner.py:47).

## 2. Scheduler semantics — the four rules that keep it honest

1. **Release on confirmed flat, not on schedule.** The gff decade panel
   contains 112 events with no 09:31 print — the halt tail. If day2's
   09:32 sizing assumes gff's exit filled, a halted name double-spends the
   pool. A reservation releases when the plan is verifiably flat; a
   dependent strategy whose pool isn't free SHRINKS to what is free or
   SKIPS, and journals which one it did.
2. **Recycle latency is an account property.** Taxable margin recycles
   intraday instantly. The Roth's 1x limited margin should permit reusing
   unsettled proceeds without good-faith violations — believed, NOT
   verified, and it gates v2 (§4). `pdt_bound` matters only under $25k
   equity; carried as a flag anyway.
3. **Contested windows resolve by static, pre-registered priority** — an
   ordered list per pool, committed like any other registered choice.
   Reallocating toward whatever ran hot recently is a meta-strategy, and
   an unregistered one; the scheduler refuses it BY DESIGN, not by
   deferral.
4. **The overnight boundary is a regime line.** A pool may be flagged
   `rth_only` (must be flat by close); enforcement happens at reservation
   time — an `overnight` envelope simply cannot reserve from an
   `rth_only` pool. Not a runtime hope, a type error.

## 3. Risk under shared dollars

Per-strategy breakers stay exactly as they are. The pool adds its own
**daily drawdown breaker**: the same dollars working three shifts can lose
three times in one day — "stacking trades Sharpe for CAGR" has a risk
face, and it is serial same-day losses. A tripped pool breaker pauses
every subscriber and requires the same written post-mortem the instance
breakers do. Currencies never mix: a pool is share-dollars XOR
options-BP; `options_required_capital` already prices held collateral
honestly and `execute_intent` (strategy_runner.py:425) already refuses
past `available` — pools inherit that arithmetic, they don't replace it.

## 4. Accounts: v1 single, v2 concurrent

**v1 keeps the current constraint** — one active account (switching is
refused while plans are open today; keep that). Pools live inside the
active account. The capability vector still ships in v1: it turns "this
strategy is silent" into "this account can't run it" in the console.

**v2 (concurrent taxable + Roth) is gated on the IRA preflight script**
(handoff engineering queue): AH orders in an IRA, PDT treatment, and
recycle semantics are all UNVERIFIED. Known rows so far, from docs (not
yet empirical — the preflight makes them empirical):

| capability | taxable margin | Roth IRA |
|---|---|---|
| shorts | yes | no |
| options | L3, spreads/flies | L2 only — no spreads, no fly |
| leverage | 2-4x | 1x forever |
| ah_orders | yes (verified live) | UNKNOWN — make-or-break for delayed arm |
| recycle | instant | believed ok (limited margin), UNVERIFIED |

## 5. The registered stats block (frozen numbers only)

Attached at pre-registration, per instance (or class default):

```
registered: {
  metric: "net bp/trade",          # the pre-reg's OWN yardstick
  band: [8, 15],                   # the expectation band
  sharpe: 1.00, cagr_pct: null,    # point estimates, full window
  window: "2022-01..2026-08",
  costs_assumed_bp: 10,
  source_note: "research/open-window/notes/failed_gap_split_20260810.md",
  series: "backend/registered/gap_fail_fade_daily.csv",
  registered_commit: "<hash of the pre-reg commit>",
}
```

Three rules. **No naked numbers** — a Sharpe travels with its window and
cost assumption or it is a lie (gff is 1.25 @6bp and DEAD @20bp; the
bare "1.25" is not information). **Frozen at the pre-reg commit** — live
results never edit the backtest column; a better backtest later is a new
registration. **Full window only** — the 2024+ face of the mechanical
book (2.7-3.0) is documented flattery; projections pin to 2019-26 (or the
sleeve's full data), never to a recent regime.

**Series freezing (dependency rule):** `app/` never reads `research/`.
The composer needs each sleeve's daily return series, so REGISTRATION
COPIES it out of research-land into `backend/registered/<kind>_daily.csv`
(daily net returns on the sleeve's own capital, flat days included — the
`mech_account` convention), committed alongside the pre-reg doc. Research
produces; registration freezes; app reads only the frozen copy. Same
one-way arrow as everything else.

## 6. The day composer (projected book, done honestly)

Input: the day's build — instances, pools, envelopes. Computation:
overlay the frozen daily series and compute the composed book's
Sharpe/CAGR/maxDD/beta FROM THE COMBINED SERIES — the exact method of the
mechanical-book study (account-level, flat days in series, daily OLS vs
SPY), which produced 11%/yr Sharpe 1.62 at cross-sleeve correlations
~0.00. **Never add summary stats**: Sharpe combines through correlations,
CAGR through capital shares — formula math on the table's numbers
overstates both. A sleeve without a frozen series renders "no composable
series", not a guess. Output is labeled **BACKTEST**, not expectation.

Side effect that pays for the feature: the composer is the conflict
detector. Same pool + overlapping envelopes + reserved sum > pool size is
visible in the composer BEFORE it is a sizing bug at 09:32.

## 7. Band-vs-live cards (the ladder as a progress bar)

Per instance, one card joining what already exists — registered block
(§5), realized P&L (`GET /api/strategies/{id}/performance`), note-mode
twin (`GET .../twin`) — plus one new counter: **sessions inside the
band**. The card reads like the pre-reg ladder: "gff-1 — registered +8-15
net bp/trade · live: 4 sessions, +11bp mean, measured RT cost 12bp vs 10
assumed · **4/20 in band**." Rules: the card shows the pre-reg's OWN
metric; **no live Sharpe until the sample carries it** (at 10 sessions a
Sharpe is noise with a confidence interval wider than the number, and an
early scary/euphoric one beside a frozen backtest is goal-pressure by
UI); the cost row compares measured vs assumed because that is stopping
rule #1 in both current pre-regs.

## 8. Console

- **FUND page — day timeline**: a Gantt of pools × clock, each
  reservation a bar colored by strategy; idle windows and conflicts
  visible at a glance. The existing allocation bar keeps answering "how
  much"; the timeline answers "when".
- **FUND page — composer panel**: the §6 projected book beside the
  account's live curve, clearly labeled backtest vs realized.
- **STRATEGIES page — §7 cards** on the instance view.

## 9. Build order (each stage ships alone)

1. **Registered stats + band-vs-live cards.** Pure join; the data exists
   (pre-reg docs, performance, twin). Backend: the registered block +
   sessions-in-band counter. Frontend: the card. Zero engine-behavior
   change. Do first.
2. **Day composer, read-only.** Needs series freezing (§5) for fly and
   gff (the two registered sleeves) + one composer endpoint. Still zero
   behavior change.
3. **Pools, envelopes, reservations.** `execute_intent` /
   `allocation_state` (strategy_runner.py:694) grow pool awareness;
   private-pool default keeps ship day inert; pool breaker; timeline UI.
   This is the real engine work — it lands with the same test discipline
   as the collateral fix (strategy-path only, manual trading untouched).
4. **v2 concurrent accounts** — only after the IRA preflight empirically
   answers §4's UNKNOWNs.

## 10. What does NOT change

Paper lock. The manual trading path. Per-strategy allocation + breaker
semantics (pools wrap them, never replace them). The one-way
research→registration→app arrow. And no dynamic performance-chasing
allocation — that refusal is a design decision recorded here, not a
missing feature.

## 11. Open questions, honestly small

- Pool-breaker day boundary: ET session or ET calendar day (pick at
  build; session is probably right — overnight holds span calendar days).
- Does `fallback` ever get a tenant? Only if a soaker survives a study;
  none has.
- Series format edge: sleeves measured on sub-daily exposure (gff's ~4
  minutes) still freeze as DAILY net-on-own-capital rows — the composer
  composes days, not minutes. Documented so nobody "fixes" it into a
  minute panel.
