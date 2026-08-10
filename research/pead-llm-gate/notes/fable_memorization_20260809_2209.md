# Fable 5 memorisation panel — 20260809_2209

Fable 5's training data ends 2026-01, four months before Opus 5's 2026-05, while the model is the more capable of the two. Events in (2026-01, 2026-05] are therefore the first cells in this study where the corpus advantage and the capability advantage point in OPPOSITE directions. Fable verdicts here were collected through the Claude Code CLI on subscription auth (engine-identical invocation; no tools; system prompt replaced); the CLI exposes no effort control, so 'medium' in these ids is a join label, and Fable's always-on thinking makes its probes a ceiling on recall — see the module docstring.

## Forced memory probe (`notextf`) — ticker + date, no text, no abstention

| model | window | n | said up | accuracy | z | assoc (pp) |
|---|---|---|---|---|---|---|
| Fable 5 | all | 1694 | 51% | 59.5% | +7.82 | +19.1 |
| Fable 5 | in-corpus | 1552 | 51% | 61.3% | +8.88 | +22.6 |
| Fable 5 | post-cutoff | 142 | 47% | 40.1% | -2.35 | -20.7 |
| Opus 5 | all | 1789 | 48% | 57.1% | +6.03 | +14.2 |
| Opus 5 | in-corpus | 1767 | 48% | 57.1% | +5.97 | +14.1 |
| Phi-4 14B (Q8_0, local) | all | 1800 | 95% | 48.6% | -1.23 | -4.4 |
| Phi-4 14B (Q8_0, local) | in-corpus | 1251 | 94% | 50.3% | +0.20 | -3.7 |
| Phi-4 14B (Q8_0, local) | post-cutoff | 549 | 100% | 44.6% | -2.52 | — |
| Qwen3-30B-A3B (Q4_K_XL, local) | all | 1800 | 82% | 50.1% | +0.05 | +2.7 |
| Qwen3-30B-A3B (Q4_K_XL, local) | in-corpus | 1542 | 81% | 50.6% | +0.51 | +2.9 |
| Qwen3-30B-A3B (Q4_K_XL, local) | post-cutoff | 258 | 89% | 46.5% | -1.12 | +5.5 |

### Fable recall by year — how deep does memory go?

| year | n | accuracy | z |
|---|---|---|---|
| 2016 | 84 | 57.1% | +1.31 |
| 2017 | 87 | 55.2% | +0.96 |
| 2018 | 110 | 55.5% | +1.14 |
| 2019 | 110 | 66.4% | +3.43 |
| 2020 | 110 | 67.3% | +3.62 |
| 2021 | 197 | 60.9% | +3.06 |
| 2022 | 222 | 59.9% | +2.95 |
| 2023 | 179 | 61.5% | +3.06 |
| 2024 | 221 | 64.7% | +4.37 |
| 2025 | 220 | 61.4% | +3.37 |
| 2026 | 154 | 40.9% | -2.26 |

| confidence | n | accuracy |
|---|---|---|
| low | 977 | 53.3% |
| medium | 530 | 63.2% |
| high | 187 | 81.3% |

