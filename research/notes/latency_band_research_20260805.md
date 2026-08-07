# The seconds-to-minute band: what it's worth — 2026-08-05

Question (Matthew): we react faster than a human discretionary trader but far
slower than HFT (~1-30s data-to-order). What can that band systematically
exploit — (a) at all, (b) during RTH in a normal no-event environment with a
volume-negligible small account, (c) given equities are near-free to trade?

Produced from three deep web-research passes (strategy-class evidence with
citations; 0DTE/options dynamics; 2026 data/broker feasibility) plus one
empirical probe on our own 2026-08-04 earnings tape. Evidence tiers below:
[PR] peer-reviewed, [WP] working paper, [EX] exchange/official, [V] vendor/
practitioner folklore, [OURS] measured here.

## 1. The loop we own (measured, not aspirational)

| Leg | Today | Floor (2026, retail) |
|---|---|---|
| Tape/news in | ws ~10-50ms; Benzinga wire-lag UNMEASURED (probe live tonight); EDGAR RSS lags acceptance ~24s median | EDGAR predictive-accession polling recovers ~20s, $0 |
| Decision | rules ~0ms; claude-cli 4-19s api + ~7s spawn | fast-host LLM (Haiku 4.5 TTFT ~0.7s; Groq/Cerebras-class) ~0.5-2s |
| Order out | REST ack, live-measured ~14-30ms; `trade_updates` +150-250ms; `get_order_by_id` ~25ms | same |
| PAPER caveat | paper fills lag 1-5s, extremes 50-260s on limits; no queue model | fill-quality claims require live 1-share probes, never paper |

So: rule-based reaction ~1-3s wire-to-ack is achievable today; LLM-in-loop
~2-4s after a backend swap (vs ~10-26s now). HFT owns <500ms; humans own
>2min. The band T+1s..T+60s is genuinely ours.

## 2. The structural law of the band

At seconds horizons in a normal market, predictability exists (order-flow /
queue imbalance hit rates 60-75% [PR]) but the conditional move is smaller
than the spread. Monetizing it requires maker economics — queue position plus
microsecond cancels. A slow poster gets the toxic fills the fast makers dodge;
a slow taker pays a spread bigger than the predicted move. Latency-arb prizes:
modal race 5-10 MICROseconds, ~0.5bp, top-6 firms win >80% (Aquilina-Budish-
O'Neill QJE 2022 [PR]). ES-SPY arb windows: 7ms by 2011 (Budish QJE 2015).

Consequence: **in normal tape there is no taker signal-alpha in this band.**
The band pays only where (i) a forced repricing is in progress (events — the
price has distance to travel and interpretation determines direction), or
(ii) you are reducing costs on trades you already wanted (execution alpha).
Round-trip taker cost: SPY shares ~0.3bp; 0DTE SPY options 30-300bp of
premium. Equities are where experiments are free; options are where saved
spread is worth 100x more.

## 3. [OURS] Probe: 2026-08-04 AMC continuation after the release minute

Method: for the 10 names in earnings_latency_20260804.md, anchor at the
first >=1% SIP 1m bar (the "release minute"), measure signed continuation
(sign of initial move) from (A0) that bar's close ~ a T+60-90s rule loop,
and (A2) two bars later ~ our T+3min LLM loop. Script in session scratchpad;
close-to-close, no spread costs, thin AH tape — direction-of-evidence only.

```
median signed continuation, 10 names (+ = kept going)
           +1m     +2m     +5m    +10m    +30m    +60m
A0       -1.30   -1.79   -1.56   -2.26   -4.06   -2.76   (n>0: 1-3/10)
A2       +0.01   +0.20   -0.28   -1.72   -1.27   -0.65   (n>0: 2-5/10)
```

Reading: chasing the release-minute move was net NEGATIVE — the median name
overshot and faded (ANET +17.3% print, -6% retrace; ALAB +8% print, -4%;
SPCX faded ~10%). The name that continued hard was AMD — the largest, most
unambiguous print of the night. One night, n=10, but it agrees with the
literature: the *tape* reprices instantly; whether it repriced *correctly*
is an interpretation question. Naive speed chases noise; the fade and the
verdict-gated continuation are the two live hypotheses. It also motivates an
entry rule refinement for earnings_reaction: after-first-retrace beats
at-first-text for the continuation expression.

## 4. Verdict map (all candidates, all three reports merged)

| Candidate | Horizon | Verdict @ our latency | Evidence |
|---|---|---|---|
| Execution-timing entries off underlying micro-moves (options) | 1-30s | **YES** — cuts effective spread to <40% of quoted; 5x for consistent timers | Muravyev-Pearson RFS 2020 [PR] |
| LLM interpretation of complex/qualitative news (small caps, negative news, guidance-vs-headline, 8-Ks) | 15min-next day | **YES** — incorporation horizon is slow; our loop over-serves it; decays with adoption | Lopez-Lira & Tang; "Fast Numbers, Slow Language" 2026; von Beschwitz RAPS 2020 [PR/WP] |
| Scheduled event-vol selling, defined-risk (FOMC/CPI/earnings) | hours-1d | **YES, not a latency play** — event VRP documented | Wright NBER 28306 [WP] |
| FOMC press-conference language trading | minutes | **MARGINAL-YES** — statement-move→presser-move corr 44-58%; discovery takes minutes; 8 days/yr | Gómez-Cram & Grotteria JFE 2022 [PR] |
| Drift-burst fade (no-news air pockets) | ~5min | **MARGINAL** — real: ~1/wk/asset, 25-200bp, 66-78% revert, ~20% mean retrace in 5min; net-of-cost UNPUBLISHED | Christensen-Oomen-Renò J.Econometrics [PR] |
| Close-deviation fade after 4:00 (auction price pressure) | minutes-overnight | **MARGINAL** — half reverts quickly, ~85% by next open; needs AH limit fills (we have 24/5) | Bogousslavsky-Muravyev JFM 2023 [PR] |
| Peer-sympathy selection via LLM ("who is the true comp") | minutes-days | **MARGINAL-YES** — spillover documented; strategy returns not published | RAS 2026; Frankel JAR 2025 [PR] |
| Last-30-min momentum conditioned on gamma regime | 30min-close | **MARGINAL** — documented, latency-insensitive, regime estimate noisy | Baltussen JFE 2021; Barbon-Buraschi [PR/WP] |
| First-30-min options-flow tilt → rest-of-day (single names) | hours | **MARGINAL** — one study, pre-0DTE era; replicate first | Bergsma et al. FM 2020 [PR] |
| Post-spike vol normalization fade (futures-basis-confirmed) | 10min-hours | **MARGINAL** — mechanism documented (Aug-5-24 VIX spike >85% quote artifact); not backtested | BIS Bulletin 95 [EX] |
| Next-open post-earnings options (signal from AH equity move) | minutes-hours | **MARGINAL** — venue-viable after ~9:35; edge is the signal, not speed; first-minute "stale IV" is spread-eaten | agent-2 §3 [V/PR] |
| Halt-reopen directional | minutes | **NO** — no documented edge; spreads 2x, vol 9x at reopen | Hautsch-Horváth JFE 2019 [PR] |
| Headline race on liquid names | <5s | **NO** — priced in ~5s; after-spread ≈ 0 even at 0 delay (2016-20) | Christensen "Warp Speed" JFE 2025 [PR] |
| Macro first print / post-print OHLCV drift | <1s / min | **NO** — 300ms delay already costs; drift falsified on MNQ | Scholtus JBF 2014 [PR]; arXiv 2605.04004 |
| Liquid lead-lag (ES→SPY, BTC→COIN/MSTR beta) | ms-s | **NO** liquid / **unmeasured** stress-conditional residue | Budish QJE 2015 [PR] |
| Options sweep-chasing, minute horizon | min | **NO** — no PR support; 30-40% of flagged flow misclassified | agent-2 §4 |
| Real-time dealer-GEX triggers | min | **NO** — flow balanced (hedge flow 0.04-0.17% of ES liquidity); only 4/43 MMs consistently delta-hedge; exchange truth costs $18-72k/yr | Cboe [EX]; Dim-Eraker-Vilkov; Hu et al. [WP] |
| 0DTE pinning/max-pain daytrades | day | **NO** — diffuse daily OI; documented for monthlies only | [PR monthlies]+[V] |
| Taker staleness capture in options quotes | 1-10s | **NO** — sniped in µs; residual is sub-spread by construction | Nimalendran JFE 2024 [PR] |
| Passive pseudo-market-making (slow maker) | s | **NO** — toxic-fill asymmetry; NOII race into the cross also consumed in-window (Jegadeesh-Wu JFE 2022) | [PR] |
| Crypto cross-venue on Alpaca | s | **NO** — 15-25bp taker fees vs bp-scale dislocations | [EX] |

## 5. The five worth building (ranked)

**5.1 Execution-timing overlay — the compounding certainty.** Muravyev-
Pearson: option fair value follows the underlying at seconds lag *within the
spread*; buying just after favorable underlying micro-moves cuts effective
spread to <40% of quoted (5x for consistent timers). We hold every input:
underlying ws tape, chain NBBO, fair_value service, exec-quality ledger.
Build: stage intents ("want this vertical"), trigger submission on the
favorable side of the last few seconds of underlying movement; mid-peg-then-
walk ladder; never market orders. Applies to every strategy exit AND
Matthew's discretionary clicks (biggest single payer at his volume: 0DTE
round trips cost 30-300bp of premium). Falsification: exec ledger A/B —
timed vs immediate submission — on live 1-lot probes eventually (paper fills
can't score this).

**5.2 The interpretation loop we already run (earnings_reaction) — pointed
at the documented pocket.** The literature's surviving alpha is exactly
"complex content, small caps, negative news, 15min-to-next-day horizons."
Refinements from this research: (a) entry after first retrace, not at first
text (probe §3); (b) verdict-gated continuation vs fade as an explicit A/B
in the journal; (c) universe tilt toward smaller/less-covered reporters
(drift concentrates there — Lopez-Lira & Tang), which our liquidity-ranked
watchlist currently deprioritizes — consider a mid-cap band, not just
top-10 by $vol; (d) LLM fast-path (~1-2s) only if journals show decay
inside minute one — literature says they shouldn't.

**5.3 Drift-burst fade — the RTH normal-day candidate.** New strategy, same
runtime: detect locally-explosive no-news moves (t-stat computable from ws
bars in ~1s), require NO news/edgar signal for symbol in last N min (our
event bus makes news-absence queryable — our unique asset), spread below
cap, fade with marketable limit, hard 5-10min time stop, note-mode first.
Long-only starts with down-bursts; up-burst fades need the equity_long_only
flip. Expect thin margins: ~20% mean retrace of 25-200bp minus 5-20bp moat;
the literature explicitly does not promise net profit — that's what $0
1-share live probes are for. IEX-only limits the scan universe (~30-symbol
ws cap on free tier); ATP widens to full SIP.

**5.4 Close-deviation fade — smallest build, real paper.** Compare 3:59:50
NBBO mid vs 4:00 auction print (SIP); when deviation > threshold, fade in
AH with our 24/5 limit path; exit by next open (~85% reversion documented).
Capacity tiny — perfect for us. First step is free and offline: measure
deviation distribution + reversion on historical closes before any order
logic. No imbalance feed needed for this expression.

**5.5 FOMC presser language loop — 8 scheduled days/yr.** Live ASR + LLM on
the presser (discovery takes minutes; corr 44-58% between statement-window
and presser-window moves). Instruments: SPY shares (no futures on Alpaca);
options spread-hostile mid-presser. Calendar-triggered strategy instance;
low build cost given ctx.analyze exists. Next FOMC is the natural probe.

Adjacent conditioners worth cheap adoption: daily gamma-regime estimate +
last-30-min momentum bias (5.4-adjacent, also useful context for Matthew's
own 3-4pm discretionary window); first-30-min options-flow tilt needs OPRA
(ATP) and a replication pass first.

## 6. Infra decisions (all Matthew's — money)

| Item | $ | Unblocks | Verdict |
|---|---|---|---|
| Alpaca Algo Trader Plus | $99/mo | real-time full SIP (+ `statuses`/`lulds`/`imbalances` halt channels, full-universe burst scan), real-time OPRA (exec-timing overlay needs option NBBO), real-time BOATS overnight (un-gates overnight equity entries that are structurally blocked on 15-min-delayed tape) | the one subscription that matters; three current gaps at once |
| Tiingo overnight | ~$39/mo | overnight tape only | only if skipping ATP |
| IBKR TotalView | ~$15/mo | Nasdaq NOII — IF their API streams it (disputed) | optional $15 experiment, only if auction family graduates beyond §5.4 |
| Massive NYSE imbalances | $49/mo | NYSE auction imbalance ws | same condition |
| LLM fast path (Haiku/Groq, API key) | ~$0-low | 10-26s → 1-2s decisions | only if journals show sub-minute decay |
| Databento OPRA/equities, Cboe Open-Close intraday | $199/mo; $1.5-6k/mo | depth/positioning truth | not justified by any strategy above |

## 7. Probe queue (information value ÷ cost)

1. Tonight, in flight already: `verify_news_latency.py --live` (fills the
   acceptance→seen column) + first earnings_reaction journal night.
2. Free, offline: close-deviation reversion study on historical SIP closes
   (§5.4 step 1). Extend probe §3 to more AMC nights while at it.
3. Free, offline: drift-burst detector replay against cached/historical 1m
   bars; count candidate triggers/day, sign, spread at trigger.
4. Free, live tape: Benzinga wire-lag — PR `published_utc` vs ws arrival,
   n>100 releases (agent-3 found no public measurement; ours would be new).
5. Free: EDGAR predictive-accession polling upgrade to EdgarFeed (~20s).
6. $0 but Matthew's call: equity_long_only flip for up-burst/short legs.
7. $99: ATP, if/when 1-5 justify. Then exec-timing overlay dev vs OPRA NBBO.
8. Live-account 1-share latency/fill probes (someday; paper ≠ live).

## 8. Core citations

Warp Speed (earnings jumps, 5s death): arxiv.org/abs/2601.08962 · Machine
news 0-5s pricing: academic.oup.com/raps/article/10/1/122/5555424 · Fast
Numbers Slow Language: arxiv.org/pdf/2606.29734 · LLM drift: arxiv.org/abs/
2304.07619 · Chrono-consistent LLMs: arxiv.org/abs/2502.21206 · Fed presser:
sciencedirect.com/science/article/abs/pii/S0304405X21005109 · Macro latency:
ssrn.com/abstract=2174901 · MNQ drift falsification: arxiv.org/pdf/
2605.04004 · Drift bursts: arxiv.org/abs/2601.08974 · Stop cascades (Osler):
newyorkfed.org/medialibrary/media/research/staff_reports/sr150.pdf · Latency
arb prize: academic.oup.com/qje/article/137/1/493/6368348 · ES-SPY 7ms:
academic.oup.com/qje/article/130/4/1547/1916146 · Close deviations:
ssrn.com/abstract=3485840 · NOII consumed in-window: sciencedirect.com/
science/article/abs/pii/S0304405X21005092 · Opening-auction retail reversal:
ssrn.com/abstract=5498938 · Halt pauses: sciencedirect.com/science/article/
abs/pii/S0304405X18302356 · Options exec timing: academic.oup.com/rfs/
article-abstract/33/11/4973/5732665 · Options MM sniping costs: sciencedirect
.com/science/article/pii/S0304405X24001235 · 0DTE not destabilizing: ssrn.com
/abstract=4692190 + cboe.com/insights/posts/0-dt-es-decoded · MMs don't
delta-hedge: ssrn.com/abstract=4633451 · Intraday momentum/gamma: academic
web.nd.edu/~zda/intramom.pdf · Retail 0DTE loses: ssrn.com/abstract=4404704
· VIX spike anatomy: bis.org/publ/bisbull95.htm · Event VRP: nber.org/
papers/w28306 · 8-K gap: ssrn.com/abstract=2657877 · Run EDGAR Run: ssrn.com
/abstract=2513350 · EDGAR polling latencies (practitioner): medium.com/
@jgfriedman99/8ddb719e62ba · Alpaca latency (staff): forum.alpaca.markets/t/
order-execution-times/18156 · Full three research reports: session transcript
2026-08-05.
