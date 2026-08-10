# Overnight decomposition, flagship panel — 20260810_0127

1787 scored events with the legs their horizon needs (0 dropped for missing daily rows), conditional horizon T+3 on moved guidance (1060 extended), costs 13bp per round trip, ladder legs 6bp per extra night. Close exits use the paper's own raw 15:55 closes, so t1_close and cond_close are the paper's mutation-table numbers by construction — read them as the regression check. Opens are the adjusted same-session open/close ratio chained onto those closes.

## FULL POLICY (never stand down) — where the drift lives (n=1787)

| leg | mean bp | t | >0 % |
|---|---|---|---|
| night0 (all trades) | +123.3 | +9.30 | 60 |
| day1 (all trades) | +20.5 | +1.35 | 52 |
| night1 (T+3 subset, n=1060) | -3.1 | -0.48 | 49 |
| day2 (T+3 subset, n=1060) | +41.2 | +3.31 | 52 |
| night2 (T+3 subset, n=1060) | +7.0 | +1.24 | 52 |
| day3 (T+3 subset, n=1060) | +13.5 | +1.27 | 52 |

## THE GATE (agreements only) — where the drift lives (n=1111)

| leg | mean bp | t | >0 % |
|---|---|---|---|
| night0 (all trades) | +122.1 | +7.44 | 60 |
| day1 (all trades) | +32.2 | +1.70 | 54 |
| night1 (T+3 subset, n=724) | -2.9 | -0.40 | 49 |
| day2 (T+3 subset, n=724) | +51.9 | +3.42 | 53 |
| night2 (T+3 subset, n=724) | -1.3 | -0.21 | 50 |
| day3 (T+3 subset, n=724) | +12.3 | +0.93 | 52 |

## Exit policies through the 6-slot account (FULL POLICY sides)

| policy | mean bp | win% | t | CAGR | Sharpe | maxDD | CAGR@23.2bp |
|---|---|---|---|---|---|---|---|
| cond_close (paper flagship) | +166.9 | 57.9 | +7.18 | 48.2% | 2.15 | 18.0% | 44.9% |
| cond_open (exit at the open) | +135.2 | 56.5 | +6.94 | 37.1% | 2.08 | 15.3% | 34.0% |
| t1_close | +129.4 | 56.5 | +6.47 | 36.2% | 2.01 | 12.7% | 33.1% |
| t1_open | +110.3 | 58.3 | +8.32 | 28.1% | 2.32 | 7.9% | 25.2% |
| hybrid (T+1 at open, T+3 at close) | +145.1 | 57.4 | +6.85 | 39.8% | 2.03 | 13.8% | 36.6% |
| nights_only (ladder) | +105.4 | 56.9 | +7.56 | 27.2% | 2.19 | 9.4% | 24.3% |
| days_only (diagnostic) | +34.8 | 52.0 | +1.84 | 9.6% | 0.66 | 37.1% | 7.1% |

Same trades, same slots, same sessions — only the exit clock moves. The ladder pays its extra legs; the diagnostic days-only book is what the exits would leave behind.
