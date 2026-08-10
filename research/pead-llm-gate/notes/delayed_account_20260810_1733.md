# Delayed-entry mech PEAD, 6-slot account (OOS 2019-26) — 20260810_1733

1518 OOS events; entry accept+60m, exit T+1 15:55, tape sides, net 13bp; 6 equal-weight slots, slot contention by dollar volume. P&L lands on the exit session; flat days included. The 23.2bp column restates the measured AH round trip (entry an hour after the release is still AH).

| arm | trades | net bp/tr | t | win% | net@23.2 | ann ret % | Sharpe | maxDD % | alpha %/yr | beta |
|---|---|---|---|---|---|---|---|---|---|---|
| ungated +60m | 1518 | +39.5 | +2.01 | 54 | +29.3 | +12.23 | +0.72 | 42.5 | +14.12 | -0.050 |
| P>=0.45 gate | 923 | +81.2 | +3.33 | 56 | +71.0 | +16.75 | +1.16 | 17.6 | +16.39 | +0.006 |

## By year, net bp/trade (ungated | gated)

| year | ungated n | bp | t | gated n | bp | t |
|---|---|---|---|---|---|---|
| 2019 | 110 | -11.6 | -0.21 | 67 | -28.1 | -0.44 |
| 2020 | 110 | +19.2 | +0.34 | 58 | +122.4 | +1.72 |
| 2021 | 197 | +41.0 | +0.68 | 116 | +158.5 | +1.98 |
| 2022 | 239 | -94.6 | -1.61 | 126 | -11.4 | -0.17 |
| 2023 | 179 | +74.2 | +1.38 | 113 | +85.0 | +1.25 |
| 2024 | 253 | +88.7 | +2.17 | 161 | +76.1 | +1.66 |
| 2025 | 276 | +139.8 | +3.13 | 177 | +189.1 | +3.26 |
| 2026 | 154 | -3.6 | -0.05 | 105 | -24.2 | -0.27 |

The gate's threshold carries best-of-family risk (wick study's own limitations note); the ungated row is the selection-free floor.

_Provenance: `research_delayed_account.py` at 67cc8c4+dirty, 2026-08-10 17:34 ET_
