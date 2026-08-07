# Phase 15 — run the flagship

Written 2026-08-06, after the paper closed and the research tree moved to
`research/`. The study is finished; this is what it would take to trade its
conclusion.

Read [the paper](../report.html) first, Table 1 in particular. Everything
below assumes it.

## Standing constraints (unchanged)

- Commit locally per step, **never push**.
- Paper-lock: `docs/report.html` is generated, never hand-edited. Any change
  under `research/` is a refactor whose success criterion is that
  `docs/report_data.json` rebuilds byte-identical.
- Keys only in `.env`.
- `app/` must never read a research artefact.
- 408 backend / 139 frontend tests stay green; ruff clean.

---

## 1. The spec, and what the engine actually runs

The paper's flagship is **never stand down + conditional exit + a six-slot
book**: 48.2% CAGR at Sharpe 2.15 and an 18.0% maximum drawdown across
2016–2026, taking 1,420 of 1,787 qualifying events. With the $100M/day
liquidity floor it is 44.6% at Sharpe 2.27 on 1,167 trades — less return,
better risk-adjusted, and the version the paper says it would run.

`app/strategies/earnings_reaction.py` is not that strategy. It is the
configuration the paper reports as **the weakest thing it measured**: 38.3bp
per trade, compounding to 5.5% a year at Sharpe 0.63 — against 16.0% for SPY
over the same decade. The engine is running the control, not the result.

Eight deltas, in descending order of how much each is worth:

| # | flagship | live today | worth |
|---|---|---|---|
| 1 | **no stop, no target** | vol-scaled stop, target at 2× | 38.3 → 138.6bp/trade. The single largest number in the study. |
| 2 | **never stand down** — with the tape on agreement, *against* it on disagreement or neutral | gate only; ~38% of events journalled and skipped | 129.4bp across every event vs 138.6bp on the 62% it keeps, but on 61% more trades. Neutral verdicts faded are the strongest single row in the mutation table (+77.3bp on 296). |
| 3 | **6 concurrent, 16.67% each**, contested slot to the highest prior-session dollar volume | risk-based sizing × conviction multipliers, no cap | together with #4: Sharpe 1.91 → 2.15, CAGR 26.4% → 48.2%. Peak exposure 100%, never levered. |
| 4 | **exit at the next close; the third close where guidance was raised or lowered** | time stop T+1/T+2 at 15:55 ET | the cross of §5.3's best exit with §5.4's best policy — the combination the paper concludes for, and one it never measured separately from #3. |
| 5 | top **5** per night, floor **$50M/day** ($100M preferred) | `n_names=8`, `min_price` only, no dollar-volume floor | the floor is most of the liquidity effect; the rank is already implemented. |
| 6 | costs charged at measured spreads | flat 13bp assumed | §6: 23.2bp measured. Not a code change — a truth about the P&L. |
| 7 | \|reaction\| ≥ 5% vs the official close | ✓ same | — |
| 8 | one call, medium effort, 12k chars, no tools | ✓ same | — |

Deltas 7 and 8 are already right.

> **BUILT 2026-08-07 as `app/strategies/pead_flagship.py`**, a new class
> rather than an edit to the control. `earnings_reaction` is retired from the
> registry along with the two integration-proof strategies; the values Table 2
> documented are frozen at `research/pead-llm-gate/scripts/_shipped_config.py`
> rather than read live, because a live read would now make the table track a
> strategy it is not about. The paper rebuilds byte-identical.
>
> Stages 0 and 2–4 below are still open. What exists is the class, its
> allocation and breaker, and a note-mode instance.

---

## 2. Three things that must be true first

Ordered by whether they kill the strategy outright.

### 2.1 The fill has never happened. This is the blocker.

The paper says it in Limitations and it is not hedging: *the live account is
not entitled to the consolidated feed, so no after-hours entry has ever
filled — the part of this strategy least supported by evidence is the part
that puts the trade on.*

`docs/notes/sip_preflight_20260806_0016.md` is the measurement:

| | |
|---|---|
| configured feed | `iex` |
| SIP quotes for NVDA / AMD / SPY | none — the feed returns nothing |
| IEX quote age after hours | 26,660s (7.4 hours) |
| IEX book at 00:16 ET | AMD 460.81 / 509.53 — a 10% "spread" |
| verdict | **NOT READY** |

Every after-hours entry dies at `QUOTE_MAX_AGE_S = 120`, correctly: a
seven-hour-old quote is not a price. Until the Alpaca subscription carries
real-time SIP, the strategy is a journal and nothing else. **No amount of
strategy code changes this.** It is a billing decision, then a re-run of
`backend/scripts/verify_sip_entitlement.py` until it reads READY.

### 2.2 Never standing down means shorting, and shorting is gated off

`equity_long_only` defaults to `True`. The flagship shorts on every bearish
agreement *and* fades every bullish-verdict-into-a-falling-tape — a large
fraction of 1,787 events. `backend/scripts/verify_short_paths.py` exists to
prove broker behaviour before that flag is flipped; run it in RTH, because
the fill probe is only meaningful when shorts can actually be borrowed.

### 2.3 The risk gate will refuse every flagship trade

`RiskService` caps per-trade max loss at 2% of equity
(`max_loss_pct`, risk.py:18). A 16.67% position with no stop declares a
16.67% max loss. Every intent is refused, correctly, by a gate doing its job.

There was also a schema problem underneath it: `tp_premium` and `sl_premium`
were non-nullable, and `ExitEnforcer` *is* the bracket — "no stop, no target"
was not expressible as a plan.

> **RESOLVED 2026-08-07 (migration 0007), and not the way this brief first
> recommended.** The original recommendation was a catastrophic 20% stop,
> priced from Table 12 at 22.1bp per trade. Matthew's call was better: retire
> the per-position stop requirement entirely and move the bound up a level.
>
> - `tp_premium` / `sl_premium` are nullable. **Both** null is a
>   time-stop-only plan; one alone is refused, because it is neither a target
>   nor a stop. The enforcer skips its whole quote-evaluation path for such a
>   plan rather than running a Kalman filter against triggers that do not
>   exist.
> - `RiskService` takes `max_loss_dollars=None` for those plans and skips
>   only the per-position loss check. Gross exposure and the BP cap still
>   apply.
> - Two new bounds replace it, both per strategy: an **allocation** (percent
>   of equity or a dollar ceiling) that caps what the instance can commit at
>   all and that `ctx.account()` reports as its equity, and a **circuit
>   breaker** — a drawdown against the strategy's own high-water mark that
>   flattens its book and pauses it.
>
> The reasoning, in one line: a per-position stop bounds one trade and costs
> edge on **every** trade; a breaker bounds the strategy and costs nothing
> until it fires. The engine's invariant survives intact — every position
> still has a server-enforced exit plan, and a hard time stop is one.
>
> Do not run an unbracketed strategy with its breaker disabled. The console
> says so where the toggle is.

---

## 3. The stages

### Stage 0 — pre-register, before a single live decision

The paper is explicit that two of the flagship's lines were chosen after
seeing results: the conditional exit and the six-position limit. It also says
what that implies — *a pre-registered test on genuinely forward data is worth
more than any further sweep.* This is the cheapest item on the list and the
only one that buys evidence nothing else can.

Write `docs/pre-registration-flagship.md`: the exact parameter set (read it
out of `PeadFlagship.default_params`), the allocation and breaker settings,
the metric (per-trade bp and the account curve), the sample (every qualifying
event from the first live night), and the stopping rule from §4. Commit it,
and record the commit hash. A number chosen after that commit is a new
hypothesis, not a result.

### Stage 1 — the strategy class

`app/strategies/pead_flagship.py`, registered in
`app/strategies/__init__.py`. Deltas 1–5 from the table above. It reuses
`earnings_reaction`'s watchlist, re-anchor, confirmation-delay and analysis
paths almost entirely — the genuinely new code is:

- **the fade branch.** `side` becomes `+1/-1` on agreement and
  `-sign(move_pct)` on disagreement *or* neutral, instead of `0`.
- **the slot allocator.** Six concurrent positions, equal weight, contested
  slots resolved by prior-session dollar volume. This is per-instance state
  and must survive a restart, unlike the in-memory watchlist — a restart that
  forgets it holds four positions will open six more.
- **the conditional horizon.** `time_stop_utc` at T+3's close when
  `verdict["guidance"] in ("raised", "lowered")`, T+1 otherwise. Note the
  paper charges the capital cost of the longer hold; the live account pays it
  automatically by holding the slot.

Tests mirror `tests/test_earnings_reaction.py`, plus: a neutral verdict
produces a short against an up tape; a seventh simultaneous event is declined
for want of a slot and journals *why*; the allocator's state reloads.

### Stage 2 — shadow, in note-mode, for a full earnings season

`live: false`. Both this and `earnings_reaction` run side by side on the same
events — that is the comparison the paper could not make, because the twin
policies were scored on cached verdicts rather than on live ones.

Watch it in the ops console: STRATEGIES → the instance → DECISION JOURNAL,
and `/api/strategies/{id}/twin` for the equity curve the would-be trades
imply. Two questions the shadow answers that the backtest cannot:

1. **Did the release arrive in time?** The paper drops events whose 8-K was
   accepted outside `[16:00, 16:20]` — 227 of 1,552. Live, that is not a
   filter, it is a miss.
2. **What was the book actually quoting at entry?** Every decision journals
   `spread_pct`. The paper's 23.2bp is measured from historical NBBO; this is
   the same measurement on the account that would trade it.

### Stage 3 — prove the fill

Not tradeable until this passes, and it cannot start before §2.1.

One share. A real after-hours entry on a real earnings night, filled and
exited, with the exec-quality ledger recording what immediacy cost. Until a
fill exists, every number in the paper is a claim about a market the account
has never transacted in.

### Stage 4 — live, small

`live: true`, at a fraction of the paper's weight. Scale on evidence from
Stages 2 and 3, not on the backtest — the backtest is why we are here, it is
not confirmation.

---

## 4. What should stop this

Write these down before the first trade, not after the first drawdown.

- **The realised round trip runs above 60bp.** The gated leg breaks even at
  151.6bp and the measurement says 23.2bp, so there is real headroom — but
  the measurement is of historical NBBO, and the account crossing the book is
  not the same event. 60bp is a third of the way to breakeven and means the
  cost model is wrong.
- **The fade leg loses live.** It is 34% of the book, it is the part that
  bets against the tape, and it is the part most exposed to costs.
- **2026 continues negative.** It is the only year with a negative selection
  spread: −30.3bp on 153 scored events, against +231.5bp in 2025. Far too
  small to act on now, and the first thing another two quarters will answer.
- **A slot cap that binds harder live than in the sim.** 20.5% of events were
  declined for want of a slot in the backtest. If the live rate runs much
  higher, the capacity model is wrong and the return scales down with it.

## 5. What this does not fix

- The memorisation bound is ±85.9bp — 61% of the edge — and
  **supply-constrained**: every model with a training cutoff before 2025 now
  404s. It cannot be narrowed by spending. Forward trading is the only
  instrument that shrinks it, because a release the model cannot have read is
  the one control money cannot buy.
- Familiarity is not separated from skill. The identity-ablation arm fails to
  scrub almost every release, so whether the model reads a large issuer's
  release better because it *knows* that issuer is still open.
- Survivorship is structural: the universe comes from today's SEC ticker map,
  so a fully delisted issuer could never have entered it.
