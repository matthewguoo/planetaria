# Runbook — first no-SIP earnings night (`nosip-1`)

The instance is enabled and spawns on every boot; the engine just has to be
RUNNING through 15:00–20:00 ET. Pre-registration: `pre-registration-nosip.md`.

## Before 15:25 ET

```powershell
cd C:\Users\matth\Desktop\planetaria\backend
Start-Process -FilePath .venv\Scripts\python.exe -ArgumentList "-m","uvicorn","app.main:app","--port","8000" -RedirectStandardError "$env:LOCALAPPDATA\planetaria-logs\engine.err.log" -RedirectStandardOutput "$env:LOCALAPPDATA\planetaria-logs\engine.out.log"
```

Detached, not a terminal child — a closed window must not kill the night.
Then in the ops console (`/`): STRATEGIES → `nosip-1` shows RUNNING;
`/api/health` shows the finnhub key and `paper: true`. KeepAwake holds while
the instance runs — leave the lid alone.

- 15:30 — watchlist freezes itself (journal shows the `watchlist` note with
  tonight's AMC top-5 and dollar volumes). Engine booted late? Send
  `{"cmd": "build_watchlist"}` from the instance's trigger box.
- 16:04 — re-anchor to the official close.

## The night, 16:00–20:00 ET

Releases land (EDGAR + Benzinga), decisions journal after the 10-minute
confirmation. What to look at in DECISION JOURNAL:

- `would_trade` rows **after 17:00 ET** with `quote_src: yahoo|finnhub` —
  each one is an entry the SIP-less engine could never price before.
- `skip: no fresh quote` rows after 17:00 — the external sources were stale
  too; count them, they measure the path's coverage.
- From ~16:20 onward, `fill_check` rows (entry instant + 16 min): the true
  delayed-SIP bid/ask, `sip_spread_bp`, `print_vs_mid_bp`,
  `marketable_at_limit`. This is the AMC fill simulation — the number the
  paper could not measure.

At 17:05 run the go/no-go once (it proves Yahoo/Finnhub are actually fresh
past IEX close *tonight*, not just last week):

```powershell
cd C:\Users\matth\Desktop\planetaria\backend
.venv\Scripts\python.exe scripts\verify_nosip_ah_quotes.py
```

## Costs to know

`LLM_BACKEND=claude-cli` bills the subscription window per analysis —
~28k tokens of CLI context (cache-read after the first call) plus medium
thinking, capped at `n_names`=5 calls a night. A fresh `ANTHROPIC_API_KEY`
in `.env` with `LLM_BACKEND=api` moves that to API billing (~cents/night)
and cuts latency.

## Morning after

`GET /api/strategies/{id}/twin` for the equity curve the decisions imply;
the `fill_check` rows for spread/marketability; the pre-registration's
metric is **drift hit rate on non-neutral verdicts** — log it per night,
judge it at n≥200.
