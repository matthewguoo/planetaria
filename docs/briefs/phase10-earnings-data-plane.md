# Phase 10 brief: earnings data plane (feeds) + latency proof

Paste this whole file as the opening prompt of a fresh Claude Code session in
`C:\Users\matth\Desktop\planetaria`. It is self-contained: orientation,
conventions, the task, and acceptance criteria. Ask Matthew only when a
decision is genuinely his (spending money, changing scope).

## What this repo is

planetaria: a paper-locked Alpaca trading engine (FastAPI backend,
`backend/`) with a React terminal UI (`frontend/`) and, since Phases 2-9, an
autonomous **strategy runtime**: an in-process event bus feeds journaled
signals to strategy instances, which emit trade intents through a four-lock
gate (stale-event guard, dedupe, per-strategy budget, global risk) into the
same `place_trade` path human clicks use — inheriting the FSM, exit
enforcer (software bracket), and execution-quality ledger. Strategies can
call `ctx.analyze()` for schema-constrained, injection-hardened, journaled
LLM analysis (`backend/app/services/llm.py`; needs `ANTHROPIC_API_KEY` in
the root `.env`).

The goal this phase serves: an **earnings reaction-continuation strategy**
(parse each after-close earnings release with the LLM; trade shares long or
short only when the verdict's direction agrees with the tape's reaction;
exit T+1/T+2 by time stop). Everything exists EXCEPT the data plane that
tells the strategy who reports tonight, what consensus is, and hands it the
release text fast. That data plane is this phase.

## Orientation (read these first)

- `backend/app/services/signals/` — event bus (`events.py`, typed Events,
  lossless queues), journal (`store.py`, journal-before-publish rule),
  feeds (`feeds.py`: `TimerFeed`, `AlpacaNewsFeed` — copy their shape).
- `backend/app/services/strategy_runner.py` — how instances consume events.
- `backend/app/strategies/ref_tick.py`, `llm_probe.py` — reference
  strategies; `base.py` is the contract.
- `backend/app/bootstrap.py` — composition root; feeds are wired ~line 126.
- `backend/scripts/verify_equity_paths.py`, `verify_short_paths.py` — the
  house pattern for proving broker/feed behavior EMPIRICALLY before
  building on it.
- Memory file `planetaria-project.md` (auto-loaded) — verified broker facts.

Run: `uvicorn app.main:app --port 8000` from `backend/` (`.venv`,
Python 3.13). Tests: `python -m pytest -q` (328 must stay green) and
`npm test` in `frontend/` (139). Lint: `ruff check app tests scripts`.

## Standing rules (non-negotiable)

- Commit locally per phase with explanatory messages; **NEVER push**.
- Paper-only is hard-locked; don't touch that.
- Broker/feed behavior gets verified against the real paper API by a
  `scripts/verify_*.py` before code depends on it; findings go in code
  comments with dates.
- API keys live in the gitignored root `.env` only — never in code, chat,
  or commits. If a new key is needed, name the env var, tell Matthew what
  to paste where, and read it via `app/config.py` Settings.
- New services get supervised loops (`supervision.py`) and chaos-style
  tests (see `test_strategy_chaos.py`, `test_signals_plane.py`).

## The task

### 1. Earnings calendar + consensus feed (`EarningsCalendarFeed`)

Source decision (researched 2026-08-04): start with **Finnhub free tier**
(60 calls/min; earnings calendar with EPS estimates) — env var
`FINNHUB_API_KEY`. If revenue consensus quality is insufficient, propose
**FMP** (paid, better estimates) to Matthew before spending. Do NOT scrape.

Behavior: once daily ~15:00 ET (and on boot), fetch today's + tomorrow's
calendar; journal one `estimate` signal per reporter: payload
`{symbol, when: bmo|amc, eps_consensus, revenue_consensus?, fiscal_period}`,
key `cal:{symbol}:{date}` (the journal's (source,key) dedupe makes re-polls
idempotent). Publish on the bus so strategies can build watchlists.

### 2. EDGAR 8-K feed (`EdgarFeed`)

Free, authoritative, full text. Poll
`https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent` (or the RSS
variant) for 8-K filings during 06:00-09:30 and 16:00-20:00 ET windows;
politeness rules: <=10 req/s hard limit — use ~1 poll per 2-3s in-window,
send a real `User-Agent` with contact email, back off on 429/503. For
8-Ks with Item 2.02 (results of operations), fetch the press-release
exhibit text (strip HTML), journal as `news` signal with
`source="edgar-8k"`, key = accession number, payload
`{headline, item_codes, text: <=20k chars, url}`. Measure and log
acceptance->journal latency.

### 3. Latency proof on a live earnings night (the deliverable that gates
the strategy)

`scripts/verify_news_latency.py`: run 15:55-17:30 ET on an earnings day.
For each watched reporter (pull 5-10 from the calendar feed), record when
each source first mentions it: Alpaca/Benzinga websocket (already wired),
EDGAR poll, and (reference) the first overnight-tape print outside the
prior close's range. Output a table: symbol | wire ts | benzinga ts |
edgar ts | first-move ts. This tells us whether the free stack is fast
enough (target: text in hand < 60s after the release) or whether to
propose the paid Benzinga-via-Massive upgrade.

### 4. Only after 1-3 land: `earnings_reaction` strategy skeleton

One instance, many tickers: on the 15:30 ET timer tick, build tonight's
watchlist from `estimate` signals (params: `n_names`, `min_price`,
`notional_per_name`); subscribe their quotes; on each `news`/`edgar-8k`
event for a watched name, `ctx.analyze()` with a surprise schema
(eps_vs_consensus, revenue_vs_consensus, guidance_direction, quality
flags), compare with the tape move since prior close, and journal the
would-be intent as a NOTE (paper-observation mode) — actual order
submission stays behind a `live: false` param until Matthew reviews a
night of journaled would-be trades. Shorts: the engine is short-ready
(Phase 9) but `equity_long_only` stays true until he flips it.

## Acceptance

- Both feeds run supervised, journal correctly (dedupe on re-poll), and
  survive the chaos tests you write for them (feed down, malformed
  payload, replay).
- 328+ backend tests green, ruff clean, frontend untouched or green.
- `verify_news_latency.py` has produced one real earnings-night table,
  committed as a docstring/comment or `docs/` note with the date.
- One commit per coherent step, phase-numbered like the git log shows.
- Report at the end: what was measured, what it implies for the free vs
  paid feed decision, and what remains before the strategy can go from
  note-mode to order-mode.
