# pead_confirmed: year table + slot sim — 20260811_0405

1,800 events with a +75-bar decision (of 1,800 pathed). Delayed entry at the decision print, exit T+1 close, net@13bp (panel convention) and @23.2bp (AH-measured stress). Gate: walk-forward GBM P(T+1 continuation), OOS AUC 0.751 on 1,518. PRIMARY (declared in docstring): tau=0.5.

## The family (OOS 2019-26, per trade)

| gate | kept | keep% | net@13 bp | t | net@23.2 bp | t |
|---|---|---|---|---|---|---|
| ungated (delayed hold) | 1,518 | 100 | +41.6 | +2.18 | +31.4 | +1.65 |
| P >= 0.45 | 895 | 59 | +83.0 | +3.55 | +72.8 | +3.11 |
| P >= 0.50 | 805 | 53 | +79.9 | +3.22 | +69.7 | +2.81 **<-- primary** |
| P >= 0.55 | 712 | 47 | +89.4 | +3.41 | +79.2 | +3.02 |
| rule: <=50% retraced & above vwap | 862 | 57 | +75.1 | +3.05 | +64.9 | +2.63 |

## Primary cell by year (net@13)

| year | ungated n | ungated bp | gated n | gated bp | t |
|---|---|---|---|---|---|
| 2019 | 110 | -1.6 | 57 | -31.5 | -0.48 |
| 2020 | 110 | +7.0 | 58 | +79.9 | +1.16 |
| 2021 | 197 | +52.3 | 100 | +155.1 | +1.77 |
| 2022 | 239 | -90.2 | 105 | +45.6 | +0.60 |
| 2023 | 179 | +67.7 | 92 | +48.9 | +0.68 |
| 2024 | 253 | +86.3 | 141 | +44.7 | +0.94 |
| 2025 | 276 | +133.8 | 156 | +164.2 | +2.95 |
| 2026 | 154 | +19.3 | 96 | +50.0 | +0.59 |

## Slot sim (4 slots/night by dollar volume, 25% each, 2019-26, net@13)

| book | trades | ann % | Sharpe | maxDD % | beta |
|---|---|---|---|---|---|
| ungated | 1,433 | +10.09 | +0.50 | 57.0 | -0.003 |
| gated (primary) | 802 | +21.31 | +1.11 | 16.5 | +0.013 |

Reading gates before anyone builds: the entry is an AFTER-HOURS fill (same SIP/AH dependency class as the delayed arm — the IRA preflight's question applies); the gate is a fitted model and inherits the full 2b testing bar (placebo/CV) before pre-reg; and the 23.2bp column is the cost reality until AH fills are measured on these books.

_Provenance: `research_pead_confirmed.py` at 3ed096f, 2026-08-11 04:05 ET_
