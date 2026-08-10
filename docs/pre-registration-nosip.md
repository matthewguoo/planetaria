# Pre-registration — the no-SIP live test of the PEAD flagship

Written 2026-08-09, before the first live decision. Phase 15 Stage 0 asked
for this and the paper says why: the conditional exit and the slot cap were
chosen after seeing results, so only numbers committed to in advance count
as evidence. Anything chosen after this document is a new hypothesis.

## The instance

`nosip-1` (id `ee46b2a76cda46dc96a53097f2068885`), kind `pead_nosip`,
note-mode (`live: false`), allocation 25% of account, circuit breaker 15% of
allocation, on the planetaria-1 paper account. Every parameter is the
class default — the flagship's Table-1 policy line for line:

| param | value | provenance |
|---|---|---|
| n_names | 5 | §3, top-5 per night by prior-session $vol |
| min_dv_musd | 100.0 | the version the paper says it would run |
| min_move_pct | 5.0 | §2.1 event gate |
| confirm_min | 10.0 | §4.5 confirmation delay |
| never_stand_down | true | §5.4 — fade disagreement AND neutral |
| hold_days / hold_days_guidance | 1 / 3 | §5.3 conditional exit |
| slots | 6 | §5.5, equal weight, contested by $vol |
| sl_pct / tp_pct | null / null | no bracket; the breaker is the bound |
| model / effort | engine default / medium | §5.6 |
| quote_max_age_s | 120.0 | the engine's own freshness bar, made a param |
| verify_min | 16.0 | the free tier's SIP delay + 1 minute |

The 10% profit target from the 2026-08-07 exit sweep is **excluded**: it has
never been out of sample. The journal will show what it would have done; it
is not part of the policy.

## The metric

**Drift hit rate on non-neutral verdicts** — does the verdict's side predict
the sign of the next-session (or T+3) return. Not P&L (a single reaction is
hundreds of bp of noise), and not tape agreement (Phi-4 and Qwen3 both read
the tape well above chance with zero drift edge — tape agreement will look
healthy regardless).

Secondary, from the `fill_check` journal: the true delayed-SIP spread at
entry instants, the print-vs-mid cost, and the marketable-at-limit rate —
the paper's 23.2bp cost assumption measured on the account that would trade.

## The decision rule

In-corpus reference: 57.6% drift accuracy (Opus 5, n=1,787). Chance is 50%.
~200 non-neutral verdicts separate the two at z≈2 — roughly two to three
earnings seasons of top-5 nights.

- **Scale** (proceed to Stage 3 fill proof, then order-mode small): forward
  drift hit rate holds ≥55% with the fade leg not losing outright.
- **Stop**: forward drift hit rate ≈50% at n≥200 (the edge was recall
  and/or regime — there is nothing to trade); or the fade leg loses live
  while the gated leg does not (the policy's distinctive claim failed); or
  realised round trip exceeds 60bp (the cost model is wrong); or 2026's
  negative selection spread persists through two more quarters.

## Why this is the experiment that matters

Every release after this commit is out-of-corpus for every model by
construction. The 2026-08-09 Fable panel made the in-sample ambiguity as
sharp as it gets — recall is real (61.3% from ticker+date alone, decade
deep, 81.3% at high confidence) and the in-sample backtest cannot separate
reading from remembering. Forward events are the one control money cannot
buy backwards.
