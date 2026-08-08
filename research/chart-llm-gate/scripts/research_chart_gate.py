"""Open weights on one 3090, reading anonymised charts.

SIX ARMS, AND EACH EXISTS TO KILL A SPECIFIC ALTERNATIVE EXPLANATION.

  gate       A mechanical screen flags a setup, the model sees the anonymised
             chart and calls it bullish or bearish, and the gate KEEPS the
             trade when the call agrees with the flagged side. The statistic
             is the spread between kept and vetoed trades — the same
             statistic the PEAD study's gate reports, so the two are directly
             comparable. The call is FORCED; see the schema comment below for
             the 48-verdict experiment that settled that.

             THIS ARM TURNED OUT NOT TO DISCRIMINATE. Measured keep rate 97%,
             and 100% on three of five strategies. Kept and reported, because
             read against `shuffled` the agreement rate is itself the result.

  outcome    The repair, and the arm the gate conclusion actually rests on.
             Same charts, same brackets, but the question is which barrier
             price reaches FIRST. The setup's direction does not telegraph
             that, so the answer can vary. See its schema comment.

  gateopt    Like `gate`, with `neutral` available. Run at low volume purely
             to put the abstention RATE on the record.

  shuffled   Identical in every respect except that the chart shown is some
             OTHER setup's chart, drawn at random. The verdict is then scored
             against THIS setup's outcome. If `gate` shows a spread and
             `shuffled` shows the same spread, the spread is not coming from
             the chart — it is coming from the pipeline, from the sampling,
             or from a leak, and the whole result is void. This is the
             counterfactual the PEAD study added late (research_perturbation.py)
             and it is cheap enough to run from the start here.

  coldread   No setup, no proposal: a random window of tape and a FORCED call
             on the next 12 bars. This is the analogue of the PEAD study's
             `notextf` probe and it is here for the same reason — a model
             that abstains its way out of an opinion produces an unreadable
             number, so the abstention is removed. A model with nothing to
             say scores 50%. This arm, not the gate, is the direct test of
             "is reading a chart worth anything at all".

  synthetic  A geometric Brownian motion path rendered through the identical
             formatter. There is provably nothing to find. Its purpose is not
             its accuracy — that is 50% by construction — but its CONVICTION
             DISTRIBUTION: if the model is as confident on noise as it is on
             real tape, then its confidence on real tape is not tracking
             anything real, and that holds regardless of what the outcome
             data says.

WHY TWO MODELS. Same logic as `research/pead-llm-gate/scripts/research_local_gate.py`:
a null result from one small model is unreadable, because "chart reading is
astrology" and "a 14B cannot parse a candle table" predict the same number.
phi-4 (14B dense, Q8_0) carries the volume; Qwen3-30B-A3B (MoE, 4-bit)
re-scores a subset as a CAPABILITY CONTROL. If both find nothing, the
instrument is not obviously broken. If qwen3 finds something phi-4 does not,
the phi-4 null was a capability floor and says nothing about chart reading.

Note what is NOT needed here and was the whole second half of the PEAD study:
a contamination control. See `research_chart_render.py` — there is no
outcome-paired text for these windows anywhere, so there is nothing to recall.

Run:
    python scripts/research_chart_gate.py serve --model phi4      # one shell
    python scripts/research_chart_gate.py sample                  # build panels
    python scripts/research_chart_gate.py run --model phi4 --arm gate
    python scripts/research_chart_gate.py run --model phi4 --arm shuffled
    python scripts/research_chart_gate.py run --model phi4 --arm coldread
    python scripts/research_chart_gate.py run --model phi4 --arm synthetic
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from _paths import BACKEND, CACHE, VERDICTS

sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_chart_data import UNIVERSE, bars_path  # noqa: E402
from research_chart_render import (  # noqa: E402
    WINDOW,
    render_coldread,
    render_gate,
    snapshot,
    synthetic_frame,
)
from research_chart_setups import SETUPS, TF, prep  # noqa: E402

# ------------------------------------------------------------------ serving
#
# Config transplanted from research_local_gate.py, where the VRAM arithmetic
# was worked out against a 24GB 3090 with ~1.7GB held by the desktop. The one
# change: prompts here are ~1.2k tokens rather than ~5k (a 48-row candle table
# is far smaller than a 12k-char earnings release), so the same context budget
# buys many more slots and throughput is several times higher.

LLAMA = os.environ.get("LLAMA_BIN", r"C:\Users\matth\llamacpp\llama-server.exe")
GGUF_DIR = os.environ.get("GGUF_DIR", r"C:\Users\matth\models")
ENDPOINT = os.environ.get("LOCAL_LLM", "http://127.0.0.1:8080")

LOCAL: dict[str, dict] = {
    # ctx is slots x 3072 in both cases. The first cut used 2048/slot on an
    # estimate of ~1.2k-token prompts; the smoke test measured a MEDIAN of
    # 1,951 prompt tokens, which leaves 97 tokens of headroom for a 59-token
    # answer. That is not a margin, it is a coin flip on the long tail of
    # charts. 3072/slot is measured-plus-50%.
    "phi4": {
        "gguf": "phi-4.Q8_0.gguf",
        # 15.6GB weights; 40 layers x 10 KV heads x 128 head_dim f16 =
        # 200KB/token, so 24576 tokens is 4.9GB and the total is ~20.5GB
        # inside the 22.5GB working ceiling (24GB card, ~1.7GB to the desktop).
        "args": ["--ctx-size", "24576", "--parallel", "8"],
        "slots": 8,
    },
    "qwen3": {
        "gguf": "qwen3-30b-a3b.UD-Q4_K_XL.gguf",
        # 17.7GB weights but a much cheaper cache — 4 KV heads is 98KB/token,
        # so 18432 tokens is only 1.8GB and the total is ~19.5GB. MoE with 3B
        # active, so decode stays fast despite the larger footprint.
        "args": ["--ctx-size", "18432", "--parallel", "6"],
        "slots": 6,
    },
}

# The KV cache is f16 deliberately. Quantising it to q8_0 was tried during the
# PEAD run and llama-server b10319 dies at model load with a bare
# `ggml-impl.h:318: fatal error` on this build, with or without -fa. Do not
# re-add -ctk/-ctv without re-testing the load.


def serve_cmd(tag: str) -> list[str]:
    cfg = LOCAL[tag]
    return [
        LLAMA,
        "--model", os.path.join(GGUF_DIR, cfg["gguf"]),
        "--n-gpu-layers", "999",
        "--flash-attn", "on",
        "--jinja",
        "--host", "127.0.0.1",
        "--port", ENDPOINT.rsplit(":", 1)[-1],
        "--threads", "8",
        *cfg["args"],
    ]


def stage_serve(args) -> None:
    cmd = serve_cmd(args.model)
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=False)


def wait_healthy(timeout: float = 900) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{ENDPOINT}/health", timeout=5).status_code == 200:
                print(f"server healthy after {time.time() - t0:.0f}s")
                return
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(3)
    raise SystemExit(f"no healthy server at {ENDPOINT} after {timeout:.0f}s")


# ------------------------------------------------------------------ prompts

SYSTEM = (
    "You are a technical analyst reading intraday price action. You are shown "
    "a table of candles and nothing else — no ticker, no date, no news, no "
    "fundamentals. Judge only what the price and volume in front of you "
    "support. Be decisive when the chart is clear and say so when it is not. "
    "Keep `reason` under 40 words and ground it in specific bars."
)

# TWO GATE SCHEMAS, AND THE FORCED ONE IS THE PRIMARY. This was settled
# empirically on 2026-08-07 and it cost 48 wasted verdicts to learn, which is
# cheap for the lesson: phi-4 given a `neutral` option returned
# neutral/low on 48 of 48 charts. Every one of them. The gate vetoes every
# neutral, so an optional-abstention gate on this model keeps nothing, spreads
# nothing, and measures nothing.
#
# That is not a surprise in this repo — `research_local_gate.py` records the
# same model answering `neutral` on ~83% of earnings releases against Opus's
# 16%, and answering "no recall" on 489 of 489 memory probes. Phi-4 is heavily
# hedge-tuned. An arm with an escape hatch measures its willingness to commit,
# not its ability to read.
#
# So `gate` FORCES a side, exactly as the PEAD study's `forced` and `notextf`
# arms do. A model with no signal then splits ~50/50 and the gate keeps ~50%
# of trades at random, which is a well-defined null the spread can be measured
# against. `gateopt` keeps the abstention and is run at low volume purely so
# the abstention RATE is on the record.
GATE_SCHEMA = {
    "type": "object",
    "properties": {
        # ABSOLUTE, not relative to the flagged side. Asking for
        # support/oppose would fold the model's directional read into the
        # screen's proposal and make it impossible to score the read on its
        # own — and it would let a model that always says "support" look
        # identical to one that always says "bullish".
        "direction": {"type": "string", "enum": ["bullish", "bearish"]},
        "conviction": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["direction", "conviction", "reason"],
    "additionalProperties": False,
}

GATEOPT_SCHEMA = {
    **GATE_SCHEMA,
    "properties": {
        **GATE_SCHEMA["properties"],
        "direction": {"type": "string",
                      "enum": ["bullish", "bearish", "neutral"]},
    },
}

GATE_FORCED_NOTE = (
    " This call is FORCED: answer bullish or bearish. 'No opinion' is not "
    "available. If the chart genuinely tells you nothing, set conviction to "
    "'low', say so in `reason`, and pick a side anyway — that is the correct "
    "behaviour there and it is what the analysis expects."
)

# ---------------------------------------------------------------- the `outcome` arm
#
# WHY THIS ARM WAS ADDED MID-RUN, on 2026-08-07, after 352 gate verdicts.
# The `gate` arm asks for a directional read and keeps the trade when the read
# agrees with the flagged side. Measured keep rate: 97%, and 100% on three of
# the five strategies. That is not a gate. It is a yes-man.
#
# The cause is structural rather than a bug: every one of these setups is a
# momentum or breakout pattern, so the chart the model is shown VISIBLY
# CONTAINS the thing the detector fired on. Asked whether a chart that just
# broke out looks bullish, a model says bullish. The verdict is nearly a
# deterministic function of the setup's own direction, and a gate with 10
# vetoes out of 352 has no statistical content whatever the model can do.
#
# (The 97% figure is kept and reported. Read against the `shuffled` arm it is
# a strong result on its own: if the model agrees with the proposal just as
# often when shown a DIFFERENT chart, its verdicts are not a function of the
# chart at all.)
#
# So this arm asks the question that actually decides the money, and which the
# setup direction does NOT telegraph: given this chart and this bracket, does
# price reach the target or the stop first? A model with no skill splits
# somewhere near the base rate; a model with skill separates the trades that
# resolved at the target from the ones that resolved at the stop. The gate
# keeps a trade when the answer is `target`.
OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "resolves": {"type": "string", "enum": ["target", "stop"]},
        "conviction": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["resolves", "conviction", "reason"],
    "additionalProperties": False,
}

OUTCOME_SYSTEM = (
    SYSTEM + " You are judging a bracketed trade that has already been "
    "chosen. Do NOT re-litigate the direction; the position is being taken "
    "either way. The only question is which barrier price reaches first. This "
    "call is FORCED: answer 'target' or 'stop'. If the chart tells you "
    "nothing, set conviction to 'low', say so in `reason`, and pick one "
    "anyway."
)


def render_outcome(row: pd.Series, df: pd.DataFrame) -> str:
    """The gate task, re-asked as the question the bracket actually poses."""
    base = render_gate(row, df)
    head, _, _ = base.partition("Judge the chart.")
    return head + (
        "Which does price reach FIRST — the target or the stop? Answer only "
        "that. Ignore whether you would have taken the trade."
    )

FORCED_SCHEMA = {
    "type": "object",
    "properties": {
        # No `neutral`. See the module docstring: on an arm where the model
        # may have nothing, an abstention option turns a measurement of skill
        # into a measurement of temperament.
        "direction": {"type": "string", "enum": ["up", "down"]},
        "conviction": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["direction", "conviction", "reason"],
    "additionalProperties": False,
}

COLD_SYSTEM = (
    SYSTEM + " This call is FORCED: answer up or down. 'I do not know' is not "
    "available. If the chart tells you nothing, set conviction to 'low', say "
    "so in `reason`, and pick a side anyway — a coin flip is the correct "
    "behaviour there and it is what the analysis expects."
)

COLD_HORIZON = 12       # bars; 60 minutes at 5m


def payload(task: str, system: str, schema: dict,
            temperature: float) -> dict:
    return {
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": task}],
        # Measured on the PEAD local run: median verdict 211 tokens, longest
        # 327. `reason` is capped at 40 words here, so 768 is ~4x the longest
        # plausible answer and caps the cost of a degenerate generation.
        "max_tokens": 768,
        "temperature": temperature,
        "top_p": 1.0,
        # DRY, not repeat_penalty. Greedy decoding of a free-text field inside
        # a grammar locks into loops — three of the first twelve PEAD requests
        # ran to the token ceiling mid-string. A flat repetition penalty is
        # the wrong tool because it also penalises the structural tokens JSON
        # must repeat; DRY penalises repeated SEQUENCES past allowed_length,
        # which is exactly the failure and nothing else.
        "dry_multiplier": 0.8,
        "dry_base": 1.75,
        "dry_allowed_length": 4,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "verdict", "strict": True,
                                            "schema": schema}},
    }


def score_one(client: httpx.Client, item: dict) -> dict | None:
    """One verdict, or None after three attempts.

    Retries escalate temperature rather than repeating: the primary decode is
    greedy and therefore deterministic, so re-sending an identical payload
    after a parse failure reproduces the identical failure. Only salvaged
    rows carry sampling noise, and the row records which those were.
    """
    base = payload(item["task"], item["system"], item["schema"], item["temp"])
    for attempt in range(3):
        p = dict(base)
        if attempt and not item["temp"]:
            p["temperature"] = 0.7
            p["seed"] = 20260807 + attempt
        try:
            r = client.post(f"{ENDPOINT}/v1/chat/completions", json=p,
                            timeout=600)
            r.raise_for_status()
            doc = r.json()
            v = json.loads(doc["choices"][0]["message"]["content"])
            u = doc.get("usage") or {}
            return {"id": item["id"], **v, "in": u.get("prompt_tokens"),
                    "out": u.get("completion_tokens"),
                    "salvaged": bool(attempt)}
        except Exception as exc:                             # noqa: BLE001
            if attempt == 2:
                print(f"  DROP {item['id']}: {type(exc).__name__} "
                      f"{str(exc)[:70]}")
                return None
            time.sleep(1)
    return None


# ------------------------------------------------------------------ panels

GATE_PANEL = CACHE / "panel_gate.parquet"
COLD_PANEL = CACHE / "panel_coldread.parquet"


def _stratified(s: pd.DataFrame, per_strategy: int,
                rng: np.random.Generator) -> pd.DataFrame:
    """Even coverage across (strategy, month).

    A plain random sample would let a strategy's LLM arm concentrate in
    whichever months happened to produce the most signals, and the months
    differ in regime. Stratifying costs nothing and removes the confound
    before it can be argued about.
    """
    out = []
    for name, g in s.groupby("strategy"):
        g = g.assign(_m=g["date"].str.slice(0, 7))
        months = sorted(g["_m"].unique())
        per_month = max(1, per_strategy // len(months))
        for _, gm in g.groupby("_m"):
            take = min(per_month, len(gm))
            out.append(gm.sample(take, random_state=int(rng.integers(1e6))))
    return (pd.concat(out).drop(columns="_m")
            .sort_values(["ts", "symbol"]).reset_index(drop=True))


def stage_sample(args) -> None:
    """Build both panels once, so every arm and every model scores exactly the
    same events and the comparisons are paired rather than merely similar."""
    rng = np.random.default_rng(args.seed)
    s = pd.read_parquet(SETUPS)
    # Setups too early in the frame cannot show a full window; excluding them
    # keeps the chart the model sees a constant size across the panel.
    s = s[s["i"] >= WINDOW].reset_index(drop=True)
    panel = _stratified(s, args.per_strategy, rng)
    panel.to_parquet(GATE_PANEL)
    print(f"gate panel: {len(panel):,} setups "
          + " ".join(f"{k}={v}" for k, v in
                     panel['strategy'].value_counts().sort_index().items()))

    # Cold-read panel: random RTH bars with a forward return over COLD_HORIZON
    # bars, computed the same way the gate's exits are — never across a
    # session boundary, because an overnight gap is not something a chart read
    # at 14:00 could have earned.
    rows = []
    n_per = max(1, args.coldread // len(UNIVERSE))
    for sym in UNIVERSE:
        df = prep(pd.read_parquet(bars_path(sym)), TF)
        day = df["day"].to_numpy()
        c = df["c"].to_numpy()
        ok = np.arange(WINDOW, len(df) - COLD_HORIZON - 1)
        ok = ok[day[ok] == day[ok + COLD_HORIZON]]
        pick = rng.choice(ok, size=min(n_per, len(ok)), replace=False)
        for i in sorted(pick):
            rows.append({"symbol": sym, "i": int(i), "ts": df["ts"].iat[i],
                         "date": str(df["day"].iat[i].date()),
                         "fwd_bp": (c[i + COLD_HORIZON] / c[i] - 1) * 1e4})
    cold = pd.DataFrame(rows).sort_values(["ts", "symbol"]).reset_index(drop=True)
    cold["id"] = (cold["symbol"] + "_"
                  + cold["ts"].dt.strftime("%Y%m%dT%H%M"))
    cold.to_parquet(COLD_PANEL)
    print(f"coldread panel: {len(cold):,} windows, "
          f"forward +{COLD_HORIZON} bars, "
          f"{(cold['fwd_bp'] > 0).mean() * 100:.1f}% up")


# --------------------------------------------------------------------- run

class _RunLock:
    """One writer per (arm, model), enforced on disk.

    THE FAILURE THIS PREVENTS actually happened on the PEAD run on 2026-08-07:
    a driver's shell was killed, its python child survived, a second driver
    started, both read the completed-id set at their own startup, and both
    appended to the same jsonl — 2,609 rows for 1,815 events. Nothing
    downstream deduplicated, so 44% of events would have been double-counted
    in every mean and every permutation draw, with no symptom but a row count
    nobody was checking. The dedupe-on-read in `_load` is the belt; this is
    the braces.
    """

    def __init__(self, arm: str, tag: str) -> None:
        self.path = VERDICTS / f".lock_{arm}_{tag}"

    def __enter__(self):
        VERDICTS.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = self.path.read_text(encoding="utf-8").strip()
            raise SystemExit(
                f"another run already holds {self.path.name} ({held}).\n"
                f"If that process is gone, delete the file — but check for "
                f"orphans FIRST:\n"
                f"  Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                f" | ? {{ $_.CommandLine -match 'research_chart_gate' }}"
            ) from None
        os.write(fd, f"pid={os.getpid()} started={_dt.datetime.now():%H:%M:%S}"
                 .encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def results_path(arm: str, tag: str):
    return VERDICTS / f"results_{arm}_{tag}.jsonl"


def _done_ids(arm: str, tag: str) -> set[str]:
    p = results_path(arm, tag)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["id"])
        except Exception:                                    # noqa: BLE001
            continue
    return out


def _frames() -> dict[str, pd.DataFrame]:
    return {s: prep(pd.read_parquet(bars_path(s)), TF) for s in UNIVERSE}


def build_items(arm: str, tag: str, limit: int | None,
                seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    done = _done_ids(arm, tag)
    items: list[dict] = []

    if arm in ("gate", "shuffled", "gateopt", "outcome"):
        panel = pd.read_parquet(GATE_PANEL)
        # ANY limited arm subsamples at random rather than truncating. The
        # panel is ts-sorted, so `head(limit)` would hand a limited arm the
        # earliest months only — one regime, not the year. That matters most
        # for qwen3, which runs `gate` and `outcome` at 2,000 of 6,000 as a
        # capability control: truncation would have compared phi-4 over a year
        # against qwen3 over one quarter and called the difference capability.
        #
        # The seed is fixed, so a resumed run redraws the SAME subset and the
        # already-completed ids simply drop out below.
        if limit and limit < len(panel):
            panel = (panel.sample(limit, random_state=seed)
                     .sort_values("ts").reset_index(drop=True))
        frames = _frames()
        # For `shuffled`, each setup is paired with a DIFFERENT setup's chart.
        # The derangement is drawn once from a seeded generator so the mapping
        # is reproducible and auditable, and a fixed roll guarantees no setup
        # is ever paired with itself.
        order = (np.roll(rng.permutation(len(panel)), 1)
                 if arm == "shuffled" else np.arange(len(panel)))
        for pos, row in panel.iterrows():
            if row["setup_id"] in done:
                continue
            src = panel.iloc[order[pos]]
            df = frames[src["symbol"]]
            # The PROPOSAL always comes from the real setup; only the CHART is
            # swapped. Swapping both would just be a second gate arm.
            shown = row.copy()
            if arm == "shuffled":
                shown = shown.copy()
                shown["i"] = src["i"]
                shown["symbol"] = src["symbol"]
                # Restate the stop/target as the same PERCENTAGES against the
                # donor chart's anchor, so the proposal looks equally
                # plausible and only the price action differs.
                anchor = float(df["c"].iat[int(src["i"])])
                shown["stop"] = anchor * (1 + (row["stop"] / row["entry"] - 1))
                shown["target"] = anchor * (1 + (row["target"] / row["entry"] - 1))
                shown["entry"] = anchor
            if arm == "outcome":
                items.append({
                    "id": row["setup_id"], "system": OUTCOME_SYSTEM,
                    "schema": OUTCOME_SCHEMA, "temp": 0.0,
                    "task": render_outcome(shown, df),
                })
                if limit and len(items) >= limit:
                    break
                continue
            forced = arm != "gateopt"
            items.append({
                "id": row["setup_id"],
                "system": SYSTEM + (GATE_FORCED_NOTE if forced else ""),
                "schema": GATE_SCHEMA if forced else GATEOPT_SCHEMA,
                # Greedy on the reading arms: reproducible, and it removes
                # sampling noise from a between-model comparison. The forced
                # PROBES (coldread, synthetic) are sampled instead — see the
                # note there; the difference is that those arms may have no
                # signal at all, where greedy would collapse onto a constant.
                "temp": 0.0,
                "task": render_gate(shown, df),
            })
            if limit and len(items) >= limit:
                break

    elif arm == "coldread":
        panel = pd.read_parquet(COLD_PANEL)
        frames = _frames()
        for _, row in panel.iterrows():
            if row["id"] in done:
                continue
            items.append({
                "id": row["id"], "system": COLD_SYSTEM,
                "schema": FORCED_SCHEMA,
                # SAMPLED, not greedy. The PEAD run learned this the
                # expensive way: greedy decoding on a probe the model has no
                # signal for collapses onto one constant answer, whose
                # accuracy is merely the base rate of up-moves and cannot
                # distinguish "no skill" from "weak skill".
                "temp": 1.0,
                "task": render_coldread(frames[row["symbol"]], int(row["i"]),
                                        COLD_HORIZON),
            })
            if limit and len(items) >= limit:
                break

    elif arm == "synthetic":
        # Volatility matched to the real universe so the fake charts are not
        # trivially distinguishable by their amplitude. Median absolute 5m
        # return across the panel, in bp.
        frames = _frames()
        vols = [np.median(np.abs(np.diff(np.log(d["c"].to_numpy())))) * 1e4
                for d in frames.values()]
        bar_vol = float(np.median(vols))
        n = limit or 750
        for k in range(n):
            sid = f"synth_{k:05d}"
            if sid in done:
                continue
            f = synthetic_frame(rng, WINDOW, bar_vol,
                                start_min=int(rng.integers(30, 200)))
            snap = snapshot(f, len(f) - 1, WINDOW)
            items.append({
                "id": sid, "system": COLD_SYSTEM, "schema": FORCED_SCHEMA,
                "temp": 1.0,
                "task": "\n".join([
                    "Below is a price chart. Each row is one 5-minute candle. "
                    "All prices are percentages relative to the final bar's "
                    "close, so the final close is exactly 0.00. VOL is that "
                    "bar's volume as a multiple of the window's median "
                    "volume. MIN is minutes since the 09:30 session open; "
                    "SESS is 0 for the current session and -1 for the "
                    "previous one.",
                    "", snap["table"], "",
                    f"Read the chart. Over the next {COLD_HORIZON} bars "
                    f"({COLD_HORIZON * 5} minutes), does price close higher "
                    f"or lower than it is now?",
                ]),
            })
    else:
        raise SystemExit(f"unknown arm {arm}")
    return items


def stage_run(args) -> None:
    with _RunLock(args.arm, args.model):
        _run(args)


def _run(args) -> None:
    wait_healthy()
    items = build_items(args.arm, args.model, args.limit, args.seed)
    if not items:
        print(f"nothing to do: every {args.arm} item for {args.model} is on disk")
        return
    print(f"arm={args.arm} model={args.model}: {len(items):,} requests")

    path = results_path(args.arm, args.model)
    VERDICTS.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    done = failed = 0
    t0 = time.time()
    slots = args.slots or LOCAL[args.model]["slots"]

    with httpx.Client() as client, path.open("a", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=slots) as pool:
            futures = [pool.submit(score_one, client, it) for it in items]
            for fut in as_completed(futures):
                row = fut.result()
                with lock:
                    if row is None:
                        failed += 1
                    else:
                        fh.write(json.dumps({**row, "arm": args.arm,
                                             "model": args.model}) + "\n")
                        fh.flush()
                        done += 1
                    n = done + failed
                    if n % 50 == 0 or n == len(items):
                        rate = n / max(time.time() - t0, 1e-9)
                        print(f"  {n}/{len(items)} ok={done} drop={failed} "
                              f"{rate * 60:.0f}/min "
                              f"eta={(len(items) - n) / max(rate, 1e-9) / 60:.0f}min",
                              flush=True)
    print(f"wrote {done} verdicts to {path.name} ({failed} dropped) in "
          f"{(time.time() - t0) / 60:.1f}min")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="stage", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--model", choices=list(LOCAL), required=True)
    s.set_defaults(fn=stage_serve)

    p = sub.add_parser("sample")
    p.add_argument("--per-strategy", type=int, default=1200)
    p.add_argument("--coldread", type=int, default=1500)
    p.add_argument("--seed", type=int, default=20260807)
    p.set_defaults(fn=stage_sample)

    r = sub.add_parser("run")
    r.add_argument("--model", choices=list(LOCAL), required=True)
    r.add_argument("--arm", choices=["gate", "outcome", "gateopt",
                                     "shuffled", "coldread", "synthetic"],
                   default="gate")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--slots", type=int, default=None)
    r.add_argument("--seed", type=int, default=20260807)
    r.set_defaults(fn=stage_run)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
