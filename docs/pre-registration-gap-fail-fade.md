# Pre-registration — gap_fail_fade (the failed-gap auction fade)

Registered 2026-08-10, BEFORE the first live decision. Per
`docs/briefs/strategy-authoring.md` §2: numbers chosen after this commit
are hypotheses, not results. The commit hash of this file is the
reference.

## The evidence this trades

`research/open-window/notes/premarket_auction_20260810_0453.md` and
`failed_gap_split_20260810.md` (6,836 gap events, 2022-01..2026-08):
when the late-premarket tape (09:15->09:29) has moved >= 20bp AGAINST a
>= 1.5% overnight gap, entering AGAINST the gap AT THE OPENING AUCTION
and exiting one minute later earns +23.9bp gross (t 5.85), both
directions (+24.3 long / +23.6 short), positive 4 of 5 years, strongest
2025-26. Entering at 09:31 instead of the print erases the edge — the
09:28 MOO order is the strategy. Account sim (2 slots per leg, 10bp):
long 1.00 / short 1.11 / both 1.53 Sharpe, leg correlation -0.05.

## The exact configuration under test

`gap_fail_fade` defaults as committed with this file:
candidates from the premarket movers screen (top 50), price >= $10,
|gap vs prior close at 09:27| >= 1.5%, late-premarket turn (09:15 ->
09:27 snapshot) >= 20bp against the gap · up to 2 positions per leg,
contested slots to the higher |gap| x log(dollar volume) · size = 25% of
allocation per position (4 positions = 100%, never levered) · entry MOO
submitted ~09:28 (auction print; no spread paid) · exit: hard time stop
09:31:30 ET, both legs (the "snap"; no tp/sl) · the short leg exists in
code and stands down automatically while `equity_long_only` is on.
Allocation $10,000 (usd). Circuit breaker 20% of allocation.

## Deviations from the measurement, accepted a priori

1. Live turn is measured 09:15 -> 09:27 (decision time before the 09:28
   MOO cutoff); the study measured to 09:29.
2. The exit fills at the enforcer's 09:31:30 sweep with a market-able
   exit, not the 09:31 bar close; slippage on a gapped book is the soft
   assumption (10bp/round-trip was charged; the journal's spread field
   measures the truth).
3. The study's universe came from daily-panel gaps at the open; live
   candidates come from the premarket movers screen at ~09:12 — names
   that gap without premarket tape can be missed. Coverage is journaled.
4. Selection inside the study's top-8 was arbitrary; live pins |gap| x
   log(dv) rank. Registered as a rule choice, not a fitted one.

Registered expectation, long leg only (live=false first, then one-share):
+8-15bp net/trade, ~0.5-0.8 trades/day, account Sharpe 0.7-1.1. Both
legs (when shorts unlock): Sharpe 1.2-1.7.

## Metric and sample

Per-trade net bp (journal/twin in note-mode; realized P&L per plan once
live), trades/day, and the measured exit cost vs the 10bp assumption.
Sample: every session from enable. Gates: >= 20 note-mode sessions
consistent with the band -> one-share live >= 10 sessions -> full size.

## Stopping rules (written before the first trade)

- Measured round-trip cost (auction fill vs 09:31:30 exit fill, vs the
  journal's own marks) above 20bp -> halt; the cost model is wrong.
- 30-session realized mean below -5bp/trade -> pause.
- Screener coverage: if >1/3 of journaled study-qualifying gaps were
  missed by the movers screen, fix the scanner before trusting any
  result.
- Breaker fires -> paused until a written post-mortem exists.
