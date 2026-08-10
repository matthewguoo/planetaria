# Pre-registration — afternoon_fly (QQQ 0DTE iron fly)

Registered 2026-08-10, BEFORE the first live decision of the instance. Per
`docs/briefs/strategy-authoring.md` §2: any number chosen after this
commit is a new hypothesis, not a result. The commit hash of this file is
the reference.

## The evidence this trades

`research/0dte-vrp/notes/flies_0dte_20260810_0214.md` (652 QQQ sessions,
2024-02..2026-08): 14:00 entry, 1.0%-width wings, held to expiry
intrinsic — +3.2bp of underlying per day, t 3.51, 57% win, worst day
−68bp, gross Sharpe 2.20; positive each of 2024/2025/2026 (+4.5/+2.4/
+2.6). The 1.0%-width row clears t 2.8–3.5 at every entry time. Ingredient
study (straddle note, same directory): QQQ implied 0.69% vs delivered
0.62%; afternoon entries t 3.6–3.9.

## The exact configuration under test

`afternoon_fly` defaults as committed with this file:
underlying QQQ · entry 14:00 ET · exit (time stop) 15:50 ET · wings at
1.0% of spot, min $1 · max 10 sets · margin budget 50% of available ·
leg spread cap $0.10 · credit sanity band 25–90% of width · $0.02 credit
given up for marketability. Allocation $10,000 (usd). Circuit breaker
20% of allocation. tp/sl none — time-stop-only, per the measurement.

## Known deviations from the measured cell (accepted a priori)

1. Exit at 15:50 instead of expiry intrinsic — gives up terminal decay
   and pays an exit spread, avoids the broker's expiry-liquidation desk.
2. Real leg friction (~2–5c/structure/side) that minute-bar marks don't
   charge.
3. Best-of-9 selection: the honest prior is the 1.0%-row average
   (+2.5–2.8bp gross), not the best cell's +3.2.

Net expectation registered: +1.5 to +2.5bp of underlying/day, Sharpe
1.3–1.8, win% 53–58. Paper fills flatter reality; the decision journal's
spread field and the exec-quality ledger are the honesty instruments.

## Metric and sample

Per-day P&L in bp of the underlying (journal/twin for note-mode, realized
P&L per plan once live), win%, and worst day. Sample: every session from
the instance's enable date; no discarding. Note-mode ≥ 20 sessions, then
one-set live ≥ 10 sessions, then full sizing — each gate needs the prior
stage consistent with the registered expectation band.

## Stopping rules (written before the first trade)

- Realized worst day beyond −1.5× the measured −68bp per set → halt,
  investigate the wings' fill quality.
- 30-session realized mean below −1bp/day → pause the instance; the
  premium may have left QQQ afternoons the way it left SPY.
- Breaker fires → stays paused until a written post-mortem exists.
- Any structural change (strike granularity, expiry schedule, venue
  behaviour at 14:00) → re-register before resuming.
