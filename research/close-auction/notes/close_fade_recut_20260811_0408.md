# close_fade re-cut, the ONE declared config — 20260811_0408

391 signals (clean, depth >= 100bp, Tue-Thu). Fade at the close print, exit next open, net@5. Declared in the stage docstring before running; no other cut was tried.

- per trade: +93.2bp net (t +4.30, n 391, 2.6/active day)

| year | n | net bp | t |
|---|---|---|---|
| 2022 | 53 | -127.7 | -2.43 |
| 2023 | 21 | -64.4 | -0.61 |
| 2024 | 83 | +112.1 | +3.29 |
| 2025 | 147 | +18.0 | +0.56 |
| 2026 | 87 | +374.9 | +7.61 |

Slot sim (top-2 by depth, 25% each): 196 trades, ann +1.91%, Sharpe +0.27, maxDD 20.9%

Verdict rule, declared: this cut earns a pre-registration ONLY if Sharpe >= 1.0 here AND stays positive ex-2026; otherwise close_fade stays parked with no further cuts.

_Provenance: `research_close_fade.py recut` at b5486f8, 2026-08-11 04:09 ET_
