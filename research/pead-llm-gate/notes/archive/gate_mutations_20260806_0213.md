# Gate mutations — 20260806_0213

The shipped gate trades the tape when the verdict agrees and stands down otherwise. Each mutation below spends the same verdicts differently. Chosen on nothing — every row is reported, including the failures.

1312 scored events, 552 in 2021-23 / 760 in 2024-26. Costs 13bp round trip. No bracket unless stated.

## Every mutation, unbracketed, at two horizons

| mutation | trades | % of book | T+1 all | 21-23 | 24-26 | win% | t | T+3 all | 21-23 | 24-26 |
|---|---|---|---|---|---|---|---|---|---|---|
| SHIPPED gate (verdict agrees with tape) | 919 | 70 | +171.7 | +141.2 | +195.1 | 57.5 | +6.31 | +200.5 | +146.3 | +241.9 |
| pure tape (mechanical, ungated) | 1312 | 100 | +53.8 | +31.5 | +70.0 | 52.6 | +2.20 | +54.2 | +24.0 | +76.2 |
| anti-tape baseline (fade EVERY event, no model) | 1312 | 100 | -79.8 | -57.5 | -96.0 | 46.3 | -3.27 | -80.2 | -50.0 | -102.2 |
| fade only: trade every disagreement | 202 | 15 | +154.6 | +222.2 | +106.5 | 55.9 | +2.28 | +233.4 | +248.9 | +222.3 |
| fade only: high confidence | 3 | 0 | +1017.7 | +244.1 | +2565.1 | 66.7 | +1.24 | +1123.9 | +254.6 | +2862.6 |
| fade only: high|medium confidence | 158 | 12 | +131.1 | +179.5 | +96.4 | 54.4 | +1.75 | +199.0 | +175.1 | +216.1 |
| pure LLM direction (gate + fade, tape ignored) | 1121 | 85 | +168.7 | +155.3 | +178.7 | 57.2 | +6.63 | +206.4 | +164.2 | +238.3 |
| gate + fade on high confidence | 922 | 70 | +174.5 | +141.7 | +199.6 | 57.5 | +6.40 | +203.5 | +146.9 | +246.9 |
| neutral verdicts, traded with the tape | 191 | 15 | -265.7 | -256.9 | -270.8 | 39.3 | -3.89 | -318.1 | -313.1 | -321.0 |
| neutral verdicts, traded against the tape | 191 | 15 | +239.7 | +230.9 | +244.8 | 60.2 | +3.51 | +292.1 | +287.1 | +295.0 |
| fade every non-agreement (disagree OR neutral) | 393 | 30 | +195.9 | +226.2 | +176.5 | 58.0 | +4.07 | +261.9 | +266.3 | +259.1 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 1312 | 100 | +179.0 | +164.9 | +189.2 | 57.6 | +7.49 | +218.9 | +179.8 | +247.3 |
| gate AND eps agrees | 504 | 38 | +121.3 | +145.9 | +103.2 | 54.0 | +3.61 | +139.7 | +150.5 | +131.7 |
| gate AND revenue agrees | 548 | 42 | +118.1 | +103.7 | +129.0 | 55.3 | +3.61 | +149.1 | +122.7 | +168.9 |
| gate AND eps AND revenue agree | 426 | 32 | +116.7 | +145.0 | +98.5 | 54.2 | +3.25 | +133.2 | +153.3 | +120.2 |
| gate AND guidance agrees | 607 | 46 | +192.0 | +173.5 | +205.2 | 56.8 | +5.60 | +236.2 | +197.8 | +263.6 |
| gate AND no quality flags (hard veto) | 6 | 0 | +406.3 | +461.9 | +395.1 | 83.3 | +1.36 | +295.2 | +568.9 | +240.5 |
| gate, quality flags INVERT the side | 919 | 70 | -192.3 | -164.8 | -213.2 | 41.8 | -7.06 | -222.5 | -169.4 | -263.0 |

Read the two horizon blocks against each other. The vetoed bucket gets monotonically worse with holding period while the gated one is flat, so if the refusals really contain a tradeable signal the fade must IMPROVE from T+1 to T+3. That is the single prediction this table exists to test.

## The fade, under scrutiny

This is the mutation that would change the strategy, so it gets the hostile treatment: does it survive a wider spread, does it hold in both halves, and is it one side of the book or both?

| holding period | trades | mean bp | 2021-23 | 2024-26 | win% | t |
|---|---|---|---|---|---|---|
| T+1 | 202 | +154.6 | +222.2 | +106.5 | 55.9 | +2.28 |
| T+2 | 202 | +207.8 | +246.6 | +180.2 | 51.0 | +2.56 |
| T+3 | 202 | +233.4 | +248.9 | +222.3 | 53.0 | +2.55 |
| T+4 | 202 | +222.9 | +225.9 | +220.8 | 53.0 | +2.31 |
| T+5 | 202 | +228.3 | +227.3 | +229.0 | 53.0 | +2.39 |

### Cost sensitivity

Fading a 5%+ after-hours move means crossing the book in the direction nobody wants. The study's flat 13bp is the friendliest assumption available; these are the same trades priced progressively worse.

| round-trip cost | T+1 | T+3 | T+3 2024-26 |
|---|---|---|---|
| 13bp | +154.6 | +233.4 | +222.3 |
| 25bp | +142.6 | +221.4 | +210.3 |
| 40bp | +127.6 | +206.4 | +195.3 |
| 60bp | +107.6 | +186.4 | +175.3 |
| 100bp | +67.6 | +146.4 | +135.3 |

### By confidence, by side, by year

| slice | trades | T+1 | T+3 | win% (T+3) |
|---|---|---|---|---|
| high confidence | 3 | +1017.7 | +1123.9 | 100.0 |
| medium confidence | 155 | +113.9 | +181.1 | 49.0 |
| low confidence | 44 | +239.0 | +357.1 | 63.6 |
| fading an UP tape (short) | 81 | +182.6 | +293.0 | 59.3 |
| fading a DOWN tape (long) | 121 | +135.8 | +193.5 | 48.8 |
| |reaction| >= 10% | 66 | +333.4 | +514.4 | 56.1 |
| year 2021 | 14 | +86.1 | +291.4 | 57.1 |
| year 2022 | 35 | +398.6 | +404.4 | 51.4 |
| year 2023 | 35 | +100.3 | +76.6 | 57.1 |
| year 2024 | 34 | +161.0 | +193.3 | 47.1 |
| year 2025 | 45 | +162.2 | +289.4 | 55.6 |
| year 2026 | 39 | -5.4 | +170.2 | 51.3 |

## Account model

Every mutation compounded through the real calendar at 30% average deployed capital, per-name weight normalised by the average number of concurrent positions so a longer hold does not silently commit more capital. T+1, unbracketed.

| mutation | trades | total | CAGR | maxDD | Sharpe | alpha | beta |
|---|---|---|---|---|---|---|---|
| SHIPPED gate (verdict agrees with tape) | 919 | +420.2% | 39.14% | 9.5% | 2.59 | +33.80% | 0.009 |
| pure tape (mechanical, ungated) | 1312 | +73.9% | 11.72% | 19.3% | 0.96 | +11.93% | -0.007 |
| anti-tape baseline (fade EVERY event, no model) | 1312 | -60.0% | -16.77% | 63.0% | -1.41 | -17.64% | 0.006 |
| fade only: trade every disagreement | 202 | +79.8% | 12.47% | 10.9% | 1.01 | +12.17% | 0.031 |
| fade only: high|medium confidence | 158 | +47.0% | 8.03% | 10.9% | 0.77 | +8.18% | 0.010 |
| pure LLM direction (gate + fade, tape ignored) | 1121 | +463.7% | 41.40% | 6.3% | 2.77 | +35.22% | 0.023 |
| gate + fade on high confidence | 922 | +434.4% | 39.90% | 9.4% | 2.63 | +34.34% | 0.009 |
| neutral verdicts, traded with the tape | 191 | -65.1% | -18.99% | 66.0% | -1.70 | -20.30% | -0.003 |
| neutral verdicts, traded against the tape | 191 | +141.2% | 19.29% | 11.6% | 1.55 | +18.32% | 0.002 |
| fade every non-agreement (disagree OR neutral) | 393 | +241.5% | 27.89% | 13.0% | 1.80 | +25.30% | 0.028 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 1312 | +587.0% | 47.12% | 7.0% | 3.12 | +39.19% | 0.021 |
| gate AND eps agrees | 504 | +154.5% | 20.57% | 10.0% | 1.58 | +18.79% | 0.060 |
| gate AND revenue agrees | 548 | +152.1% | 20.35% | 10.9% | 1.61 | +18.74% | 0.044 |
| gate AND eps AND revenue agree | 426 | +128.1% | 17.96% | 8.1% | 1.46 | +16.75% | 0.042 |
| gate AND guidance agrees | 607 | +398.2% | 37.95% | 7.7% | 2.36 | +33.02% | 0.013 |
| gate, quality flags INVERT the side | 919 | -85.6% | -32.21% | 86.5% | -2.86 | -37.84% | -0.010 |

SPY buy & hold over the same span: +88.2% total, 13.51% CAGR, 24.5% max drawdown.

Quality flags fire on 99% of releases, so the live 0.75x size shrink is very nearly a constant and cannot be doing discriminating work.
