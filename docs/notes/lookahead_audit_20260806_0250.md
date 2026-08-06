# Lookahead audit — 2026-08-06 02:50 ET

1552 scored events, 2021-08-02..2026-07-30. Claim under test: every input the model saw was available at 16:20 ET on the announce day, and nothing derived from the outcome reached the prompt, the gate, or the selection.

**0/1 checks passed.** [FAIL] 2

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

