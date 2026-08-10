# Late premarket -> the auction print — 20260810_0453

6,836 gap events with premarket internals. `aligned` = the 09:15->09:29 premarket move (>=20bp) agrees with the gap's sign; `fading` = it disagrees (>=20bp against). Entries: the 09:30 auction print (a 09:28 MOO order's own fill) and the 09:31 close. Trade WITH the gap sign; 6bp charged. `edge` = cell minus the unconditional same-window mean over all events — the conditioning must beat not-conditioning.

## Entry: auction open (09:28 MOO), trading WITH the gap

| cell | window | n | bp | t | edge bp | net |
|---|---|---|---|---|---|---|
| aligned (PM still going) | +1m | 3,725 | +5.2 | +1.77 | +9.6 | -0.8 |
| fading (PM turned) | +1m | 1,919 | -23.9 | -5.85 | -19.6 | -29.9 |
| aligned (PM still going) | +5m | 3,724 | +6.6 | +1.60 | +9.1 | +0.6 |
| fading (PM turned) | +5m | 1,922 | -22.3 | -3.93 | -19.8 | -28.3 |
| aligned (PM still going) | +15m | 3,729 | +0.2 | +0.04 | +9.2 | -5.8 |
| fading (PM turned) | +15m | 1,921 | -30.3 | -4.22 | -21.3 | -36.3 |
| aligned (PM still going) | +30m | 3,731 | -3.8 | -0.64 | +7.0 | -9.8 |
| fading (PM turned) | +30m | 1,922 | -32.6 | -3.85 | -21.8 | -38.6 |
| aligned (PM still going) | 11:00 | 3,728 | +0.8 | +0.12 | +13.1 | -5.2 |
| fading (PM turned) | 11:00 | 1,924 | -42.4 | -4.02 | -30.1 | -48.4 |
| aligned (PM still going) | close | 3,733 | -5.6 | -0.60 | +10.7 | -11.6 |
| fading (PM turned) | close | 1,925 | -45.2 | -3.22 | -29.0 | -51.2 |

## Entry: 09:31 close, trading WITH the gap

| cell | window | n | bp | t | edge bp | net |
|---|---|---|---|---|---|---|
| aligned (PM still going) | +1m | 3,725 | +0.0 | +nan | +0.0 | -6.0 |
| fading (PM turned) | +1m | 1,919 | +0.0 | +nan | +0.0 | -6.0 |
| aligned (PM still going) | +5m | 3,718 | +1.5 | +0.52 | -0.2 | -4.5 |
| fading (PM turned) | +5m | 1,916 | +1.2 | +0.28 | -0.5 | -4.8 |
| aligned (PM still going) | +15m | 3,721 | -3.8 | -0.86 | +0.2 | -9.8 |
| fading (PM turned) | +15m | 1,915 | -7.3 | -1.20 | -3.3 | -13.3 |
| aligned (PM still going) | +30m | 3,723 | -8.1 | -1.54 | -2.2 | -14.1 |
| fading (PM turned) | +30m | 1,916 | -9.0 | -1.20 | -3.2 | -15.0 |
| aligned (PM still going) | 11:00 | 3,720 | -4.0 | -0.60 | +3.7 | -10.0 |
| fading (PM turned) | 11:00 | 1,918 | -18.9 | -1.95 | -11.2 | -24.9 |
| aligned (PM still going) | close | 3,725 | -10.1 | -1.14 | +1.4 | -16.1 |
| fading (PM turned) | close | 1,919 | -21.8 | -1.62 | -10.3 | -27.8 |

## Aligned cell, auction entry, by direction and year (window +15m)

| slice | n | bp | t | net |
|---|---|---|---|---|
| gap UP & PM rising (long at auction) | 1,893 | -8.5 | -1.19 | -14.5 |
| gap DOWN & PM falling (short at auction) | 1,836 | +9.2 | +1.15 | +3.2 |
| 2022 (both sides) | 846 | -6.9 | -0.61 | -12.9 |
| 2023 (both sides) | 747 | +8.3 | +0.76 | +2.3 |
| 2024 (both sides) | 760 | +2.3 | +0.23 | -3.7 |
| 2025 (both sides) | 855 | +2.6 | +0.20 | -3.4 |
| 2026 (both sides) | 521 | -6.9 | -0.45 | -12.9 |
