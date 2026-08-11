# close_fade pre-registration battery — 20260811_0228

2,562 long signals (late <= -50bp). Earnings flags from the events_v2 panel (8-K acceptances, 2016-26): `reports_tonight` = same symbol accepted >= 15:30 that day; `reported_recently` = accepted the prior day or pre-open same day.

## Earnings x-ray (gross bp to next open)

| slice | n | bp | t | net@5 |
|---|---|---|---|---|
| ALL long signals | 2,562 | +26.9 | +4.07 | +21.9 |
| reports TONIGHT (warehouses the print) | 48 | -26.6 | -0.18 | -31.6 |
| reported recently (post-earnings drift) | 97 | +50.7 | +1.72 | +45.7 |
| CLEAN (no earnings adjacency) | 2,417 | +27.0 | +4.32 | +22.0 |

## Sub-split family on the CLEAN set (report in full; x9 family)

| slice | n | bp | t | net@5 |
|---|---|---|---|---|
| depth 50-75bp | 1,361 | +9.8 | +1.49 | +4.8 |
| depth 75-100bp | 530 | +22.6 | +1.93 | +17.6 |
| depth 100-150bp | 329 | +43.9 | +2.30 | +38.9 |
| depth >= 150bp | 197 | +129.2 | +3.09 | +124.2 |
| Mon | 344 | +14.7 | +0.77 | +9.7 |
| Tue-Thu | 1,611 | +39.6 | +5.39 | +34.6 |
| Fri (holds the weekend) | 462 | -8.0 | -0.57 | -13.0 |
| 2022 clean | 624 | -5.4 | -0.48 | -10.4 |
| 2023 clean | 252 | +13.4 | +0.78 | +8.4 |
| 2024 clean | 509 | +45.1 | +3.36 | +40.1 |
| 2025 clean | 629 | +9.5 | +0.83 | +4.5 |
| 2026 clean | 403 | +89.9 | +4.67 | +84.9 |

## Slot-level account sim (CLEAN set, top-4/day by depth, 25%/slot, @5bp)

- 1,498 slot-trades on 671 active days of 1,149: ann +8.44%, Sharpe +0.49, maxDD 26.0%, beta -0.054

Overnight beta note: every slot holds the close->open gap; the beta above is the measured net-of-conditioning number, not an assumption.

_Provenance: `research_close_fade.py xray` at 0916a9d, 2026-08-11 02:28 ET_
