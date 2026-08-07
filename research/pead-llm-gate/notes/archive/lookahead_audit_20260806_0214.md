# Lookahead audit — 2026-08-06 02:13 ET

1552 scored events, 2021-08-02..2026-07-30. Claim under test: every input the model saw was available at 16:20 ET on the announce day, and nothing derived from the outcome reached the prompt, the gate, or the selection.

**6/7 checks passed.** [PASS] 1 [FAIL] 2 [PASS] 3 [PASS] 4 [PASS] 5 [PASS] 6 [PASS] 7

## [PASS] 1. prompt contents carry no outcome

rebuilt 1552 named requests (0 skipped: no cached release text)
outcome-perturbation: 0 requests changed when anchor/react/exit/move_pct/fwd_bp were replaced with noise
numeric scan of the authored scaffolding on 40 sampled events: 0 carried an outcome value
the tape reaction is absent from the prompt by construction — the model never sees the move it is gating

## [FAIL] 2. release-text provenance and timing

resolved the filing behind 1552/1552 scored events ({'ok': 1552})
amendments (8-K/A) used as the release text: 0 — the resolver matches form == '8-K' exactly, so an amendment cannot be selected
filings whose acceptance DATE differs from the announce day: 0

acceptance time IN ET vs the 16:20 entry, n=1552:
   before noon     25    1.6%
   12:00-15:00     50    3.2%
   15:00-16:00      0    0.0%
   16:00-16:05    237   15.3%
   16:05-16:20   1088   70.1%
   16:21-17:30    151    9.7%
   after 17:30      1    0.1%
accepted AFTER the 16:20 entry print: 152 (9.8%)
accepted BEFORE 16:00, i.e. not an after-close release at all: 75 (4.8%)

the second number is the interesting one. fetch_calendar_window classifies the hour by slicing the RAW string and comparing to 16:00, so on a UTC timestamp its 'amc' bucket really means 'accepted after 16:00 UTC' = after noon ET in summer, 11:00 ET in winter. Midday filers therefore enter the AMC calendar.

| subset | events | gated n | gated bp | vetoed bp | spread |
|---|---|---|---|---|---|
| all scored events | 1552 | 1072 | +185.7 | -256.9 | +442.5 |
| acceptance in [16:00, 16:20] ET only | 1325 | 925 | +210.3 | -274.3 | +484.6 |
| acceptance before 16:00 ET | 75 | 50 | +48.0 | +241.5 | -193.4 |
| acceptance after 16:20 ET | 152 | 97 | +21.7 | -356.2 | +377.9 |

per-event provenance written to C:\Users\matth\Desktop\planetaria\backend\scripts\_leadup_cache\lookahead_provenance.parquet

  refetched CLS 2026-04-27 (0001030894-26-000030): byte-identical
  refetched JBHT 2026-07-15 (0001437749-26-023629): byte-identical
accession-scoped EDGAR URLs are immutable: 2/2 refetches reproduced the cached text exactly

## [PASS] 3. run-up and liquidity features are prior-session only

recomputed run5d/dv from the raw daily panels for 1552/1552 scored events (0 skipped: symbol absent from the covering panel, or fewer than 6 prior sessions)
disagreements with the shipped feature: 0
events whose newest reachable bar was NOT strictly older than the announce day: 0
FUTURE-ERASURE TEST — every price and volume from the announce day forward set to NaN: 1552 features unchanged, 0 moved
index i is located by date, then only c[i-1], c[i-6] and dv[i-1] are read — i itself never enters either feature

## [PASS] 4. universe selection and survivorship

build_universe_window ranks on [win_start, win_start+10d]: True
  universe_2021-08-01.parquet: 1000 tickers ranked at the window open
  universe_2022-01-01.parquet: 1000 tickers ranked at the window open
  universe_2023-01-01.parquet: 1000 tickers ranked at the window open
  universe_2024-01-01.parquet: 1000 tickers ranked at the window open
  universe_2025-02-01.parquet: 1000 tickers ranked at the window open
per-day top-5 is ranked on the PRIOR session's dollar volume (check 3 proves that value survives erasing the future)
power of that statement: ranking the same 373 days on the announce day's OWN dollar volume changes the chosen names on 158 of them (42%) — the two rankings are genuinely different objects, so the invariance is not vacuous
gate |reaction| >= 5% is applied to the announce day's own tape (that IS the signal, not a lookahead); the $50M floor uses the prior session's volume

EDGAR calendar covers 848 distinct tickers; 0 are absent from today's company_tickers.json
that count is structurally ZERO and is NOT a clean bill of health: the calendar was BUILT from today's ticker map (build_universe_window reads company_tickers.json), so an issuer delisted before today could never have entered it. The survivorship is in the map, not in the filter, and cannot be measured from these artefacts — only stated.
scored universe: 337 distinct issuers over 2021-08-02..2026-07-30

## [PASS] 5. no retrieval channel

scanned 1552 rebuilt request payloads for a tools / tool_choice / mcp_servers key: 0 found
output_config.format.type == 'json_schema' on 1552/1552 — the schema-constrained path emits one JSON object and has no tool-call channel to invoke search with

read 1736 returned messages from the Batch API: 0 non-text content blocks, 0 with stop_reason 'tool_use' — no retrieval happened

## [PASS] 6. entry print is at or before 16:20 ET

refetched minute bars for 40 of 40 sampled events (0 had no usable tape in the window)
cached `react` reproduced from the raw tape: 40/40 (mismatches 0)
cached `anchor` mismatches: 0
entry print stamped after 16:20 ET: 0 — latest observed 16:20 ET
the reaction window is a hard [16:00, 16:20] slice in research_pead_backtest, so this is true by construction; the refetch confirms the cache was built by that code and not edited afterwards

## [PASS] 7. no post-release commentary in the release texts

scanned 1552 cached release texts for post-release market commentary: 0 matches
text length: median 12000 chars, 97% truncated at the 12k max_text ceiling, 0 under 500 chars
five texts read end to end by hand are listed in the note; this scan covers all of them mechanically

