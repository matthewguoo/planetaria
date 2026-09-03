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

## Amendment 1 — 2026-08-10, before any live decision

`max_sets` 10 → **5** on fly-1. Reason, from the shield study
(`research/0dte-vrp/notes/fly_shield_20260810_0324.md`): the two tested
overlays both REDUCE the edge (skipping FOMC days discards the best days
— +13.0bp/day, Sharpe 5.6 on the 21 in-sample statement days; a 15:30
checkpoint stop sells whipsaw lows for a 2bp worst-day improvement), so
sizing is the only real tail control. At 5 sets the measured worst day
(−68bp x 5 ≈ −$1,900) sits just inside the $2,000 breaker instead of
blowing through it at 2x. The instance remains note-mode; no live
decision has been made under either value.

## Amendment 2 — 2026-08-10, before any live decision

`min_credit_frac` 0.25 -> **0.10**. The 25% floor was stricter than
anything the measured strategy filtered — the study's +3.2bp/day includes
thin-credit days — so the live config was silently a different strategy.
Exhibit A: 2026-08-10 priced 21% credit/width, the guard skipped it, and
the structure went on to keep 86% of its credit. 0.10 remains as pure
bad-quote protection. The instance is note-mode; no live decision has
been made under either value. Same-day forensic note: current-day OPRA
bars are 403-gated without the OPRA agreement (historical bars and live
quotes both work) — the study-method cross-check of today's marks runs
tomorrow.

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

---

*Correction (2026-09-01): the evidence note cited above,
`flies_0dte_20260810_0214.md`, was superseded the same night by
`research/0dte-vrp/notes/flies_0dte_20260810_0306.md` (an ROC-column fix,
commit 845565b); the cited 14:00 / 1.0%-width numbers are unchanged and
appear verbatim in the successor note. The registered hypothesis and
thresholds above are untouched.*
