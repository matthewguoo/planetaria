# planetaria — what it is and how it works

A discipline-enforced terminal for 0–3 DTE options on Alpaca paper. One design
rule generates everything else: **every position carries a server-enforced
exit plan (TP / SL / hard time stop) that survives restarts.** The trader
picks the trade; the machine runs the exits.

## The four planes

**1. Data plane** — one Alpaca stock stream + one option stream per process
(broker hard limit), reference-counted subscriptions, in-process fanout to a
snapshot-then-stream client WebSocket. Bars: server-aggregated 1m (SIP >16min
/ IEX live blend), derived 5m/15m/1h, Redis write-through with in-memory
fallback. Coverage gaps are patched by dedicated loops: premarket delayed-SIP
gap-fill (IEX is silent 04:00–08:00 ET), overnight Blue Ocean trade polling,
and a staleness rule everywhere: **a quote anchors pricing only while it's as
fresh as the tape; otherwise the last bar close wins.** The chart repaints on
every tick (the forming bar follows the live quote) and labels its regime:
PRE / RTH / AH / CLOSED, session-day boundaries, ETH shading behind a toggle.

**2. Pricing plane** — one scenario-pricing function shared by everything:
trading-time Black-Scholes over **entry-implied leg IVs** (always re-solved
from the contract's own mid — feed IVs proved 2–3x off our convention), an
anchored sticky-moneyness smile correction (zero at current spot *by
construction*, so a polluted off-hours fit can't invent P/L), optional IV
shock and chain-derived skew-beta. The on-chart heatmap, TP/SL/BE contours,
hover P/L, exit-drag mapping, payoff profile, and the Monte Carlo simulator
all consume this one function; the expiry column is model-free intrinsic.
Python and TypeScript implementations are held to 1e-9 parity by fixture
tests. The MC (2000 seeded paths) runs the enforcer's EXACT exit rules —
TP-as-limit, SL with gap-through, time stop, spread friction — and is
deliberately edge-free: its EV is the rake you pay if your thesis is worth
nothing.

**3. Execution plane** — the FIX-style plan FSM is the only writer of
lifecycle state (declarative transition table, per-plan locks, atomic
compare-and-set, append-only event journal — dropped events included). The
exit enforcer is the bracket the broker doesn't offer for options: TP rests
AT the broker as a live limit (fills even if the engine is down); SL runs on
a Kalman fair value (microprice observations weighted by quote quality,
theo-drift prediction from underlying ticks between option quotes) with
dwell confirmation for shallow breaches and instant firing for deep or
quality-quoted ones; monitors are quote-OR-poll driven with cadence that
tightens near thresholds. Exits escalate: limit @ mid−2% → mid−6% → market →
REST verify loop. Every order carries a deterministic client id, ambiguous
submits recover instead of double-placing, ghost orders are hunted and
swept, a 45s REST reconcile is the truth-sync behind the stream, and
positions closed OUTSIDE the engine (broker UI, expiry) force-close with
their real fills recovered from order history. All of it is pressure-tested
by a chaos harness (flaky broker: latency, lost events, hung submits that
land late) — 200+ backend tests.

**4. Risk plane** — server-side, never advisory: per-trade max loss vs the
enforced stop, cash-secured/margin-realistic BP (broker-verified numbers),
daily loss circuit breaker, max positions, PDT/overtrading/duplicate guards,
spread-width cap, stale-data entry block, fast-tape staged-price rejection,
naked-call unplaceability surfaced at the UI. Exits may only tighten on the
loss side; TP moves freely behind an explicit confirm. Portfolio view adds
correlation-adjusted account risk and beta-weighted factor exposures.

## Verdict for pure directional betting

The system is genuinely strong for its stated job — disciplined, structured
directional expression with options — with these honest boundaries:

**What's solid:** sizing is stop-based and honest; the surface/contours show
exactly where the enforced exits act, in underlying terms, before entry; the
staged numbers are guarded against fast tape; the exit machinery has desk-
grade noise handling and is the most-tested code in the repo; post-trade
replay (MAE/MFE, exit markers) closes the review loop most retail platforms
never close.

**What's assumption:** scenario prices are model marks (BSM + smile ride),
not executable quotes — friction estimates and the MC's gap-through model
bound but don't eliminate fill reality; touch probabilities and EV live
under risk-neutral drift by design. IV inputs off-hours are solved from
stale mids and are only as good as those books.

**What to watch live:** the enforcer's SL acts on filtered fair value — in a
true gap it fires on the first quality print, which can be well through the
stop (the MC's SL slippage stats and the stop-slippage tracker measure this;
believe them over the line on the chart). Exits enforce only while the
engine runs — keep it supervised whenever a position is on. And the model's
edge-free EV will stay slightly negative forever: the P&L case is the
trader's read, not the terminal's math.
