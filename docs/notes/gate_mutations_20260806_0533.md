# Gate mutations — 20260806_0533

The shipped gate trades the tape when the verdict agrees and stands down otherwise. Each mutation below spends the same verdicts differently. Chosen on nothing — every row is reported, including the failures.

722 scored events, 320 in 2021-23 / 402 in 2024-26. Costs 13bp round trip. No bracket unless stated.

## Every mutation, unbracketed, at two horizons

| mutation | trades | % of book | T+1 all | 21-23 | 24-26 | win% | t | T+3 all | 21-23 | 24-26 |
|---|---|---|---|---|---|---|---|---|---|---|
| SHIPPED gate (verdict agrees with tape) | 470 | 65 | +157.1 | +133.8 | +176.6 | 57.2 | +3.90 | +163.7 | +94.5 | +221.6 |
| pure tape (mechanical, ungated) | 722 | 100 | +31.6 | -11.6 | +65.9 | 52.6 | +0.92 | -21.1 | -125.7 | +62.2 |
| anti-tape baseline (fade EVERY event, no model) | 722 | 100 | -57.6 | -14.4 | -91.9 | 45.7 | -1.68 | -4.9 | +99.7 | -88.2 |
| fade only: trade every disagreement | 146 | 20 | +185.6 | +285.7 | +103.0 | 54.1 | +2.33 | +384.3 | +554.4 | +244.0 |
| fade only: high confidence | 5 | 1 | -201.8 | -294.3 | -63.1 | 40.0 | -0.46 | +55.5 | -183.0 | +413.3 |
| fade only: high|medium confidence | 115 | 16 | +150.9 | +215.7 | +95.6 | 51.3 | +1.78 | +337.4 | +485.8 | +210.6 |
| pure LLM direction (gate + fade, tape ignored) | 616 | 85 | +163.8 | +169.6 | +159.1 | 56.5 | +4.54 | +216.0 | +202.9 | +226.9 |
| gate + fade on high confidence | 475 | 66 | +153.3 | +127.9 | +174.7 | 57.1 | +3.82 | +162.6 | +90.7 | +223.1 |
| neutral verdicts, traded with the tape | 106 | 15 | -190.2 | -294.4 | -127.1 | 44.3 | -2.06 | -304.5 | -553.7 | -153.5 |
| neutral verdicts, traded against the tape | 106 | 15 | +164.2 | +268.4 | +101.1 | 55.7 | +1.78 | +278.5 | +527.7 | +127.5 |
| fade every non-agreement (disagree OR neutral) | 252 | 35 | +176.6 | +279.2 | +102.1 | 54.8 | +2.93 | +339.8 | +544.3 | +191.3 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 722 | 100 | +163.9 | +181.9 | +149.5 | 56.4 | +4.88 | +225.2 | +243.5 | +210.6 |
| gate AND eps agrees | 263 | 36 | +153.7 | +169.7 | +140.0 | 56.7 | +3.21 | +169.4 | +162.4 | +175.4 |
| gate AND revenue agrees | 273 | 38 | +131.2 | +118.9 | +141.5 | 56.4 | +2.81 | +148.2 | +79.5 | +206.2 |
| gate AND eps AND revenue agree | 215 | 30 | +146.2 | +158.2 | +137.3 | 56.7 | +2.81 | +152.0 | +111.7 | +182.1 |
| gate AND guidance agrees | 312 | 43 | +172.2 | +146.5 | +191.4 | 55.4 | +3.35 | +188.9 | +122.8 | +238.0 |
| gate AND no quality flags (hard veto) | 2 | 0 | +477.3 | +nan | +477.3 | 50.0 | +0.68 | +428.8 | +nan | +428.8 |
| gate, quality flags INVERT the side | 470 | 65 | -178.9 | -159.8 | -194.9 | 40.9 | -4.44 | -186.0 | -120.5 | -240.7 |

Read the two horizon blocks against each other. The vetoed bucket gets monotonically worse with holding period while the gated one is flat, so if the refusals really contain a tradeable signal the fade must IMPROVE from T+1 to T+3. That is the single prediction this table exists to test.

## The fade, under scrutiny

This is the mutation that would change the strategy, so it gets the hostile treatment: does it survive a wider spread, does it hold in both halves, and is it one side of the book or both?

| holding period | trades | mean bp | 2021-23 | 2024-26 | win% | t |
|---|---|---|---|---|---|---|
| T+1 | 146 | +185.6 | +285.7 | +103.0 | 54.1 | +2.33 |
| T+2 | 146 | +276.9 | +420.6 | +158.3 | 56.8 | +2.81 |
| T+3 | 146 | +384.3 | +554.4 | +244.0 | 59.6 | +3.51 |
| T+4 | 146 | +371.8 | +534.8 | +237.2 | 58.9 | +3.10 |
| T+5 | 146 | +380.0 | +512.4 | +270.8 | 54.8 | +3.18 |

### Cost sensitivity

Fading a 5%+ after-hours move means crossing the book in the direction nobody wants. The study's flat 13bp is the friendliest assumption available; these are the same trades priced progressively worse.

| round-trip cost | T+1 | T+3 | T+3 2024-26 |
|---|---|---|---|
| 13bp | +185.6 | +384.3 | +244.0 |
| 25bp | +173.6 | +372.3 | +232.0 |
| 40bp | +158.6 | +357.3 | +217.0 |
| 60bp | +138.6 | +337.3 | +197.0 |
| 100bp | +98.6 | +297.3 | +157.0 |

### By confidence, by side, by year

| slice | trades | T+1 | T+3 | win% (T+3) |
|---|---|---|---|---|
| high confidence | 5 | -201.8 | +55.5 | 80.0 |
| medium confidence | 110 | +167.0 | +350.3 | 55.5 |
| low confidence | 31 | +314.1 | +558.3 | 71.0 |
| fading an UP tape (short) | 64 | +178.3 | +395.2 | 67.2 |
| fading a DOWN tape (long) | 82 | +191.3 | +375.8 | 53.7 |
| |reaction| >= 10% | 51 | +417.4 | +635.4 | 64.7 |
| year 2021 | 15 | +120.9 | +450.6 | 73.3 |
| year 2022 | 26 | +573.4 | +960.0 | 65.4 |
| year 2023 | 25 | +85.5 | +194.9 | 64.0 |
| year 2024 | 26 | +140.7 | +163.6 | 46.2 |
| year 2025 | 29 | +222.9 | +312.0 | 51.7 |
| year 2026 | 25 | -75.5 | +248.8 | 64.0 |

## Account model

Every mutation compounded through the real calendar at 30% average deployed capital, per-name weight normalised by the average number of concurrent positions so a longer hold does not silently commit more capital. T+1, unbracketed.

| mutation | trades | total | CAGR | maxDD | Sharpe | alpha | beta |
|---|---|---|---|---|---|---|---|
| SHIPPED gate (verdict agrees with tape) | 470 | +209.1% | 25.36% | 16.7% | 1.71 | +23.64% | -0.008 |
| pure tape (mechanical, ungated) | 722 | +26.9% | 4.89% | 32.4% | 0.44 | +5.93% | -0.031 |
| anti-tape baseline (fade EVERY event, no model) | 722 | -42.3% | -10.43% | 54.1% | -0.80 | -10.52% | 0.030 |
| fade only: trade every disagreement | 146 | +67.7% | 10.91% | 8.9% | 1.07 | +10.55% | 0.027 |
| fade only: high|medium confidence | 115 | +39.0% | 6.81% | 7.9% | 0.81 | +6.96% | -0.001 |
| pure LLM direction (gate + fade, tape ignored) | 616 | +277.8% | 30.51% | 12.9% | 1.95 | +27.49% | 0.012 |
| gate + fade on high confidence | 475 | +203.2% | 24.88% | 18.7% | 1.68 | +23.25% | -0.007 |
| neutral verdicts, traded with the tape | 106 | -34.4% | -8.10% | 35.0% | -0.94 | -7.93% | -0.013 |
| neutral verdicts, traded against the tape | 106 | +39.1% | 6.84% | 10.8% | 0.82 | +6.83% | 0.013 |
| fade every non-agreement (disagree OR neutral) | 252 | +133.6% | 18.53% | 9.5% | 1.38 | +17.38% | 0.040 |
| FULL POLICY: with the tape if the verdict agrees, against it otherwise | 722 | +306.0% | 32.41% | 8.8% | 2.12 | +28.80% | 0.019 |
| gate AND eps agrees | 263 | +117.0% | 16.79% | 16.6% | 1.39 | +15.87% | 0.029 |
| gate AND revenue agrees | 273 | +97.9% | 14.65% | 16.4% | 1.24 | +14.28% | 0.005 |
| gate AND eps AND revenue agree | 215 | +82.7% | 12.83% | 13.6% | 1.23 | +12.55% | 0.004 |
| gate AND guidance agrees | 312 | +165.4% | 21.59% | 18.1% | 1.45 | +20.58% | -0.003 |
| gate, quality flags INVERT the side | 470 | -75.0% | -24.27% | 75.7% | -1.95 | -26.87% | 0.004 |

SPY buy & hold over the same span: +88.2% total, 13.51% CAGR, 24.5% max drawdown.

Quality flags fire on 98% of releases, so the live 0.75x size shrink is very nearly a constant and cannot be doing discriminating work.
