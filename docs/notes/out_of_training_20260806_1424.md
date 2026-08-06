# Out-of-training test (2025-08-04..2026-07-23)

257 events x 4 models = 1028 verdicts.

| model | corpus | n | signed bp | accuracy | gated bp |
|---|---|---|---|---|---|
| Opus 4.6 | out | 222 | -31.3 | 41.5 | 77.5 |
| Opus 4.6 | in | 35 | +263.6 | 55.6 | 627.9 |
| Opus 4.7 | out | 141 | -111.8 | 37.7 | -95.6 |
| Opus 4.7 | in | 116 | +72.1 | 46.5 | 299.1 |
| Opus 4.8 | out | 141 | -56.2 | 42.1 | -31.7 |
| Opus 4.8 | in | 116 | +101.0 | 47.7 | 304.7 |
| Opus 5 | out | 21 | -46.8 | 47.1 | -72.9 |
| Opus 5 | in | 236 | +82.8 | 48.6 | 198.4 |

Two-way FE beta = +14.21bp (se 59.13, 90% CI [-83.1, 111.5]).
Verdict: inconclusive.
