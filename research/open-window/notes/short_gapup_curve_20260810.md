# Short-the-gap-up on the top-8 minute universe: thin — 2026-08-10

Pass two of the bleed map (§11's queued hold-time curve), run on the
open-window minute panel rather than the daily-turnover universe the §11
numbers came from. v2 spec: gap in [1.5%, 4%], px >= $25, short AT the
auction print. 765 events, 2022-01..2026-08, net@8bp (borrow + costs).

| exit | n | gross bp | t | net@8 |
|---|---|---|---|---|
| +1m | 765 | +6.2 | +1.71 | −1.8 |
| +5m | 766 | +11.3 | +2.18 | +3.3 |
| +15m | 764 | +10.6 | +1.55 | +2.6 |
| +30m | 765 | +2.2 | +0.28 | −5.8 |
| 11:00 | 762 | +10.6 | +1.06 | +2.6 |
| close | 767 | −1.1 | −0.09 | −9.1 |

By year at 11:00 net@8: 2022 +6.6, 2023 −2.6, 2024 +21.7, 2025 −1.7,
2026 −63.6 (n=38). Best cell +3.3bp net at t≈0.6 after costs; years
lumpy; 2026 wrong-signed.

VERDICT: not a standalone sleeve on this universe. The §11 daily-universe
form ("thin standalone", ~40-90 candidates/day) remains the only live
version of this idea, unchanged. The earnings-morning subset is +57bp at
+1m but n=27 — an anecdote to fold into the day2/PEAD family studies, not
a signal. Joins the anti-queue as measured for the top-8 minute form.

_Provenance: ad-hoc against open_paths.parquet + events_v2 calendars,
2026-08-10 evening session; spec from research/notes/alpha_scan_20260810.md §11._
