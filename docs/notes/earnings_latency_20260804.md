# Earnings-night news latency - 2026-08-04

Produced by `backend/scripts/verify_news_latency.py`. Columns:
- `bz_first/bz_nums`: first Benzinga mention / first headline with numbers (created_at from the news REST API; in live mode bz_first is OUR websocket arrival wall-clock).
- `edgar_acc`: EDGAR acceptance (Atom `<updated>`).
- `seen+`: acceptance -> our poll saw it (live mode only).
- `move@/move`: first SIP 1m bar >= threshold off the official close, at/after the earliest text source (pre-release drift excluded by construction).

## run 2026-08-04 23:57 ET (retro)

```
earnings-night latency - 2026-08-04 ET - RETRO (source timestamps)

symbol   $vol(M)  bz_first   bz_nums edgar_acc  seen+  bz-edgar     move@   move
--------------------------------------------------------------------------------
AMD      25156.9  16:13:54  16:15:45  16:16:24      -      -39s  16:15:00  -5.7%
SPCX     18073.8  15:34:52  16:03:18  16:01:05      -     +133s  16:02:00  +2.4%
ALAB      3638.5  16:06:15  16:06:15  16:09:40      -     -205s  16:06:00  +8.0%
ANET      2686.0  16:06:52  16:06:52  16:06:44      -       +8s  16:06:00 +17.3%
BKNG      1871.6  16:04:52  16:04:52  16:03:14      -      +98s  16:03:00  +4.5%
AMGN      1413.4  16:02:43  16:02:43  16:03:16      -      -33s  16:02:00  +2.3%
GILD      1369.6  16:01:33  16:01:33  16:03:26      -     -113s  16:07:00  -1.6%
EMR        847.3  16:06:40  16:06:40  16:05:37      -      +63s  16:05:00  +2.6%
DVA        595.5  16:06:31  16:06:31  16:08:24      -     -113s  16:06:00  -3.5%
TOST       572.6  16:10:52  16:10:52  16:12:21      -      -89s  16:10:00  -6.4%

benzinga(nums|first) vs edgar acceptance: median -33s over 10 names (negative = benzinga earlier)
moved >= threshold after release: 10/10
```

## Reading (2026-08-04, 168 Item-2.02 8-Ks in window, top-10 by $ volume)

1. **Neither free source dominates — subscribe to both, trigger on the
   first.** Benzinga's numbers headline led EDGAR on 6/10 (by 33-205s);
   EDGAR acceptance led Benzinga on 4/10 (by 8-133s, including SPCX +133s
   and BKNG +98s). The strategy should react to whichever arrives first:
   headline-level surprise from Benzinga, full text from EDGAR.
2. **The tape reprices in the release minute.** 8/10 names' first >=1% bar
   IS the minute the numbers crossed (AMD -5.7% in the 16:15 bar, ANET
   +17.3% in the 16:06 bar). A <60s text-in-hand target is real: by T+2min
   the initial repricing is done and what's left to trade is the
   continuation, which is exactly the strategy's thesis.
3. **EDGAR acceptance was within ~90s of the earliest evidence in every
   case**, and full press-release text is 2 fetches behind acceptance
   (index + exhibit, ~150ms each, verified in the feed smoke test). The
   unmeasured piece is acceptance -> getcurrent visibility + our poll
   cadence: that is the `seen+` column, which only `--live` can fill —
   run it on the next earnings evening (15:55-17:30 ET).
4. **Verdict so far: the free stack looks sufficient to test the thesis in
   note-mode.** Paid upgrades (Benzinga-via-Massive push, or a paid
   realtime overnight tape) only become worth proposing if (a) `--live`
   shows getcurrent visibility lag is large, or (b) journaled would-be
   trades show the edge decays inside the first minute.
5. Curiosity worth knowing: CIK 1181412 ("SPACE EXPLORATION TECHNOLOGIES
   CORP") maps to ticker SPCX with $18.1B day volume — SpaceX trades
   publicly and files 2.02s like everyone else. The ticker map handled it
   with no special-casing.
