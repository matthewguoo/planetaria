# Intraday momentum (GHLZ first->last half-hour) — 20260810_0140

SIP 30-minute bars via Alpaca, 2016 -> 2026-08. Sign trade: enter 15:30 in the predictor's direction, exit at the close (MOC). Net lines charge 1.5bp per round trip. GHLZ's own sample ends 2013; this is purely post-publication data.

## SPY — 2663 days

| predictor | window | n | bp/trade gross | t | net bp | ann net % | ann Sharpe (net) | win% |
|---|---|---|---|---|---|---|---|---|
| A (on+30m) | all | 2656 | -0.28 | -0.46 | -1.78 | -4.5 | -0.91 | 44.7 |
| A (on+30m) | 2016-2020 | 1254 | +0.08 | +0.08 | -1.42 | -3.6 | -0.62 | 44.3 |
| A (on+30m) | 2021-2026 | 1402 | -0.61 | -0.89 | -2.11 | -5.3 | -1.31 | 45.1 |
| B (30m) | all | 2647 | -0.64 | -1.06 | -2.14 | -5.4 | -1.09 | 45.4 |
| B (30m) | 2016-2020 | 1244 | -0.58 | -0.56 | -2.08 | -5.2 | -0.91 | 46.0 |
| B (30m) | 2021-2026 | 1403 | -0.70 | -1.03 | -2.20 | -5.5 | -1.37 | 45.0 |

- small |A| (<p50): +0.48bp gross (t +0.79, n=1331, net -1.02)
- mid |A| (p50-p80): -0.64bp gross (t -0.68, n=799, net -2.14)
- big |A| (>=p80): -1.64bp gross (t -0.76, n=533, net -3.14)

## QQQ — 2661 days

| predictor | window | n | bp/trade gross | t | net bp | ann net % | ann Sharpe (net) | win% |
|---|---|---|---|---|---|---|---|---|
| A (on+30m) | all | 2656 | +0.03 | +0.04 | -1.47 | -3.7 | -0.66 | 46.8 |
| A (on+30m) | 2016-2020 | 1254 | +1.59 | +1.39 | +0.09 | +0.2 | +0.04 | 48.7 |
| A (on+30m) | 2021-2026 | 1402 | -1.37 | -1.70 | -2.87 | -7.2 | -1.51 | 45.1 |
| B (30m) | all | 2644 | -0.48 | -0.70 | -1.98 | -5.0 | -0.89 | 45.4 |
| B (30m) | 2016-2020 | 1243 | +0.15 | +0.13 | -1.35 | -3.4 | -0.53 | 47.3 |
| B (30m) | 2021-2026 | 1401 | -1.05 | -1.30 | -2.55 | -6.4 | -1.34 | 43.7 |

- small |A| (<p50): +0.25bp gross (t +0.36, n=1330, net -1.25)
- mid |A| (p50-p80): -0.48bp gross (t -0.44, n=798, net -1.98)
- big |A| (>=p80): +0.22bp gross (t +0.09, n=533, net -1.28)

