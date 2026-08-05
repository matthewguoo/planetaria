# Runbook: first earnings-reaction night (note-mode)

Target: any weekday evening with AMC reporters (written for Wed 2026-08-05).
The strategy journals would-be trades only (`live: false`); nothing can
reach the broker. Instance `earn-night` already exists and is ENABLED in
the DB — it spawns automatically whenever the backend boots.

## One-time, before 15:00 ET (2 minutes)

1. Register a free Finnhub key: https://finnhub.io/register
2. Add one line to the root `.env`:

   ```
   FINNHUB_API_KEY=<paste it here>
   ```

   (`LLM_BACKEND=claude-cli` is already set — analyses run through the
   local Claude Code CLI on the subscription login, no Anthropic API key
   needed. Swap to `ANTHROPIC_API_KEY=...` later if per-call latency or
   subscription quota becomes annoying.)

3. Start (or restart) the backend and leave it running:

   ```
   cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
   ```

   KeepAwake now inhibits Windows idle-sleep while any strategy instance
   is running — the box will not sleep through the afternoon. Do NOT
   manually sleep the machine.

4. (Optional, 1 min) Prove the calendar shape empirically and update the
   dated comment in `earnings_calendar.py`:

   ```
   cd backend && .venv\Scripts\python.exe scripts\verify_earnings_calendar.py
   ```

## What happens by itself

| ET    | What                                                              |
|-------|-------------------------------------------------------------------|
| boot  | calendar feed fetches today+tomorrow, journals `estimate` signals |
| 15:00 | daily calendar re-fetch (dedup + republish)                       |
| 15:30 | watchlist freeze: tonight's AMC names, px0 snapshot, run-up context|
| 16:00 | EDGAR 8-K polling window opens (2.5s cadence)                     |
| 16:04 | px0 re-anchor to the post-auction tape (undecided names only)     |
| 16:00–20:00 | per watched reporter: first results-bearing source → LLM surprise read vs consensus → tape-agreement gate → `would_trade` / `no_trade` note |
| 15:55 (T+1) | the would-be exit time the notes record                     |

## Optional: live latency measurement (15:55–17:30 ET)

Fills the acceptance→seen (`seen+`) column the 8/4 retro table couldn't:

```
cd backend && .venv\Scripts\python.exe scripts\verify_news_latency.py --live --write-doc
```

## Evening review (UI)

STRATEGIES tab → click `earn-night`:
- DECISION JOURNAL — the night's `watchlist`, `reanchor`, `would_trade`,
  `no_trade`, and any `skip` rows, each with signal provenance.
- PERFORMANCE — stays empty until `live: true`; would-be trades are in the
  journal (that's the point of note-mode).
- SOURCE — the exact module the engine ran.
- SIGNALS panel — raw `estimate` / `news` / `analysis` rows.

Then decide: flip `live` in PARAMS (per-instance) when a night of
would-be trades looks right; shorts additionally need the engine-wide
`equity_long_only` gate flipped.

## Known pre-existing wart (not from this phase)

The account carries a zombie plan: NVDA 2026-07-31 195C ×33, status
`exiting` since expiry week (DB-split fallout) — it trips the "1 OPEN
POSITION UNMONITORED" banner. The account also holds 900 untracked NVDA
shares (~$195k paper) from those calls being exercised at expiry. Neither
interferes with `earn-night` (different strategy label; note-mode places
nothing), but the plan row should eventually be retired and the shares
consciously kept or flattened.
