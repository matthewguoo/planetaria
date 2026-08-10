# Alpha scan against our three edges — 2026-08-10

Request (Matthew, 2026-08-09): extensively research strategies that monetise
compute, better-than-retail execution, and Alpaca automation; backtest the
promising ones; synthesise for Sharpe. Named leads: earnings inefficiency,
the Barclays retail-behaviour thread, chart astrology.

Five studies ran tonight, all off cached or trivially-fetched data. Three
web-literature agents died at a session cap before reporting; the literature
below is folded from model knowledge plus the citations already verified in
`research/notes/overnight_alpha_20260805.md`. Every number in this note has
a script and a dated run log behind it.

| study | note |
|---|---|
| PEAD overnight decomposition | `pead-llm-gate/notes/overnight_decomp_flagship_20260810_0127.md` |
| Mechanical carry, all seasons | `pead-llm-gate/notes/overnight_decomp_mech_20260810_0125.md` |
| Retail tug-of-war deciles | `tug-of-war/notes/tug_of_war_20260810_0130.md` |
| planetaria null battery | `astro-null/notes/astro_null_20260810_0138.md` |
| Intraday momentum (GHLZ) | `intraday-mft/notes/intraday_momentum_20260810_0140.md` |

## 1. The one result that changes a decision

**95% of the flagship's T+1 edge is already earned at the next open.**
On the paper's own panel and sides (the close-exit rows reproduce the
mutation table and the 48.2%/2.15/18.0% flagship exactly, so the harness is
trusted), the T+1 return decomposes into +123.3bp overnight (t 9.30) and
+20.5bp intraday (t 1.35). The T+3 extension's extra value is real but
lives in day-2 RTH (+41.2bp, t 3.31) on guidance-movers.

Consequences, in order:

- **The SIP entitlement is not one blocker among three; it is the product.**
  An entry that waits for the 09:31 tape forfeits ~95% of the one-session
  edge. Phase 15's §2.1 was already right; now it has a number.
- **Exit-clock surgery does not improve the flagship.** Exit-at-open on
  everything: Sharpe 2.08. Hybrid (open on T+1 trades, close on T+3): 2.03,
  because day-1 drift concentrates on exactly the no-guidance-move subset
  (~+54bp there). The paper's conditional close exit stands.
- **`t1_open` is a legitimate defensive dial, not an upgrade**: Sharpe 2.32,
  max drawdown 7.9% vs 18.0%, CAGR 28.1% vs 48.2%. Worth having in
  `pead_flagship` as a config for higher-gross or multi-strategy regimes.
- Nights-only laddering re-buys the drift each evening and loses to simply
  holding (2.19 at 27.2% CAGR): night1 is flat-to-negative once night0 is
  spent. The drift is front-loaded into the FIRST night; there is no
  "overnight harvest" beyond it.

Mechanically (no LLM, all 2016-2026 seasons, acceptance >= 16:30 only —
see §4): the signed carry into the open is +32bp (t 2.2) at a 2% gate and
+194bp (t 2.4) at 10%, and the following RTH day REVERSES (-168bp at 10%).
The night is continuation, the day is mean reversion, and the verdict gate
is what turns the night into money. This replays probe #1 of the overnight
note across a decade instead of one season.

## 2. The Barclays/retail thread: measured, and mostly not a trade

Tug-of-war deciles (top-1000-per-year universe, 2.63M symbol-days,
speculativeness = price x vol x lottery-MAX, monthly, no lookahead):

- The night premium is monotone in retailness: D10 +10.4bp/night (t 3.7)
  vs D1 +3.2; D10-D1 spread +7.2bp/night (t 3.4). The mechanism the
  literature names (retail concentrates its buying at and around the open;
  Lou-Polk-Skouras 2019, Berkman 2012) is visibly alive in the clock.
- **But the intraday leg is not negative in this liquid universe** (D10 day
  +2.0bp, t 0.6), so overnight-only D10 (+26% gross, +11-16% net of two
  daily auction crossings) is strictly dominated by just holding D10, and
  2022 shows the regime tail (-13.5bp/night for a year). The classic
  short-the-day leg needs microcaps we would not trade and shorts we gate.
- Verdict: **no standalone build.** Keep two free overlays the engine can
  respect at zero cost: never BUY a speculative name at the open (its open
  is the local high of the clientele cycle), and treat spec-basket
  overnight exposure as the risk-bearing hours. It also independently
  confirms the §1 anatomy: speculative repricing happens overnight.

## 3. Chart astrology, taken literally and seriously

17 published calendar/astro partitions x two windows on SPY (1993-2026 via
adjusted daily closes; ephemeris from JPL DE421, FOMC dates from the Fed's
own calendars). Pre-registered as a confirmation battery, Bonferroni over
34 tests, every row reported:

- **Nothing clears the bar in either window.** Moon (both published
  conventions), Halloween, Santa, January, Monday/Friday, turn-of-month,
  Etula month-end, pre-FOMC drift (Lucca-Moench: +3.0bp, dead as the NY Fed
  says), CMVJ even-week (wrong sign since 2016): all indistinguishable from
  luck on modern data.
- The best-of-battery is, delightfully, **Mercury retrograde: -14.7bp/day
  (t -2.62, perm p 0.005) on 2016-2026, -5.9 (t -1.73) over 33 years.** A
  best-of-34 at p=0.005 is what a global null hands you; it fails
  Bonferroni; we are not trading Mercury. But for a repo named planetaria,
  the sky choosing THAT row to flirt with is the correct outcome.
- OPEX Friday is the only other consistent-sign row (-12 to -16bp/day in
  both windows, never past the bar). Costless to respect as "don't open new
  spec longs into expiration Friday afternoon"; not evidence.

With ICT already dead (`ict_backtest_20260805.md`), the chart-astrology
ledger now covers both the folk-technical and the literal-celestial ends.

## 4. Two data traps found tonight (institutional knowledge)

1. **The daily panels are Adjustment.ALL; every entry/anchor print is raw.**
   Mixing LEVELS across the two fabricates split factors as returns (first
   run: -66,000bp "means" from reverse-splits). Rule: adjusted prices enter
   only as same-session or adjacent-session RATIOS chained onto raw prints.
2. **`anchor` is post-release tape for early acceptances.** The PR crosses
   the wire before the 8-K: ROKU 2022-11-02 closed 54.32, anchor (last
   print <= 16:05) is 42.00 — already crashed. For acceptances before
   ~16:30, `move_pct` measures 16:05->16:20 tape drift, not
   reaction-vs-close, and its SIGN can differ from the true reaction. The
   paper is internally consistent (it trades and prices the same
   convention), but any NEW study keying "reaction" off `anchor` for early
   acceptances inherits this. The mech study restricts to >= 16:30.

## 5. Intraday / MFT-ish, per the follow-up ask

At our latency the viable band is minutes-to-hours, and the engine already
occupies its best corner (minute-latency event reaction). Tested tonight:
**GHLZ (JFE 2018) first->last half-hour momentum is dead post-publication**
— the sign trade is gross-NEGATIVE on SPY 2016-2026 (-0.3 to -0.6bp/trade
before costs, wrong sign 2021-2026 on SPY and QQQ, big-move conditioning
included). Time-of-day patterns on index products are not where our edge
is.

## 6. Ranked queue (what to build / verify next)

1. **Run the flagship's forward test** (unchanged, `handoff_20260807`):
   drift hit rate on post-2026-05 events is still the only number that
   settles reading-vs-recall. Tonight adds: prioritise the SIP entitlement
   above all engine work — §1 prices what the after-hours entry is worth.
2. **BMO premarket twin** (overnight note §2.1): pre-open announcers react
   36% weaker and drift days; our LLM latency is the documented counter;
   premarket books are the tightest off-hours window. Blocked on real-time
   premarket data before 08:00 (ATP or paid tier) — same billing decision
   as #1.
3. **0DTE defined-risk ingredient study** (the MFT thread that fits both
   our options infrastructure and Matthew's 0-3 DTE court): Alpaca has
   historical options bars from 2024-02. Step one is NOT a structure sweep;
   it is measuring the raw premium: SPY/QQQ ATM straddle at 10:00 vs expiry
   intrinsic, daily, ~600 sessions — is the intraday variance premium still
   positive post-2023, and what are its tails? One script, one note, then
   decide.
4. **Evening 8-K follow-on drift** (Chan-Marsh, overnight note §2.3): the
   EDGAR poller already sees them; replicate on 2022-2026 daily bars before
   any strategy talk.
5. **`t1_open` config dial** on `pead_flagship` (measured tonight, §1) —
   cheap to add, becomes relevant the day more than one strategy shares the
   account.

Anti-queue (measured dead, do not revisit without new evidence): calendar/
astro overlays; overnight-only retail baskets; GHLZ end-of-day momentum;
ICT constructs; unconditional short-the-retail-open.

---

# Part 2, same night: candles, mean reversion, 0DTE

Matthew clarified "chart astrology" meant the candle canon, and added mean
reversion and the 0DTE study. Three more studies:

| study | note |
|---|---|
| Candlestick patterns | `candle-null/notes/candles_20260810_0154.md` |
| Reversal + gap fade | `mean-reversal/notes/reversal_20260810_0200.md` |
| 0DTE ATM straddle | `0dte-vrp/notes/` (see below) |

## 7. The candle canon, on 2.0M liquid symbol-days

Dead to INVERTED, and the residue is one factor. With the entry a human
could take (next open) and a same-universe baseline: hammer -4.4bp excess
(t -1.9), bullish engulfing -6.3 (t -5.4), three white soldiers -2.8
(t -2.1), three black crows -7.2 signed (t -4.1) — the continuation
patterns point the WRONG way because liquid daily returns mean-revert.
The only positive rows are both haramis (+3.4/+3.7, t ~3.5): "big move,
then a pause -> fade the move," i.e. short-term reversal in a costume, at
half the size of a round trip. Marshall-Young-Rose (2006) replicates on
modern data, with the amusing upgrade that several patterns now carry
statistically solid NEGATIVE information at sub-cost magnitudes.

## 8. Mean reversion, measured as itself

- Daily-turnover reversal (1d and 5d ranks, next-open entries): +0.6 to
  +3.6bp/d gross, t <= 1.3, every net line negative at 6bp costs, weaker
  2021-26. That spread is the market maker's paycheck, not ours.
- Weekly form with honest stats (non-overlapping t, per-trade costs):
  long-losers t 1.5, long-short t 0.2. The fat per-trade number was beta.
- Cooper volume conditioning: gone entirely (+0.9bp both branches).
- **The survivor: the opening gap fade.** Fade the cross-sectional gap
  deciles open->close: L/S +10.5bp/d gross (t 3.7), monotone D1..D10
  (+5.8 .. -4.7), alive in 2021-26 (+9.1, t 2.0). Net +4.5bp/d at 6bp
  all-in, NEGATIVE at 13bp — an execution-bound edge that also needs the
  gated short leg. Breakeven all-in cost ~10.5bp/round trip. Parked with
  numbers; the long-gap-down side alone nets ~0. Corroborates (again)
  that the retail open is rich and the day mean-reverts — same physics as
  the earnings day-1 reversal and the Berkman prohibition.

## 9. 0DTE ATM straddle (ingredient study)

`0dte-vrp/notes/straddle_0dte_20260810_0202.md` — 634/635 sessions
(2024-02..2026-08), OPRA minute bars on the paper keys, strikes and
payoffs in RAW dollars (the basis rule applied forward).

- **SPY is thin**: implied 0.51% vs delivered 0.48%; seller +3.2bp of
  S/day gross (t 1.65), and the worst day (-597bp) eats ~6 months of
  average P&L. Not a strategy on its own.
- **QQQ still pays**: implied 0.69% vs delivered 0.62%; seller +6.4bp/day
  (t 2.52), 66% win, gross Sharpe 1.59, and REMARKABLY stable by year
  (+6.8 / +5.8 / +6.7 across 2024/2025/2026). Worst day -760bp.
- **The clock matters more than the ticker**: selling the remaining
  straddle at 14:00 (SPY t 2.79, QQQ t 3.62) or 15:30 (t 3.76 / 3.92,
  +2.9 / +3.8bp for THIRTY MINUTES of exposure) is far more reliable per
  unit time — the morning owns the tail, the last 90 minutes own the
  dependable decay.
- Reality adjustments before anyone gets excited: 2.5 years, no 2022-style
  regime in sample; gross of ~0.5-1bp spread friction; naked short calls
  are broker-refused and short puts are cash-secured (~$70k/set), so the
  implementable form is DEFINED-RISK (condor/fly) whose edge is this
  ingredient minus wing costs. The next study is the structure sweep:
  wings at fixed deltas, entries at 14:00/15:30, QQQ first — and its base
  rate is now measured instead of imagined.

## Revised queue after part 2

The part-1 queue stands, with one addition at the top of the options
track: **the 0DTE afternoon-decay structure sweep on QQQ** (ingredient
confirmed, t 3.6-3.9 on the afternoon entries, defined-risk only). The
gap fade joins the "parked, execution-bound" shelf (worth revisiting the
day all-in auction costs are measured under ~8bp and shorts are on).
Candles join the anti-queue as measured — with the note that their
inversions are just the reversal factor, already covered above.
