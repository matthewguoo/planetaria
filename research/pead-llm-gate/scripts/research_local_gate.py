"""The gate, re-scored by open weights running on one 3090.

THE QUESTION THIS EXISTS FOR. Every contamination control in this study so
far has been run INSIDE the Anthropic family — scrub the identity (`blind`),
remove the text (`notext`), vary the cutoff across four Opus generations,
perturb the document (research_perturbation.py). Those are good tests, but
they share a weakness that no amount of care inside the family removes: the
models share a training corpus lineage. If something about how that corpus
was built puts these events in front of every one of them, a cross-Opus
design cannot see it, because the confound is constant across the arms.

So bring in weights from a different builder entirely, and — this is the
part that matters — pick two that sit at OPPOSITE ends of the axis under
test, so the pair identifies what a single model cannot.

  phi4   Phi-4 14B. ~75% of its 5T tokens are synthetic, filtered-web or
         web-rewrite (tech report, arXiv 2412.08905), pretraining cutoff
         June 2024. The LOW-MEMORISATION pole. Not "clean" — Microsoft never
         claimed that and this module does not assume it. What is claimed is
         a different corpus construction, and therefore a different
         memorisation density, from anything Anthropic ships.

  qwen3  Qwen3-30B-A3B-Instruct-2507. Heavily web-trained, later cutoff,
         same rough weight class, non-thinking instruct variant. The HIGH-
         MEMORISATION pole, and the CAPABILITY CONTROL.

WHY THE PAIR AND NOT JUST PHI. A weak result from phi-4 alone is unreadable.
"A model that cannot recall these events finds no edge" and "a 14B cannot
read an earnings release" predict exactly the same number, and the first is
the finding while the second is a bug in the instrument. Running a model of
similar size that IS heavily web-trained separates them:

    phi4 ~ qwen3 ~ opus     reading does the work; contamination refuted
    phi4 << qwen3 ~ opus    ambiguous, and the notext arm has to adjudicate
    phi4 ~ qwen3 << opus    both local models are too weak; test inconclusive,
                            and says nothing about Opus either way

THE CLEANLINESS OF EITHER MODEL IS MEASURED, NOT ASSUMED — BUT NOT BY THE
ARM YOU WOULD REACH FOR FIRST. The obvious instrument is `notext`: ticker and
date, no release, so any accuracy is recall and nothing else. That is the arm
the study ran on Opus, which claimed recall on 41.4% and was 66.3% right on
the 175 directions it volunteered (z = +4.31).

It does not work on phi-4, and the way it fails is worth stating because it
would have produced a confident wrong answer. Phi-4 returned "no recall" and
"unknown" on all 1,815 events — an apparently perfect cleanliness result. A
no-schema control says otherwise: asked three plain finance questions it
opened with an unprompted disclaimer about election-related matters, refused
Microsoft's FY2023 revenue citing a knowledge cutoff, and then correctly
described Meta's post-Q4-2021 collapse. It has some memory of major market
events AND a strong refusal habit, and a probe with an "unknown" option
cannot separate them. 1,815/1,815 "no" measures willingness to claim recall.

`notextf` is the repair, and it is the arm the cleanliness claim rests on.
Same prompt, no abstention: call up or down. Refusal tuning has nothing to
express itself through, and a model with no recall scores 50%. Two further
details it took a wrong turn to learn:

  - It must run SAMPLED, not greedy. Greedy collapses a no-signal model onto
    one constant answer whose accuracy is merely the base rate of up-moves,
    which cannot distinguish "no recall" from "weak recall".
  - Raw accuracy is still the wrong statistic, because the answer stays badly
    imbalanced even sampled (phi-4 says up 95% of the time). Report the
    ASSOCIATION between answer and outcome instead. Measured on phi-4:
    P(up | said up) 49.2% against P(up | said down) 53.0%, a difference of
    -3.8pp (se 5.6), Fisher p = 0.50. No information, wrong sign, and an
    upper bound around +7pp against the ~27pp Opus shows.

Run the probes before spending a night on the reading arms: they are cheap,
and they decide what the expensive arms are capable of showing.

WHAT IS HELD IDENTICAL. The requests come out of
research_llm_contamination.build_request unchanged, so the system prompt,
the task text, the schema and the 12k-char release body are byte-identical to
what Opus 5 was shown. Verdicts land in the same results_*.jsonl with the
same custom_id grammar, so _load_results, _gate, _spread and the permutation
null all apply with no edit.

WHAT NECESSARILY DIFFERS, stated rather than buried:

  - `effort` and `thinking` have no local equivalent and are dropped. Runs
    are still LABELLED effort="medium" because that is the join key every
    existing analysis filters on; it is a label, not a claim about compute.
  - Decoding is greedy (temperature 0). Opus ran adaptive thinking at
    non-zero temperature. Greedy is the right choice for a local research
    run — it is reproducible and removes sampling noise from a between-model
    comparison — but it is a difference, and it plausibly costs the local
    models a little on genuinely marginal releases.
  - Both models are quantised, phi-4 to Q8_0 (near-lossless) and Qwen3 to a
    4-bit dynamic quant (not). The 3090 has 24GB and this is what fits. The
    asymmetry runs AGAINST the capability control, which is the safe
    direction: it makes qwen3 look worse, not better.
  - Schema-constrained decoding is enforced by llama.cpp's grammar rather
    than by the provider. Same schema, different enforcement mechanism.

Run:
    python scripts/research_local_gate.py serve --model phi4   # in one shell
    python scripts/research_local_gate.py run  --model phi4 --arm notext
    python scripts/research_local_gate.py run  --model phi4 --arm named
    python scripts/research_local_gate.py score
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from _paths import NOTES

import httpx
import numpy as np
import pandas as pd

from research_llm_contamination import (
    COSTS_BP,
    TAG_OF,
    MODELS,
    N_PERM,
    OUT,
    _completed_ids,
    _gate,
    _load_results,
    _permutation_p,
    _requests_for,
    _spread,
    universe_for,
)

# ------------------------------------------------------------------ serving

LLAMA = os.environ.get("LLAMA_BIN", r"C:\Users\matth\llamacpp\llama-server.exe")
GGUF_DIR = os.environ.get("GGUF_DIR", r"C:\Users\matth\models")
ENDPOINT = os.environ.get("LOCAL_LLM", "http://127.0.0.1:8080")

# VRAM budget on a 24GB 3090 with ~1.7GB already held by the desktop, so the
# working ceiling is ~22.5GB. Prompts are ~5k tokens (MAX_TEXT is 12k chars)
# plus up to 4k of output, so a slot needs ~6.6k and anything beyond that is
# waste — hence the odd ctx sizes, which are slots x 6656 exactly.
#
# The KV cache is f16. Quantising it to q8_0 would have bought ~2.6GB and was
# the first thing tried; llama-server b10319 dies at model load with a bare
# `ggml-impl.h:318: fatal error` on this build, with or without -fa. Measured,
# not assumed: phi-4 loads and serves at 21.7GB with f16, so the headroom was
# never needed. Do not re-add -ctk/-ctv here without re-testing the load.
LOCAL: dict[str, dict] = {
    "phi4": {
        "gguf": "phi-4.Q8_0.gguf",
        # 15.6GB of weights. 40 layers, 10 KV heads, head_dim 128 -> 200KB per
        # token of cache, so 26624 tokens is 5.3GB. Measured total 21.7GB.
        "args": ["--ctx-size", "26624", "--parallel", "4"],
        "slots": 4,
    },
    "qwen3": {
        "gguf": "qwen3-30b-a3b.UD-Q4_K_XL.gguf",
        # 17.7GB of weights — 2.1GB more than phi-4, so this one runs three
        # slots, not four. MoE with 3B active, so decode is cheap and losing a
        # slot costs less here than it would on the dense model. 4 KV heads
        # makes the cache cheap too: 98KB/token, 19968 tokens -> 2.0GB.
        "args": ["--ctx-size", "19968", "--parallel", "3"],
        "slots": 3,
    },
}

MODEL_ID = {tag: MODELS[tag][0] for tag in LOCAL}


def serve_cmd(tag: str) -> list[str]:
    cfg = LOCAL[tag]
    return [
        LLAMA,
        "--model", os.path.join(GGUF_DIR, cfg["gguf"]),
        "--n-gpu-layers", "999",
        "--flash-attn", "on",
        "--jinja",                      # use the GGUF's own chat template
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
    """llama-server answers /health with 503 while the model loads. A 17GB
    GGUF off a cold page cache takes minutes, so this is a wait, not a poll."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"{ENDPOINT}/health", timeout=5)
            if r.status_code == 200:
                print(f"server healthy after {time.time() - t0:.0f}s")
                return
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit(f"no healthy server at {ENDPOINT} after {timeout:.0f}s")


# ------------------------------------------------------------- translation

def to_openai(req: dict, temperature: float = 0.0) -> dict:
    """Anthropic batch request -> llama.cpp's OpenAI-compatible shape.

    The prompt is carried across untouched. `thinking` and `effort` are
    dropped because there is nothing local to map them onto — see the module
    docstring; this is the one place the two harnesses are not the same
    request, and it is dropped fields rather than changed ones.
    """
    p = req["params"]
    schema = p["output_config"]["format"]["schema"]
    return {
        "messages": [{"role": "system", "content": p["system"]}, *p["messages"]],
        # Opus was given 4096, but that budget covered its thinking as well.
        # These models do not think, and the measured median verdict is 211
        # tokens against a longest-seen 327. 2048 is ~6x the longest real
        # answer, so it truncates nothing; what it does is cap the cost of a
        # degenerate generation at 2048 instead of 4096.
        "max_tokens": 2048,
        "temperature": temperature,
        "top_p": 1.0,
        # DRY, not repeat_penalty. Greedy decoding on a free-text field inside
        # a grammar is prone to locking into a loop — three of the first
        # twelve named requests ran to the token ceiling mid-string and came
        # back as unparseable JSON, and those three were 87% of the decode
        # work in that batch. A flat repetition penalty is the wrong tool
        # here because it also penalises the structural tokens JSON must
        # repeat (quotes, braces, commas). DRY penalises repeated SEQUENCES
        # past `allowed_length`, which is exactly the failure and nothing
        # else. These are llama.cpp's own recommended defaults.
        "dry_multiplier": 0.8,
        "dry_base": 1.75,
        "dry_allowed_length": 4,
        # llama.cpp converts this to a GBNF grammar and constrains decoding,
        # so a malformed verdict is not a failure mode the way it is over an
        # API. `strict` is accepted and ignored by some builds; harmless.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "strict": True, "schema": schema},
        },
    }


def score_one(client: httpx.Client, req: dict,
              temperature: float = 0.0) -> dict | None:
    """One verdict, or None.

    Retries are NOT simple repeats. The primary decode is greedy and
    therefore deterministic, so re-sending an identical payload after a
    parse failure reproduces the identical failure — that is how the first
    smoke test burned four attempts each on three events before dropping
    them. The escalation adds temperature only after greedy has failed, so
    the run stays reproducible on every event that succeeds first time and
    only the salvaged ones carry sampling noise. Which events those were is
    recorded in the row as `salvaged`.
    """
    base = to_openai(req, temperature)
    for attempt in range(3):
        payload = dict(base)
        if attempt and not temperature:
            payload["temperature"] = 0.7
            payload["seed"] = 20260807 + attempt
        try:
            r = client.post(f"{ENDPOINT}/v1/chat/completions",
                            json=payload, timeout=600)
            r.raise_for_status()
            doc = r.json()
            text = doc["choices"][0]["message"]["content"]
            verdict = json.loads(text)
            usage = doc.get("usage") or {}
            return {
                "custom_id": req["custom_id"],
                "verdict": verdict,
                "in": usage.get("prompt_tokens"),
                "out": usage.get("completion_tokens"),
                "salvaged": bool(attempt),
            }
        except Exception as exc:                       # noqa: BLE001
            if attempt == 2:
                print(f"  DROP {req['custom_id']}: {type(exc).__name__} {exc}")
                return None
            time.sleep(1)
    return None


# ------------------------------------------- the forced memory probe (notextf)
#
# WHY THIS ARM HAD TO BE ADDED. The `notext` probe asks the model whether it
# recalls an event and lets it answer "no / unknown". On Opus that works: it
# claimed recall on 41.4% and volunteered 175 directions at 66.3% accuracy.
# On phi-4 it returned `no` and `unknown` on 489 of 489 events, which looks
# like a clean model until you check whether phi-4 will admit to recalling
# ANYTHING. It will not, reliably: asked three plain finance questions with
# no schema attached, it opened with an unprompted disclaimer about
# election-related matters, refused Microsoft's FY2023 revenue citing a
# knowledge cutoff — and then correctly described Meta's post-Q4-2021 drop.
# So it has some memory of major market events AND a strong refusal habit,
# and the free probe cannot tell those apart. 489/489 "no" measures
# willingness to claim recall, not recall.
#
# The fix is the one the study already uses on `direction`: remove the
# escape hatch. Force a call of up or down from memory alone. A model with no
# recall then scores 50%, a model with recall scores above it, and refusal
# tuning cannot express itself as an abstention because there is none to
# give. This is the instrument the cleanliness claim actually rests on; the
# free-response `notext` arm is kept because it is what Opus ran, and the two
# are reported side by side.
NOTEXTF_SCHEMA = {
    "type": "object",
    "properties": {
        "recalled_next_session_direction": {
            "type": "string", "enum": ["up", "down"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "basis": {"type": "string"},
    },
    "required": ["recalled_next_session_direction", "confidence", "basis"],
    "additionalProperties": False,
}


def _notextf_requests(universe: pd.DataFrame, model: str,
                      limit: int | None) -> list[dict]:
    """Built here rather than in build_request because this arm exists only
    for the local models — it is a repair to an instrument that did not need
    repairing on Opus, and the shared module should not grow an arm no
    Anthropic run will ever use."""
    from research_llm_contamination import HARDENED_SYSTEM, task_notext

    system = (
        HARDENED_SYSTEM + "\n\nYou are given no release text and no market "
        "data. This is a calibration probe of memory, and it is FORCED: you "
        "must answer 'up' or 'down' for how the shares traded in the next "
        "full session. 'I do not know' is not available and refusing is not "
        "an option. If you have no memory of this report, say so in `basis`, "
        "set `confidence` to 'low', and guess anyway — a coin flip is the "
        "correct behaviour there and it is what the analysis expects. Do not "
        "reason from the ticker's general reputation; answer from whatever "
        "specific memory of this quarter you have, or guess."
    )
    done = _completed_ids()
    reqs = []
    for _, row in universe.iterrows():
        ymd = str(row["date"]).replace("-", "")
        key = f"notextf_medium_{TAG_OF[model]}_{row['symbol']}_{ymd}"
        if key in done:
            continue
        reqs.append({
            "custom_id": key,
            "params": {
                "model": model, "max_tokens": 2048, "system": system,
                "output_config": {"format": {"schema": NOTEXTF_SCHEMA}},
                "messages": [{"role": "user",
                              "content": task_notext(row["symbol"],
                                                     str(row["date"]))}],
            },
        })
        if limit and len(reqs) >= limit:
            break
    return reqs


# -------------------------------------------------------------------- run

class _RunLock:
    """One writer per (arm, model), enforced on disk.

    THE FAILURE THIS PREVENTS, which actually happened on 2026-08-07: the
    driver was stopped by killing its shell, the shell died, and its python
    child did not. A second driver was then started, both processes read
    `_completed_ids()` at their own startup — each seeing a set that did not
    yet contain what the other was about to write — and both appended to
    results_notext_phi4.jsonl. The file ended up with 2,609 rows for 1,815
    events. Nothing in _load_results deduplicates, so 794 events would have
    been counted twice in every mean, every spread and every permutation
    draw, and the only visible symptom was a row count nobody was checking.

    The dedupe-on-read in _panel is the belt; this is the braces, because a
    silent 44% double-count is not a thing to rely on catching downstream.
    """

    def __init__(self, arm: str, tag: str) -> None:
        self.path = OUT / f".lock_{arm}_{tag}"

    def __enter__(self):
        OUT.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = self.path.read_text(encoding="utf-8").strip()
            raise SystemExit(
                f"another run already holds {self.path.name} ({held}).\n"
                f"If that process is gone, delete the file and retry — but "
                f"check for orphans FIRST:\n"
                f"  Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
                f" | ? {{ $_.CommandLine -match 'research_local_gate' }}"
            ) from None
        os.write(fd, f"pid={os.getpid()} started={_dt.datetime.now():%H:%M:%S}"
                 .encode())
        os.close(fd)
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def stage_run(args) -> None:
    with _RunLock(args.arm, args.model):
        _run(args)


def _run(args) -> None:
    tag = args.model
    model_id = MODEL_ID[tag]
    wait_healthy()
    universe = universe_for(args)
    universe["date"] = universe["date"].astype(str)
    print(f"{len(universe)} events in the panel")

    if args.arm == "notextf":
        reqs = _notextf_requests(universe, model_id, args.limit)
    else:
        reqs = _requests_for(args.arm, universe, "medium", args.limit,
                             model_id)
    if not reqs:
        print(f"nothing to do: every {args.arm} request for {tag} is on disk")
        return
    print(f"arm={args.arm} model={tag}: {len(reqs)} requests "
          f"({len(_completed_ids())} ids already complete across all runs)")

    path = OUT / f"results_{args.arm}_{tag}.jsonl"
    lock = threading.Lock()
    done = failed = 0
    t0 = time.time()
    slots = args.slots or LOCAL[tag]["slots"]

    with httpx.Client() as client, path.open("a", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=slots) as pool:
            futures = [pool.submit(score_one, client, r, args.temp)
                       for r in reqs]
            for fut in as_completed(futures):
                row = fut.result()
                with lock:
                    if row is None:
                        failed += 1
                    else:
                        # arm/effort are written explicitly so the file is
                        # readable on its own; the loader re-derives both
                        # from the custom_id and does not depend on them.
                        fh.write(json.dumps({**row, "arm": args.arm,
                                             "effort": "medium"}) + "\n")
                        fh.flush()
                        done += 1
                    n = done + failed
                    if n % 25 == 0 or n == len(reqs):
                        rate = n / max(time.time() - t0, 1e-9)
                        eta = (len(reqs) - n) / max(rate, 1e-9) / 60
                        print(f"  {n}/{len(reqs)} ok={done} drop={failed} "
                              f"{rate * 60:.1f}/min eta={eta:.0f}min",
                              flush=True)
    print(f"wrote {done} verdicts to {path.name} ({failed} dropped) in "
          f"{(time.time() - t0) / 60:.1f}min")


# ------------------------------------------------------------------ scoring

def _panel(effort: str = "medium") -> pd.DataFrame:
    """Every verdict from every model, merged onto the event panel.

    The v2 panel carries the reaction (`react`) but NOT the forward return —
    the exit close lives in event_paths_v2.parquet and has to be joined on.
    This is the same construction research_out_of_training.panel() uses, and
    it is duplicated rather than imported because that function hard-filters
    to arm=="named" and this module needs `notext` and `forced` too.
    """
    from research_event_panel import load_universe_v2
    from research_holding_period import paths_file

    res = _load_results(model=None)
    if res.empty:
        raise SystemExit("no verdicts on disk")
    res = res[res["effort"] == effort]
    # _load_results appends every line it finds and deduplicates nothing.
    # One event scored twice would be counted twice everywhere downstream,
    # so collapse on the identity the custom_id encodes. Reported, not
    # silent: a nonzero count here means a writer raced and wants
    # investigating, not shrugging at.
    before = len(res)
    res = res.drop_duplicates(subset=["arm", "model", "symbol", "date"])
    if before != len(res):
        print(f"WARNING: dropped {before - len(res)} duplicate verdict rows "
              f"(same arm/model/symbol/date) before scoring")

    universe = load_universe_v2()
    paths = pd.read_parquet(paths_file("v2"))
    first_close = {(s, d): c[0] for s, d, c in
                   zip(paths["symbol"], paths["date"], paths["closes"])}
    universe["exit"] = [first_close.get((s, d), np.nan) for s, d
                        in zip(universe["symbol"], universe["date"])]
    universe["fwd_bp"] = (universe["exit"] / universe["react"] - 1) * 1e4
    universe = universe[universe["exit"].notna()]
    universe["date"] = universe["date"].astype(str)

    m = res.merge(universe[["symbol", "date", "move_pct", "fwd_bp",
                            "year", "month"]],
                  on=["symbol", "date"], how="inner")
    m["mech_pnl"] = np.sign(m["move_pct"]) * m["fwd_bp"] - COSTS_BP
    return m


def _arm_row(g: pd.DataFrame) -> dict:
    keep = _gate(g)
    return {
        "n": len(g),
        "ungated": g["mech_pnl"].mean(),
        "gated": g.loc[keep, "mech_pnl"].mean() if keep.any() else np.nan,
        "vetoed": g.loc[~keep, "mech_pnl"].mean() if (~keep).any() else np.nan,
        "spread": _spread(g),
        "keeps": keep.mean() * 100,
    }


def stage_score(args) -> None:
    m = _panel()
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    study = "claude-opus-5"
    locals_present = [t for t in LOCAL if (m["model"] == MODEL_ID[t]).any()]
    if not locals_present:
        raise SystemExit("no local verdicts on disk yet")
    cohort = [study, *[MODEL_ID[t] for t in locals_present]]
    name_of = {mid: nm for _, (mid, _, nm) in MODELS.items()}

    emit("# The gate re-scored by open weights on one 3090")
    emit("")
    emit(f"generated {date.today()}; schema-constrained decoding, prompts "
         f"byte-identical to the Opus 5 run. Reading arms greedy; the forced "
         f"memory probe sampled at temperature 1.0 (see below for why).")
    emit("")

    # ------------------------------------------------- the forced memory probe
    #
    # Reported FIRST because it is the one the cleanliness claim rests on,
    # and because the free-response arm below it cannot carry that weight.
    ntf = m[(m["arm"] == "notextf") & (m["model"].isin(cohort))]
    if not ntf.empty:
        emit("## Forced memory probe — the cleanliness measurement")
        emit("")
        emit("Ticker and date, no release text, and NO option to abstain: the "
             "model must answer up or down for the next session. There is no "
             "information in this prompt, so accuracy above 50% is recall and "
             "nothing else, and refusal tuning cannot hide in an abstention "
             "because none is offered. Sampled rather than greedy — greedy "
             "collapses a no-signal model onto one constant answer whose "
             "accuracy is merely the base rate of up-moves.")
        emit("")
        emit("| model | n | said up | accuracy | z vs chance | 95% CI |")
        emit("|---|---|---|---|---|---|")
        for mid in cohort:
            g = ntf[ntf["model"] == mid].dropna(subset=["fwd_bp"])
            if g.empty:
                continue
            up = g["recalled_next_session_direction"] == "up"
            hit = float(((g["fwd_bp"] > 0) == up).mean())
            n = len(g)
            se = np.sqrt(0.25 / n)
            lo, hi = hit - 1.96 * se, hit + 1.96 * se
            emit(f"| {name_of.get(mid, mid)} | {n} | {up.mean() * 100:.0f}% | "
                 f"{hit * 100:.1f}% | {(hit - 0.5) / se:+.2f} | "
                 f"[{lo * 100:.1f}, {hi * 100:.1f}] |")
        emit("")
        emit("A model whose interval covers 50% has no usable recall of these "
             "events, and nothing its reading arms earn can have come from "
             "remembering the outcome.")
        emit("")

    # ------------------------------------------------ the free-response probe
    nt = m[(m["arm"] == "notext") & (m["model"].isin(cohort))]
    if not nt.empty:
        emit("## Free-response memory probe — what Opus ran, and why it is "
             "not enough on its own")
        emit("")
        emit("Same prompt, but 'no' and 'unknown' are available. This is the "
             "arm the study ran on Opus, kept here for comparability. It is "
             "NOT a safe cleanliness test for a heavily refusal-tuned model: "
             "phi-4 answered 'no recall' on every single event, and a "
             "no-schema control showed it will refuse a plain question about "
             "Microsoft's FY2023 revenue while correctly recalling Meta's "
             "post-Q4-2021 drop. On such a model this arm measures "
             "willingness to claim recall, not recall.")
        emit("")
        emit("| model | n | claims recall | volunteers a direction | "
             "recall accuracy | z vs chance |")
        emit("|---|---|---|---|---|---|")
        for mid in cohort:
            g = nt[nt["model"] == mid]
            if g.empty:
                continue
            rec = (g["recalls_event"] == "yes").mean() * 100
            vol = g[g["recalled_next_session_direction"] != "unknown"]
            if len(vol):
                acc = float(((vol["fwd_bp"] > 0) ==
                             (vol["recalled_next_session_direction"] == "up")).mean())
                z = (acc - 0.5) / np.sqrt(0.25 / len(vol))
                accs, zs = f"{acc * 100:.1f}%", f"{z:+.2f}"
            else:
                accs, zs = "—", "—"
            emit(f"| {name_of.get(mid, mid)} | {len(g)} | {rec:.1f}% | "
                 f"{len(vol)} ({len(vol) / len(g) * 100:.1f}%) | {accs} | {zs} |")
        emit("")
        emit("Read this table only as a contrast in DISCLOSURE, not in "
             "recall: Opus volunteers and is right well above chance, which "
             "establishes that it has recall; a 0% volunteer rate establishes "
             "only that the model declines. The forced probe above is what "
             "settles whether anything is behind the refusal.")
        emit("")

    # ----------------------------------------------------- the scored arms
    #
    # `named` is the replication: it is the live prompt, and it is the only
    # arm directly comparable to the paper's headline. `forced` exists
    # because the local models turned out to abstain at a completely
    # different rate — phi-4 answers `neutral` on ~83% of releases against
    # Opus's 16% — and the gate vetoes every neutral. On the named arm that
    # difference alone would drive the comparison, and "the clean model found
    # no edge" would be indistinguishable from "the clean model declined to
    # answer". The forced arm removes `neutral` from the enum and asks the
    # SAME question of the SAME releases, so what it compares is directional
    # discrimination rather than willingness to commit. It is where the power
    # is; `named` is where the comparability is.
    rng = np.random.default_rng(20260807)
    for arm in ("named", "forced"):
        a = m[(m["arm"] == arm) & (m["model"].isin(cohort))]
        if a.empty:
            continue
        # The cohort is PER ARM, not global. Opus 5 has no `forced` verdicts
        # on disk — the arm was written and never collected — so requiring
        # every model in the global cohort would silently empty this table
        # rather than report the comparison that is actually available.
        arm_cohort = [mid for mid in cohort if (a["model"] == mid).any()]
        k = a.groupby(["symbol", "date"])["model"].nunique()
        both = set(k[k == len(arm_cohort)].index)
        pair = a[a.set_index(["symbol", "date"]).index.isin(both)]
        if pair.empty:
            continue

        emit(f"## `{arm}` arm — paired on the {len(both)} events every model "
             f"below scored")
        emit("")
        if study not in arm_cohort:
            emit("**Opus 5 is absent from this arm**: no `forced` verdicts "
                 "were ever collected for it, so this table compares the two "
                 "local models to each other and not to the study model. "
                 "That is still the comparison the arm exists for — phi-4 "
                 "against a same-weight-class model that IS heavily "
                 "web-trained — but the Opus reference here has to come from "
                 "the `named` arm above.")
            emit("")
        # TWO accuracies, because they answer different questions and quoting
        # one as "directional accuracy" invites reading it as the other.
        #   predicts tape  — does the verdict match the sign of the REACTION?
        #                    This is what _gate keys on, so it drives keeps
        #                    and therefore the spread.
        #   predicts drift — does the verdict match the sign of the next
        #                    session's return? This is what actually pays if
        #                    you trade the verdict directly, and it is a much
        #                    harder target: the reaction is observable minutes
        #                    after the release, the drift is not.
        emit("| model | n | neutral | ungated | gated | vetoed | spread | "
             "keeps | predicts tape | predicts drift |")
        emit("|---|---|---|---|---|---|---|---|---|---|")
        perm: dict[str, float] = {}
        for mid in arm_cohort:
            g = pair[pair["model"] == mid].reset_index(drop=True)
            if g.empty:
                continue
            r = _arm_row(g)
            neut = (g["direction"] == "neutral").mean() * 100
            # Directional accuracy on the calls that took a side. Far better
            # powered than the bp spread, because one earnings reaction is
            # worth hundreds of bp and the variance swamps the mean; convert
            # back with 2 x mean|fwd_bp| x delta-accuracy.
            side = g[g["direction"] != "neutral"]
            if len(side):
                bull = side["direction"] == "bullish"
                def _fmt(hit, n):
                    return (f"{hit * 100:.1f}% (z="
                            f"{(hit - 0.5) / np.sqrt(0.25 / n):+.2f})")
                tape = _fmt(float(((side["move_pct"] > 0) == bull).mean()),
                            len(side))
                acc = _fmt(float(((side["fwd_bp"] > 0) == bull).mean()),
                           len(side))
                acc = f"{tape} | {acc}"
            else:
                acc = "— | —"
            emit(f"| {name_of.get(mid, mid)} | {r['n']} | {neut:.0f}% | "
                 f"{r['ungated']:+.1f} | {r['gated']:+.1f} | "
                 f"{r['vetoed']:+.1f} | {r['spread']:+.1f} | "
                 f"{r['keeps']:.0f}% | {acc} |")
            perm[mid] = _permutation_p(g, rng)
        emit("")
        emit(f"Permutation null ({N_PERM} within-month shuffles of the "
             "verdict labels), p(spread >= observed):")
        for mid, p in perm.items():
            emit(f"  - {name_of.get(mid, mid)}: p = {p:.3f}")
        emit("")

        piv = pair.pivot_table(index=["symbol", "date"], columns="model",
                              values="direction", aggfunc="first")
        for mid in arm_cohort[1:]:
            if study in piv and mid in piv:
                ok = piv[[study, mid]].dropna()
                emit(f"- {name_of.get(mid, mid)} agrees with Opus 5 on "
                     f"{(ok[study] == ok[mid]).mean() * 100:.1f}% of "
                     f"{len(ok)} paired events.")
        emit("")

    named = m[(m["arm"] == "named") & (m["model"].isin(cohort))]
    if named.empty:
        _write(lines, args)
        return

    # ------------------------------------------- the strictly-clean window
    emit("## Events after each local model's pretraining cutoff")
    emit("")
    emit("Memorisation is impossible by construction on this side of the "
         "line. It is a smaller sample and a different market regime, so the "
         "comparison that matters is a model against ITSELF across the "
         "boundary, not against the other window's absolute level.")
    emit("")
    emit("| model | window | n | gated | vetoed | spread |")
    emit("|---|---|---|---|---|---|")
    for tag in locals_present:
        mid, cut = MODEL_ID[tag], MODELS[tag][1]
        g = named[named["model"] == mid]
        post = g[g["date"] > f"{cut}-31"].reset_index(drop=True)
        pre = g[g["date"] <= f"{cut}-31"].reset_index(drop=True)
        for label, sub in (("in-corpus", pre), (f"post-{cut}", post)):
            if sub.empty:
                continue
            r = _arm_row(sub)
            emit(f"| {name_of.get(mid, mid)} | {label} | {r['n']} | "
                 f"{r['gated']:+.1f} | {r['vetoed']:+.1f} | "
                 f"{r['spread']:+.1f} |")
    emit("")

    emit("## What this can and cannot settle")
    emit("")
    emit("- It CANNOT prove any model is uncontaminated. Both have seen most "
         "of this decade. The notext arm bounds what they can recall; it "
         "does not certify a corpus.")
    emit("- A weak local result is NOT evidence for memorisation in Opus. "
         "Capability and cleanliness both push the same direction, which is "
         "exactly why the Qwen control is here.")
    emit("- Quantisation and greedy decoding handicap both local models. The "
         "handicap runs against the capability control, not for it.")
    _write(lines, args)


def _write(lines: list[str], args) -> None:
    out = NOTES / f"local_gate_{date.today():%Y%m%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


# -------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="stage", required=True)

    s = sub.add_parser("serve", help="launch llama-server for one model")
    s.add_argument("--model", choices=list(LOCAL), required=True)
    s.set_defaults(fn=stage_serve)

    r = sub.add_parser("run", help="score one arm with one local model")
    r.add_argument("--model", choices=list(LOCAL), required=True)
    r.add_argument("--arm", choices=["named", "notext", "notextf", "blind", "forced"],
                   default="named")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--slots", type=int, default=None)
    # notextf is run SAMPLED, not greedy. Greedy on a probe the
    # model has no signal for collapses to one constant answer,
    # whose accuracy is just the base rate of up-moves and cannot
    # detect a weak signal. Sampling lets the answer vary, so
    # accuracy above 50% over 1,815 events (SE 1.2pp) is real
    # recall and 50.0% is real absence.
    r.add_argument("--temp", type=float, default=0.0)
    r.add_argument("--panel", default="v2")
    r.add_argument("--windows", default="ten")
    r.set_defaults(fn=stage_run)

    c = sub.add_parser("score", help="gate spread against the Opus run")
    c.set_defaults(fn=stage_score)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
