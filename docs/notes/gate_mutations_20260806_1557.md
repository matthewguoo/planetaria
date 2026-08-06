# Gate mutations — 20260806_1557

The shipped gate trades the tape when the verdict agrees and stands down otherwise. Each mutation below spends the same verdicts differently. Chosen on nothing — every row is reported, including the failures.

1787 scored events, 612 in 2021-23 / 1175 in 2024-26. Costs 13bp round trip. No bracket unless stated.

## Every mutation, unbracketed, at two horizons

| mutation | trades | % of book | T+1 all | 21-23 | 24-26 | win% | t | T+3 all | 21-23 | 24-26 |
|---|---|---|---|---|---|---|---|---|---|---|
| THE GATE (verdict agrees with tape) | 1111 | 62 | +138.6 | +134.0 | +141.0 | 57.2 | +5.56 | +178.7 | +155.3 | +191.1 |
| pure tape (mechanical, ungated) | 1787 | 100 | +33.1 | -2.1 | +51.4 | 52.0 | +1.63 | +43.3 | -9.2 | +70.6 |
| anti-tape baseline (fade EVERY event, no model) | 1787 | 100 | -59.1 | -23.9 | -77.4 | 46.6 | -2.92 | -69.3 | -16.8 | -96.6 |
| fade only: trade every disagreement | 380 | 21 | +143.2 | +263.7 | +82.7 | 55.5 | +3.30 | +214.1 | +361.4 | +140.1 |
| fade only: high confidence | 9 | 1 | +559.5 | +344.7 | +828.1 | 66.7 | +1.28 | +785.1 | +384.1 | +1286.5 |
| fade only: high|medium confidence | 304 | 17 | +136.0 | +255.0 | +76.8 | 54.3 | +2.96 | +201.0 | +374.3 | +114.8 |
| pure LLM direction (gate + fade, tape ignored) | 1491 | 83 | +139.8 | +166.3 | +126.0 | 56.8 | +6.47 | +187.8 | +206.6 | +177.9 |
| gate + fade on high confidence | 1120 | 63 | +142.0 | +136.7 | +144.8 | 57.3 | +5.68 | +183.6 | +158.2 | +197.1 |
| neutral verdicts, traded with the tape | 296 | 17 | -103.3 | -154.8 | -76.2 | 43.2 | -1.98 | -101.3 | -155.8 | -72.7 |
| neutral verdicts, traded against the tape | 296 | 17 | +77.3 | +128.8 | +50.2 | 55.1 | +1.48 | +75.3 | +129.8 | +46.7 |
| fade every non-agreement (disagree OR neutral) | 676 | 38 | +114.3 | +203.6 | +68.6 | 55.3 | +3.42 | +153.3 | +258.2 | +99.6 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 1787 | 100 | +129.4 | +160.0 | +113.5 | 56.5 | +6.47 | +169.1 | +193.8 | +156.3 |
| gate AND eps agrees | 636 | 36 | +99.1 | +73.6 | +112.4 | 55.7 | +3.25 | +152.3 | +98.3 | +180.5 |
| gate AND revenue agrees | 632 | 35 | +133.4 | +77.7 | +162.9 | 57.1 | +4.35 | +203.2 | +78.7 | +269.3 |
| gate AND eps AND revenue agree | 505 | 28 | +114.0 | +66.5 | +137.5 | 56.2 | +3.42 | +169.0 | +57.3 | +224.1 |
| gate AND guidance agrees | 683 | 38 | +124.0 | +82.4 | +146.1 | 54.8 | +3.79 | +188.4 | +127.7 | +220.6 |
| gate AND no quality flags (hard veto) | 6 | 0 | +220.2 | +nan | +220.2 | 50.0 | +0.85 | +63.8 | +nan | +63.8 |
| gate, quality flags INVERT the side | 1111 | 62 | -162.1 | -160.0 | -163.2 | 41.2 | -6.50 | -203.9 | -181.3 | -215.8 |

Read the two horizon blocks against each other. The vetoed bucket gets monotonically worse with holding period while the gated one is flat, so if the refusals really contain a tradeable signal the fade must IMPROVE from T+1 to T+3. That is the single prediction this table exists to test.

## The fade, under scrutiny

This is the mutation that would change the strategy, so it gets the hostile treatment: does it survive a wider spread, does it hold in both halves, and is it one side of the book or both?

| holding period | trades | mean bp | 2021-23 | 2024-26 | win% | t |
|---|---|---|---|---|---|---|
| T+1 | 380 | +143.2 | +263.7 | +82.7 | 55.5 | +3.30 |
| T+2 | 380 | +178.2 | +300.1 | +117.0 | 56.1 | +3.45 |
| T+3 | 380 | +214.1 | +361.4 | +140.1 | 57.6 | +3.77 |
| T+4 | 380 | +190.8 | +335.2 | +118.3 | 57.1 | +3.12 |
| T+5 | 380 | +185.6 | +340.6 | +107.7 | 56.8 | +3.01 |

### Cost sensitivity

Fading a 5%+ after-hours move means crossing the book in the direction nobody wants. The study's flat 13bp is the friendliest assumption available; these are the same trades priced progressively worse.

| round-trip cost | T+1 | T+3 | T+3 2024-26 |
|---|---|---|---|
| 13bp | +143.2 | +214.1 | +140.1 |
| 25bp | +131.2 | +202.1 | +128.1 |
| 40bp | +116.2 | +187.1 | +113.1 |
| 60bp | +96.2 | +167.1 | +93.1 |
| 100bp | +56.2 | +127.1 | +53.1 |

### By confidence, by side, by year

| slice | trades | T+1 | T+3 | win% (T+3) |
|---|---|---|---|---|
| high confidence | 9 | +559.5 | +785.1 | 88.9 |
| medium confidence | 295 | +123.1 | +183.2 | 56.3 |
| low confidence | 76 | +172.0 | +266.4 | 59.2 |
| fading an UP tape (short) | 163 | +122.1 | +204.2 | 60.7 |
| fading a DOWN tape (long) | 217 | +159.0 | +221.5 | 55.3 |
| |reaction| >= 10% | 84 | +349.7 | +449.3 | 63.1 |
| year 2016 | 14 | -126.6 | -142.4 | 42.9 |
| year 2017 | 23 | +84.3 | +153.7 | 60.9 |
| year 2018 | 18 | +199.5 | +56.5 | 66.7 |
| year 2019 | 26 | +384.9 | +437.0 | 76.9 |
| year 2020 | 21 | -31.7 | -99.4 | 61.9 |
| year 2021 | 39 | +411.3 | +493.1 | 69.2 |
| year 2022 | 47 | +332.7 | +451.9 | 59.6 |
| year 2023 | 41 | +44.0 | +132.4 | 56.1 |
| year 2024 | 49 | +155.8 | +189.8 | 53.1 |
| year 2025 | 58 | +44.7 | +104.1 | 46.6 |
| year 2026 | 44 | -54.6 | +188.2 | 52.3 |

## Account model

Every mutation compounded through the real calendar at 30% average deployed capital, per-name weight normalised by the average number of concurrent positions so a longer hold does not silently commit more capital. T+1, unbracketed.

| mutation | trades | total | CAGR | maxDD | Sharpe | alpha | beta |
|---|---|---|---|---|---|---|---|
| THE GATE (verdict agrees with tape) | 1111 | +727.9% | 22.21% | 26.6% | 1.58 | +21.05% | -0.009 |
| pure tape (mechanical, ungated) | 1787 | +76.5% | 5.54% | 34.3% | 0.49 | +6.46% | -0.020 |
| anti-tape baseline (fade EVERY event, no model) | 1787 | -71.3% | -11.18% | 73.3% | -0.87 | -11.32% | 0.020 |
| fade only: trade every disagreement | 380 | +182.8% | 10.36% | 13.1% | 1.07 | +10.16% | 0.012 |
| fade only: high|medium confidence | 304 | +119.8% | 7.76% | 10.8% | 0.91 | +7.83% | 0.001 |
| pure LLM direction (gate + fade, tape ignored) | 1491 | +1078.8% | 26.37% | 19.6% | 1.91 | +24.21% | -0.000 |
| gate + fade on high confidence | 1120 | +777.6% | 22.88% | 26.4% | 1.63 | +21.58% | -0.008 |
| neutral verdicts, traded with the tape | 296 | -48.5% | -6.10% | 55.8% | -0.58 | -5.64% | -0.012 |
| neutral verdicts, traded against the tape | 296 | +49.9% | 3.92% | 24.2% | 0.44 | +4.18% | 0.012 |
| fade every non-agreement (disagree OR neutral) | 676 | +262.5% | 13.00% | 19.8% | 1.08 | +12.65% | 0.022 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 1787 | +1077.8% | 26.36% | 17.7% | 1.91 | +24.12% | 0.007 |
| gate AND eps agrees | 636 | +202.8% | 11.08% | 27.7% | 0.96 | +11.04% | 0.011 |
| gate AND revenue agrees | 632 | +354.8% | 15.45% | 25.6% | 1.29 | +14.93% | 0.009 |
| gate AND eps AND revenue agree | 505 | +197.5% | 10.90% | 24.5% | 1.01 | +10.83% | 0.007 |
| gate AND guidance agrees | 683 | +315.9% | 14.48% | 22.3% | 1.10 | +14.63% | -0.020 |
| gate, quality flags INVERT the side | 1111 | -93.1% | -22.42% | 93.6% | -1.84 | -24.57% | 0.007 |

SPY buy & hold over the same span: +377.9% total, 16.00% CAGR, 33.8% max drawdown.

Quality flags fire on 99% of releases, so the live 0.75x size shrink is very nearly a constant and cannot be doing discriminating work.
