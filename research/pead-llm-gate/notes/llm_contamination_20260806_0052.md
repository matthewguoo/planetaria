# LLM gate, 5 years, claude-opus-5 — contamination-controlled

universe: 1560 gate-5% events, top-5/day above $50M, 2021-08-02..2026-07-30
costs 13bp round trip; mechanical baseline is the same trades ungated

## arm: named  (n=1552)
mechanical +48.8bp  |  LLM-gated n=1072 +185.7bp  |  vetoed n=480 -256.9bp  |  spread +442.5bp
permutation null (shuffle verdicts within month, 1000x): p=0.000
gated t=6.15 · keeps 69% of events · win rate 57.0% vs 52.3% ungated
by side: long n=589 +178.0bp · short n=483 +195.1bp
verdict mix: bullish 48%, bearish 37%, neutral 15%

| year | n | mech | gated | vetoed | spread |
|---|---|---|---|---|---|
| 2021 | 115 | +139.0 | +264.4 | -159.7 | +424.1 |
| 2022 | 291 | -147.3 | +8.8 | -501.4 | +510.2 |
| 2023 | 259 | +143.2 | +250.6 | -158.5 | +409.1 |
| 2024 | 345 | +104.3 | +230.1 | -262.9 | +492.9 |
| 2025 | 338 | +91.6 | +194.6 | -143.4 | +338.0 |
| 2026 | 204 | -6.9 | +218.3 | -250.5 | +468.8 |

contamination gradient: gated P&L vs event age = -28.5bp/year (SE 21.6). A memorised edge should RISE with age; flat is evidence against it.

## what this does NOT control for

- The `blind` and `notext` arms are the DIRECT contamination controls. Until they run, the evidence against memorisation is indirect: the age gradient and the relative strength of the least-contaminated year.
- Regime priors survive any scrub. A model trained on text written after these events knows what kind of company this is, even recalling no specific release.
- Survivorship: the universe is built from today's SEC ticker map, so issuers fully delisted since are absent. This flatters the bear regime most.
- Costs are a flat 13bp round trip, not a per-name spread model, and the entry assumes a fill AT the reaction print. The live SIP problem says that fill is the part still unproven.
- Consensus reads 'unknown' in every prompt. Live has real consensus for ~2/3 of names, so this understates the information the live strategy actually gets.
- Exits are the panel's next-session 15:55 close. Intraday TP/SL brackets are not walked anywhere in this study.

measured API spend: $37.17
