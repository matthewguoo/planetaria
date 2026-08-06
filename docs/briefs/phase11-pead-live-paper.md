# Phase 11 brief: run the PEAD reaction strategy live on paper

Paste this file as the opening prompt of a fresh Claude Code session in
`C:\Users\matth\Desktop\planetaria`. Goal: the earnings reaction strategy
running LIVE on the paper account with real-time SIP data, accumulating
fills, shadow journals, and execution-quality evidence toward a real-money
go/no-go. Ask Matthew only for money/scope decisions.

## Where the strategy lives (this is the product)

- `backend/app/strategies/earnings_reaction.py` — kind `earnings_reaction`.
  LLM release-parse (claude-fable-5 via CLI subscription auth,
  `LLM_BACKEND=claude-cli` in .env) + tape-agreement gate + stop-risk
  sizing + vol-scaled brackets + 10-min confirmation delay + T+1 15:55 ET
  time stop. Manual cmds: `{"cmd":"build_watchlist"}` (late-boot salvage),
  `{symbol,text[,px0]}` dry-run.
- Instance `earn-night` EXISTS in the DB (SQLite fallback `backend/trader.db`
  — Postgres containers are typically down), state enabled, `live: true`,
  account `planetaria1` ($100k paper, key ...TMPL in .env as
  ALPACA_ACCOUNT_PLANETARIA1_*).
- Engine runbook: `docs/notes/runbook-first-earnings-night.md`. Engine must
  run DETACHED (PowerShell Start-Process, logs at
  %LOCALAPPDATA%\planetaria-logs\) — background-Bash processes die with
  the app session (verified the hard way 8/5).

## Evidence base (all in docs/notes/, scripts in backend/scripts/)

| test | script | verdict |
|---|---|---|
| News latency (8/4 real night) | verify_news_latency.py | neither Benzinga nor EDGAR dominates; tape reprices in release minute |
| ICT iFVG/PO3 | research_ict_backtest.py | dead — worse than random |
| Earnings lead-up | research_leadup_backtest.py + _account_sim.py | parked — beta in costume; 200dma gate maps regimes |
| PEAD mechanical | research_pead_backtest.py | gate-5% core: +50/+162/+72bp across 2022/2023/2025-26; longs universal; shorts need crushed-in guard (run5d<=-5 bucket was -922bp in 2022) |
| LLM layer A/B | research_llm_ab.py | OOS Feb-Jul 2026 full universe n=189: mech -36.5bp -> fable-gated +179bp, vetoed -275bp (+444 spread). In-corpus ~ OOS (no memory signature) |

Caches in `backend/scripts/_leadup_cache/` (gitignored): EDGAR calendars
per window, OHLC panels, PEAD event panels, LLM verdicts
(`llm_ab/*.jsonl`). Finnhub free = NO calendar history beyond ~1mo; EDGAR
submissions JSON is the historical calendar (acceptanceDateTime is ET
mislabeled Z). Free SIP: historical fine, recent/real-time blocked; wide
windows silently drop earliest dates near caps.

## Config changes owed BEFORE next live window (morning, with tests)

1. `min_move_pct` default/instance 1.0 -> 5.0 (gate curve replicated 3 regimes).
2. Crushed-in short guard: if side<0 and watch run5d <= -5 -> journal a
   veto note instead of submitting (the 2022 -922bp bucket).
3. Create `earn-shadow` instance (same params, live:false) — nightly
   counterfactual journal.
4. Rerun 2023 LLM sanity arm — it selected 0 events (cache-join bug in
   research_llm_ab.py WINDOWS bars_tag matching; fix tag or pass file).

## The $99 decision + SIP flip procedure (Matthew's button)

Backtests priced AH events from FREE historical SIP; LIVE decisions need
real-time SIP ($99/mo Algo Trader Plus, app.alpaca.markets -> Market Data
Subscriptions). Until then every AH entry correctly dies at the 120s
freshness gate (verified live 8/5: free IEX quotes are dead after 16:00;
APP showed $417 stale vs $302 real). After Matthew subscribes:
1. Verify entitlement empirically: latest-quote feed=sip must return fresh.
2. Flip stock_feed=sip (SYSTEM menu or PUT /api/settings/feed) — restart
   required (streams built at boot).
3. Restart detached engine before 15:00 ET; watchlist runs itself at 15:30.
Paper fills are SIMULATED (optimistic) — the exec-quality ledger
(exec_quality on plans) is the number to watch; real go-live gate needs
Matthew's explicit sign-off + caps tightened (per-name 20%/gross 100% are
paper-aggressive) + a standalone alerter (chat monitors die with sessions).

## Standing rules (unchanged)

Commit locally per step, NEVER push. Paper-lock stays. Keys only in .env
(both Alpaca paper key pairs leaked into a transcript 8/5 — suggest
rotation, again). Broker/feed behavior verified empirically via
scripts/verify_*.py before code depends on it, dated findings in comments.
328->380+ backend tests and 139 frontend tests stay green; ruff clean.
Research stays in scripts/research_* + docs/notes — never in app/.
