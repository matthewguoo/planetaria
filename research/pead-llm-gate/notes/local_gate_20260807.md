# The gate re-scored by open weights on one 3090

generated 2026-08-07; schema-constrained decoding, prompts byte-identical to the Opus 5 run. Reading arms greedy; the forced memory probe sampled at temperature 1.0 (see below for why).

## Forced memory probe — the cleanliness measurement

Ticker and date, no release text, and NO option to abstain: the model must answer up or down for the next session. There is no information in this prompt, so accuracy above 50% is recall and nothing else, and refusal tuning cannot hide in an abstention because none is offered. Sampled rather than greedy — greedy collapses a no-signal model onto one constant answer whose accuracy is merely the base rate of up-moves.

| model | n | said up | accuracy | z vs chance | 95% CI |
|---|---|---|---|---|---|
| Opus 5 | 1789 | 49% | 57.1% | +6.03 | [54.8, 59.4] |
| Phi-4 14B (Q8_0, local) | 1800 | 95% | 48.6% | -1.23 | [46.2, 50.9] |
| Qwen3-30B-A3B (Q4_K_XL, local) | 1800 | 82% | 50.1% | +0.05 | [47.7, 52.4] |

A model whose interval covers 50% has no usable recall of these events, and nothing its reading arms earn can have come from remembering the outcome.

## Free-response memory probe — what Opus ran, and why it is not enough on its own

Same prompt, but 'no' and 'unknown' are available. This is the arm the study ran on Opus, kept here for comparability. It is NOT a safe cleanliness test for a heavily refusal-tuned model: phi-4 answered 'no recall' on every single event, and a no-schema control showed it will refuse a plain question about Microsoft's FY2023 revenue while correctly recalling Meta's post-Q4-2021 drop. On such a model this arm measures willingness to claim recall, not recall.

| model | n | claims recall | volunteers a direction | recall accuracy | z vs chance |
|---|---|---|---|---|---|
| Opus 5 | 435 | 41.4% | 175 (40.2%) | 66.3% | +4.31 |
| Phi-4 14B (Q8_0, local) | 1800 | 0.0% | 0 (0.0%) | — | — |
| Qwen3-30B-A3B (Q4_K_XL, local) | 1800 | 0.0% | 0 (0.0%) | — | — |

Read this table only as a contrast in DISCLOSURE, not in recall: Opus volunteers and is right well above chance, which establishes that it has recall; a 0% volunteer rate establishes only that the model declines. The forced probe above is what settles whether anything is behind the refusal.

## `named` arm — paired on the 1787 events every model below scored

| model | n | neutral | ungated | gated | vetoed | spread | keeps | predicts tape | predicts drift |
|---|---|---|---|---|---|---|---|---|---|
| Opus 5 | 1787 | 17% | +33.1 | +138.6 | -140.3 | +278.9 | 62% | 74.5% (z=+18.93) | 57.6% (z=+5.88) |
| Phi-4 14B (Q8_0, local) | 1787 | 71% | +33.1 | +42.9 | +30.7 | +12.2 | 20% | 67.8% (z=+8.06) | 48.3% (z=-0.75) |
| Qwen3-30B-A3B (Q4_K_XL, local) | 1787 | 59% | +33.1 | +22.8 | +36.6 | -13.8 | 26% | 61.8% (z=+6.44) | 47.5% (z=-1.36) |

Permutation null (1000 within-month shuffles of the verdict labels), p(spread >= observed):
  - Opus 5: p = 0.000
  - Phi-4 14B (Q8_0, local): p = 0.288
  - Qwen3-30B-A3B (Q4_K_XL, local): p = 0.608

- Phi-4 14B (Q8_0, local) agrees with Opus 5 on 34.4% of 1787 paired events.
- Qwen3-30B-A3B (Q4_K_XL, local) agrees with Opus 5 on 40.6% of 1787 paired events.

## `forced` arm — paired on the 1790 events every model below scored

**Opus 5 is absent from this arm**: no `forced` verdicts were ever collected for it, so this table compares the two local models to each other and not to the study model. That is still the comparison the arm exists for — phi-4 against a same-weight-class model that IS heavily web-trained — but the Opus reference here has to come from the `named` arm above.

| model | n | neutral | ungated | gated | vetoed | spread | keeps | predicts tape | predicts drift |
|---|---|---|---|---|---|---|---|---|---|
| Phi-4 14B (Q8_0, local) | 1790 | 0% | +32.8 | +56.9 | +2.1 | +54.9 | 56% | 56.0% (z=+5.11) | 49.8% (z=-0.14) |
| Qwen3-30B-A3B (Q4_K_XL, local) | 1790 | 0% | +32.8 | +54.8 | +2.9 | +51.8 | 58% | 57.6% (z=+6.43) | 50.5% (z=+0.43) |

Permutation null (1000 within-month shuffles of the verdict labels), p(spread >= observed):
  - Phi-4 14B (Q8_0, local): p = 0.061
  - Qwen3-30B-A3B (Q4_K_XL, local): p = 0.053


## Events after each local model's pretraining cutoff

Memorisation is impossible by construction on this side of the line. It is a smaller sample and a different market regime, so the comparison that matters is a model against ITSELF across the boundary, not against the other window's absolute level.

| model | window | n | gated | vetoed | spread |
|---|---|---|---|---|---|
| Phi-4 14B (Q8_0, local) | in-corpus | 1244 | +46.9 | +0.1 | +46.8 |
| Phi-4 14B (Q8_0, local) | post-2024-06 | 546 | +35.5 | +107.5 | -72.0 |
| Qwen3-30B-A3B (Q4_K_XL, local) | in-corpus | 1533 | +53.9 | +11.1 | +42.8 |
| Qwen3-30B-A3B (Q4_K_XL, local) | post-2025-07 | 257 | -146.3 | +191.2 | -337.5 |

## What this can and cannot settle

- It CANNOT prove any model is uncontaminated. Both have seen most of this decade. The notext arm bounds what they can recall; it does not certify a corpus.
- A weak local result is NOT evidence for memorisation in Opus. Capability and cleanliness both push the same direction, which is exactly why the Qwen control is here.
- Quantisation and greedy decoding handicap both local models. The handicap runs against the capability control, not for it.
