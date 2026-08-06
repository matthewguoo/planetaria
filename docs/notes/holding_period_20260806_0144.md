# Holding period study — 20260806_0144

1537 scored events (1064 gated), corrected per-session exits, 13bp round trip. Same-bar bracket ties resolve to the stop; a bracket only exits if touched before the horizon's close.

## Fixed holding periods, no bracket (bp per trade)

| hold | gated | 2021-23 | 2024-26 | win% | vetoed | spread |
|---|---|---|---|---|---|---|
| T+1 | +167.4 | +143.5 | +186.6 | 57.4 | -195.6 | +363.1 |
| T+2 | +179.4 | +140.4 | +210.7 | 57.3 | -235.4 | +414.8 |
| T+3 | +175.3 | +135.2 | +207.5 | 56.9 | -277.3 | +452.7 |
| T+4 | +169.1 | +121.9 | +207.0 | 56.5 | -282.9 | +452.0 |
| T+5 | +170.3 | +114.7 | +214.9 | 56.3 | -266.3 | +436.6 |

## Fixed horizons with the shipped-style bracket (5% stop / 2x target)

| hold | gated | stopped% | target% | ran to close% |
|---|---|---|---|---|
| T+1 | +16.0 | 49.6 | 21.3 | 29.0 |
| T+2 | +16.6 | 53.5 | 24.3 | 22.2 |
| T+3 | +19.6 | 55.2 | 26.9 | 18.0 |
| T+4 | +21.4 | 56.4 | 27.6 | 16.0 |
| T+5 | +21.1 | 57.0 | 28.3 | 14.7 |

## Conditional holding period

Base hold T+1, extended to T+K when the rule fires. PEAD's claim is that drift scales with the size of the surprise and with how much of it is unabsorbed, so these extend on signal strength and on confirmation rather than on a fixed clock.

### extend to T+2

| rule | fires% | gated | 2021-23 | 2024-26 | win% |
|---|---|---|---|---|---|
| always T+1 (shipped horizon) | 0 | +167.4 | +143.5 | +186.6 | 57.4 |
| always T+3 | 100 | +179.4 | +140.4 | +210.7 | 57.3 |
| |reaction| >= 10% | 42 | +168.8 | +130.1 | +199.9 | 57.5 |
| high confidence | 16 | +169.0 | +134.9 | +196.5 | 56.8 |
| guidance raised or lowered | 69 | +185.8 | +151.9 | +213.0 | 57.9 |
| no quality flags | 1 | +166.3 | +143.4 | +184.7 | 57.2 |
| not already priced in (|run5d| < 5%) | 59 | +165.2 | +135.6 | +189.0 | 56.7 |
| T+1 confirmed (closed in favour) | 57 | +179.5 | +144.1 | +207.9 | 50.7 |
| T+1 confirmed AND |reaction| >= 10% | 25 | +163.2 | +131.5 | +188.7 | 54.3 |
| T+1 confirmed AND high confidence | 12 | +168.4 | +137.4 | +193.2 | 56.2 |

### extend to T+3

| rule | fires% | gated | 2021-23 | 2024-26 | win% |
|---|---|---|---|---|---|
| always T+1 (shipped horizon) | 0 | +167.4 | +143.5 | +186.6 | 57.4 |
| always T+3 | 100 | +175.3 | +135.2 | +207.5 | 56.9 |
| |reaction| >= 10% | 42 | +165.2 | +117.8 | +203.3 | 57.0 |
| high confidence | 16 | +165.3 | +127.5 | +195.7 | 56.4 |
| guidance raised or lowered | 69 | +186.8 | +157.3 | +210.4 | 56.9 |
| no quality flags | 1 | +166.4 | +142.3 | +185.7 | 57.3 |
| not already priced in (|run5d| < 5%) | 59 | +169.3 | +147.3 | +186.9 | 56.2 |
| T+1 confirmed (closed in favour) | 57 | +172.1 | +131.8 | +204.5 | 47.8 |
| T+1 confirmed AND |reaction| >= 10% | 25 | +154.5 | +115.1 | +186.1 | 52.8 |
| T+1 confirmed AND high confidence | 12 | +164.7 | +132.4 | +190.6 | 55.6 |

### extend to T+5

| rule | fires% | gated | 2021-23 | 2024-26 | win% |
|---|---|---|---|---|---|
| always T+1 (shipped horizon) | 0 | +167.4 | +143.5 | +186.6 | 57.4 |
| always T+3 | 100 | +170.3 | +114.7 | +214.9 | 56.3 |
| |reaction| >= 10% | 42 | +158.4 | +117.4 | +191.4 | 56.9 |
| high confidence | 16 | +168.4 | +127.1 | +201.6 | 56.1 |
| guidance raised or lowered | 69 | +183.2 | +130.9 | +225.3 | 56.5 |
| no quality flags | 1 | +164.7 | +139.7 | +184.8 | 57.1 |
| not already priced in (|run5d| < 5%) | 59 | +167.9 | +149.8 | +182.4 | 57.0 |
| T+1 confirmed (closed in favour) | 57 | +156.2 | +117.6 | +187.2 | 45.1 |
| T+1 confirmed AND |reaction| >= 10% | 25 | +142.7 | +105.1 | +172.9 | 51.6 |
| T+1 confirmed AND high confidence | 12 | +161.0 | +123.8 | +190.8 | 55.0 |

Read the two period columns together: a rule that only wins on 2021-23 is fitted to it. `always T+1` and `always T+3` are the unconditional baselines every rule has to beat.
