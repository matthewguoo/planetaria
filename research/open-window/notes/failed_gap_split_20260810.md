# The failed-gap fade, split — 2026-08-10

Direction and year splits of the premarket_auction note's `fading` cell:
|gap| >= 1.5%, late-premarket (09:15->09:29) moved >= 20bp AGAINST the
gap, enter AT THE AUCTION PRINT against the gap. net10 charges 10bp (MOO
entry pays no spread; the exit crosses a gapped book).

## Exit +1 minute (the snap-back)

| slice | n | bp | t | net@10 |
|---|---|---|---|---|
| ALL fading | 1,919 | +23.9 | +5.85 | +13.9 |
| gap UP, PM turned down -> SHORT | 1,043 | +23.6 | +4.26 | +13.6 |
| gap DOWN, PM turned up -> LONG | 876 | +24.3 | +4.01 | +14.3 |
| 2022 | 352 | +22.8 | +2.40 | +12.8 |
| 2023 | 379 | +4.6 | +0.63 | -5.4 |
| 2024 | 433 | +17.3 | +2.22 | +7.3 |
| 2025 | 475 | +37.4 | +3.94 | +27.4 |
| 2026 | 280 | +39.2 | +3.31 | +29.2 |

## Longer holds

| leg | exit | n | bp | t | net@10 |
|---|---|---|---|---|---|
| SHORT (gap up, PM down) | +5m | 1,046 | +25.8 | +3.42 | +15.8 |
| LONG (gap down, PM up) | +5m | 876 | +18.0 | +2.10 | +8.0 |
| SHORT | close | 1,048 | +67.7 | +3.67 | +57.7 |
| LONG | close | 877 | +18.4 | +0.86 | +8.4 |

The LONG leg's edge is ONLY the auction minute (close-exit years swing
-45 to +73 — noise); the SHORT leg stacks the familiar all-day down-drift
on top and can ride. Design consequence: long leg exits by 09:31-09:35;
the short leg (shorts-gated) may hold.

## LONG leg (runnable on the current account), exit +1m, by year

| year | n | bp | t | net@10 |
|---|---|---|---|---|
| 2022 | 186 | +36.8 | +2.68 | +26.8 |
| 2023 | 174 | -14.7 | -1.29 | -24.7 |
| 2024 | 197 | +6.8 | +0.63 | -3.2 |
| 2025 | 194 | +51.8 | +3.54 | +41.8 |
| 2026 | 125 | +45.0 | +2.58 | +35.0 |

## Reading

- The whole edge lives AT the auction: the 09:31-entry table in the main
  note is flat-to-negative everywhere. A 09:28 MOO order is the entire
  advantage — no human clicks it, and one minute late is too late.
- Occupancy math: ~190 long events/yr (0.76/day), exposure ~1 minute.
  Per-account Sharpe ~1.1 standalone on the long leg; ~2+ with both legs
  at full deployment (t 5.85 over 4.6y). Capital is free 99.9% of the
  day, so it stacks on every other sleeve.
- Honest marks against it: 2023 was negative on the long leg (one of
  five); the exit cost on a gapped book is the soft assumption (10bp
  charged; measure live); auction-fill realism is exact for MOO but the
  premarket read needs IEX quotes 09:15-09:29 (inside IEX hours — works
  on the current tier).
- Next per the authoring brief: pre-registration, then `gap_fail_fade`
  (premarket scanner 09:25-09:29, MOO at 09:28, exit 09:31-09:35 for the
  long leg), note-mode first.
