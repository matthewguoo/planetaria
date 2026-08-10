"""Fable 5 as the capability-inverted memorisation probe — CLI edition.

WHY THIS MODEL SETTLES SOMETHING THE OTHERS COULD NOT. Every boundary the
study has crossed so far confounds corpus with capability in the SAME
direction: the model with the later cutoff is also the newer, stronger model,
so "in-corpus models look better" and "better models look better" are the same
observation. The handoff of 2026-08-07 left two readings standing — the edge
is largely recall vs the edge is reading — because the only clean cells were
starved (37 events after Opus 5's cutoff).

Fable 5 inverts the confound. Its published training cutoff is 2026-01, four
months BEFORE Opus 5's 2026-05, while the model itself is a tier ABOVE
Opus 5. On events in (2026-01-31, 2026-05-31] the weaker-corpus model is the
stronger reader:

    capability story  ->  Fable >= Opus 5 inside the straddle window, as
                          everywhere else
    recall story      ->  Opus 5 beats Fable INSIDE the straddle window and
                          nowhere else

The straddle window holds a full earnings season, not 37 events, and Fable
shares its 2026-01 cutoff with Opus 4.7/4.8 — the same-cutoff placebo pair
becomes a trio.

TRANSPORT: THE CLAUDE CODE CLI ON SUBSCRIPTION AUTH, NOT THE API. The .env
API key 401s (rotated after the 8/5 leak advisory) and Matthew's instruction
is to spend the subscription window instead. The invocation is byte-for-byte
the engine's own ClaudeCliClient (app/services/llm.py, verified 2026-08-05):
`claude -p --system-prompt ... --output-format json --json-schema ...
--allowedTools "" --permission-mode dontAsk --model claude-fable-5`, prompt on
stdin, ANTHROPIC_* stripped from the child env so auth is the subscription.
--system-prompt REPLACES the Claude Code context and --allowedTools "" keeps
the no-retrieval invariant, so the instrument stays close to the API one.

DIFFERENCES FROM THE API INSTRUMENT, disclosed here once and in the notes:

  - No effort control: the CLI exposes no effort flag, so calls run at the
    CLI's default for the model. Ids are labelled effort=medium for join
    compatibility with every cached analysis — the research_local_gate
    precedent: a label, not a claim about compute.
  - Thinking is always on for Fable (an explicit "disabled" is a 400 API-side
    and does not exist CLI-side). The forced probe therefore runs HOT: a
    scratchpad can dredge memory, so its number is the ceiling of what the
    model can recall, and its post-cutoff cell (events after 2026-01-31,
    where recall is impossible) is the within-model null that validates the
    instrument, exactly as the local models' cutoffs validated theirs.
  - Sampling is the model/CLI default (`temperature` is rejected on Fable),
    effectively what the Opus control ran at (1.0).

RUN DISCIPLINE, learned the hard way on 2026-08-07: one _RunLock so two
drivers cannot interleave appends; every id checked against _completed_ids
at build; the writer is a single lock-guarded appender; usage-limit errors
back off and eventually stop the run cleanly with everything banked so far.
Partial coverage is planned for, not an accident: notextf iterates the panel
year-interleaved so any prefix is year-balanced, and named iterates
straddle -> adjacent in-corpus -> post-cutoff -> older, so the decisive
paired cells fill first.

Run:
    python scripts/research_fable_memorization.py run --arm notextf
    python scripts/research_fable_memorization.py run --arm named
    python scripts/research_fable_memorization.py run --arm notext --sample 600
    python scripts/research_fable_memorization.py analyze
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from _paths import NOTES

import numpy as np
import pandas as pd

from research_llm_contamination import (
    HARDENED_SYSTEM,
    MODELS,
    OUT,
    _completed_ids,
    _load_results,
    _requests_for,
    stratified_sample,
    task_notext,
    universe_for,
)
from research_local_gate import NOTEXTF_SCHEMA

FABLE = "claude-fable-5"
OPUS5 = "claude-opus-5"

CUT_OF = {mid: cut for _, (mid, cut, _) in MODELS.items()}
NAME_OF = {mid: name for _, (mid, _, name) in MODELS.items()}

# The three calendar cells the design turns on, month-end convention
# (research_out_of_training._cutoff_end): an event dated 2026-01-31 is
# credited to every 2026-01 corpus.
STRADDLE_LO, STRADDLE_HI = "2026-02-01", "2026-05-31"
NAMED_FROM = "2025-02-01"       # the named arm's window


def _cell(day: str) -> str:
    if day <= "2026-01-31":
        return "both_in"
    if day <= STRADDLE_HI:
        return "straddle"
    return "both_out"


# Byte-identical to research_notextf_opus.build and
# research_local_gate._notextf_requests — the probe's validation (predicted
# 56.7%, landed 57.1%) is a property of THIS wording; do not drift.
FORCED_SYSTEM = (
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

# Signatures of a spent subscription window in CLI stderr/results. Matched
# case-insensitively; on sight the run backs off and, if persistent, stops
# cleanly rather than burning retries against a wall.
LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "out of usage",
                 "hit your limit", "usage_limit", "overloaded")


class _RunLock:
    """One driver at a time. The 2026-08-07 incident: TaskStop left a driver
    alive, a second one started, both appended, 794 silent duplicates."""

    def __init__(self, name: str):
        self.path = OUT / f"{name}.lock"

    def __enter__(self):
        OUT.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise SystemExit(f"{self.path} exists — another driver may be "
                             f"running (pid {self.path.read_text().strip()}). "
                             f"Verify it is dead, delete the lock, retry.")
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------ request builds

def _year_interleaved(u: pd.DataFrame) -> pd.DataFrame:
    """Round-robin across years so any prefix of the run is year-balanced —
    a partial panel is then a smaller version of the same measurement, not a
    measurement of whichever years sorted first."""
    groups = [g.sort_values(["date", "symbol"]).reset_index(drop=True)
              for _, g in u.groupby("year")]
    rows, i = [], 0
    while any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g):
                rows.append(g.iloc[i])
        i += 1
    return pd.DataFrame(rows).reset_index(drop=True)


def _build_notextf(limit: int | None) -> list[dict]:
    u = universe_for(argparse.Namespace(panel="v2", windows="ten"))
    u["date"] = u["date"].astype(str)
    u = _year_interleaved(u)
    done = _completed_ids()
    reqs = []
    for _, row in u.iterrows():
        ymd = row["date"].replace("-", "")
        key = f"notextf_medium_f5_{row['symbol']}_{ymd}"
        if key in done:
            continue
        reqs.append({
            "custom_id": key,
            "system": FORCED_SYSTEM,
            "schema": NOTEXTF_SCHEMA,
            "prompt": task_notext(row["symbol"], row["date"]),
        })
        if limit and len(reqs) >= limit:
            break
    return reqs


def _named_priority(day: str) -> int:
    if STRADDLE_LO <= day <= STRADDLE_HI:
        return 0                       # the capability-inverted cell
    if "2025-08-01" <= day <= "2026-01-31":
        return 1                       # adjacent in-corpus baseline
    if day > STRADDLE_HI:
        return 2                       # both-out tail
    return 3                           # older in-corpus months


def _build_api_shaped(arm: str, limit: int | None,
                      sample: int | None) -> list[dict]:
    """notext / named requests via the shared build_request path, translated
    to the CLI's argument surface. build_request's thinking/effort fields have
    no CLI equivalent and are dropped — see the module docstring."""
    u = universe_for(argparse.Namespace(panel="v2", windows="ten"))
    u["date"] = u["date"].astype(str)
    if arm == "named":
        u = u[u["date"] >= NAMED_FROM].copy()
        u["_prio"] = u["date"].map(_named_priority)
        u = u.sort_values(["_prio", "date", "symbol"])
    if sample:
        u = stratified_sample(u, sample)
    api_reqs = _requests_for(arm, u, "medium", limit, FABLE)
    out = []
    for r in api_reqs:
        p = r["params"]
        out.append({
            "custom_id": r["custom_id"],
            "system": p["system"],
            "schema": p["output_config"]["format"]["schema"],
            "prompt": p["messages"][0]["content"],
        })
    return out


# ------------------------------------------------------------------ CLI call

def _cli_env() -> dict:
    """Subscription auth, exactly as the engine forces it."""
    return {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                         "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")}


def _call_cli(exe: str, req: dict, timeout_s: float) -> tuple[dict | None, str]:
    """One `claude -p` process. Returns (record, "") or (None, error-string).
    The argument vector is the engine's ClaudeCliClient verbatim."""
    args = [
        exe, "-p",
        "--system-prompt", req["system"],
        "--output-format", "json",
        "--json-schema", json.dumps(req["schema"]),
        "--allowedTools", "",
        "--permission-mode", "dontAsk",
        "--model", FABLE,
    ]
    try:
        proc = subprocess.run(
            args, input=req["prompt"].encode("utf-8"),
            capture_output=True, timeout=timeout_s, env=_cli_env())
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout_s:.0f}s"
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        # The CLI writes usage-limit refusals to STDOUT (learned 2026-08-09:
        # 1,024 calls burned against a spent window as bare "exit 1" because
        # this error string quoted stderr alone and the limit markers never
        # saw the message). Quote both.
        out_tail = proc.stdout.decode("utf-8", "replace").strip()[-300:]
        return None, f"exit {proc.returncode}: {err[:300]} {out_tail}"
    try:
        envelope = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        return None, f"bad envelope: {exc}"
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        return None, f"cli error: {str(envelope.get('result'))[:300]}"
    try:
        verdict = json.loads(envelope.get("result") or "")
    except (TypeError, ValueError) as exc:
        return None, f"unparseable result: {exc}"
    usage = envelope.get("usage") or {}
    return {
        "verdict": verdict,
        "in": usage.get("input_tokens"),
        "out": usage.get("output_tokens"),
    }, ""


def stage_run(args) -> None:
    exe = shutil.which("claude")
    if exe is None:
        raise SystemExit("claude CLI not on PATH")
    if args.arm == "notextf":
        reqs = _build_notextf(args.limit)
        timeout_s = 120.0
    else:
        reqs = _build_api_shaped(args.arm, args.limit, args.sample)
        timeout_s = 300.0 if args.arm == "named" else 120.0
    if not reqs:
        print(f"nothing to do — every {args.arm} id for f5 is on disk")
        return
    print(f"arm={args.arm}: {len(reqs)} calls via claude CLI "
          f"({args.workers} workers, subscription auth)")

    path = OUT / f"results_{args.arm}_f5.jsonl"
    write_lock = threading.Lock()
    stop = threading.Event()
    done_n = fail_n = 0
    t0 = time.time()

    def work(req: dict) -> None:
        nonlocal done_n, fail_n
        if stop.is_set():
            return
        rec, err = _call_cli(exe, req, timeout_s)
        if rec is None and any(m in err.lower() for m in LIMIT_MARKERS):
            # One backoff, one retry; a second sighting stops the run with
            # everything so far banked. The window refills on its own clock.
            time.sleep(90)
            rec, err = _call_cli(exe, req, timeout_s)
            if rec is None and any(m in err.lower() for m in LIMIT_MARKERS):
                if not stop.is_set():
                    print(f"USAGE LIMIT — stopping cleanly: {err[:160]}")
                stop.set()
                return
        if rec is None:
            rec2, err2 = _call_cli(exe, req, timeout_s)   # one plain retry
            rec, err = (rec2, err2) if rec2 is not None else (None, err)
        with write_lock:
            if rec is None:
                fail_n += 1
                print(f"  DROP {req['custom_id']}: {err[:160]}")
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "custom_id": req["custom_id"], "arm": args.arm,
                    "effort": "medium", "verdict": rec["verdict"],
                    "in": rec["in"], "out": rec["out"]}) + "\n")
            done_n += 1
            if done_n % 25 == 0:
                rate = done_n / max(time.time() - t0, 1)
                left = (len(reqs) - done_n - fail_n) / max(rate, 1e-9)
                print(f"  {done_n}/{len(reqs)} ok, {fail_n} dropped, "
                      f"{rate * 60:.1f}/min, ~{left / 60:.0f} min left",
                      flush=True)

    from concurrent.futures import ThreadPoolExecutor

    with _RunLock(f"fable_{args.arm}"):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, reqs))
    status = "STOPPED AT USAGE LIMIT" if stop.is_set() else "complete"
    print(f"{status}: {done_n} verdicts written, {fail_n} dropped, "
          f"{(time.time() - t0) / 60:.1f} min -> {path.name}")


# ------------------------------------------------------------------ analysis

def _outcomes() -> pd.DataFrame:
    """The event panel with the next-session outcome, the same resolution
    research_out_of_training uses: first close after the reaction print."""
    from research_holding_period import paths_file

    universe = universe_for(argparse.Namespace(panel="v2", windows="ten"))
    universe["date"] = universe["date"].astype(str)
    paths = pd.read_parquet(paths_file("v2"))
    first_close = {(s, d): c[0] for s, d, c in
                   zip(paths["symbol"], paths["date"], paths["closes"])}
    universe["exit"] = [first_close.get((s, d), np.nan) for s, d
                        in zip(universe["symbol"], universe["date"])]
    universe["fwd_bp"] = (universe["exit"] / universe["react"] - 1) * 1e4
    universe = universe[universe["exit"].notna()].copy()
    universe["cell"] = universe["date"].map(_cell)
    return universe


def _probe_row(g: pd.DataFrame, label: str) -> dict:
    """Accuracy plus the association statistic the local-gate work showed is
    the honest one when the answer distribution is imbalanced."""
    n = len(g)
    up_said = g["said_up"]
    correct = (g["went_up"] == up_said)
    acc = float(correct.mean())
    se = float(np.sqrt(0.25 / n)) if n else np.nan
    a = g[up_said]
    b = g[~up_said]
    assoc = (float(a["went_up"].mean() - b["went_up"].mean())
             if len(a) >= 15 and len(b) >= 15 else None)
    return {
        "label": label, "n": n,
        "said_up_pct": round(float(up_said.mean()) * 100, 1),
        "acc_pct": round(acc * 100, 1),
        "z": round((acc - 0.5) / se, 2) if n else None,
        "assoc_pp": round(assoc * 100, 1) if assoc is not None else None,
    }


def stage_analyze(args) -> None:
    res = _load_results(model=None)
    if res.empty:
        raise SystemExit("no verdicts on disk")
    uni = _outcomes()
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    emit(f"# Fable 5 memorisation panel — {stamp}")
    emit()
    emit("Fable 5's training data ends 2026-01, four months before Opus 5's "
         "2026-05, while the model is the more capable of the two. Events in "
         f"({CUT_OF[FABLE]}, {CUT_OF[OPUS5]}] are therefore the first cells "
         "in this study where the corpus advantage and the capability "
         "advantage point in OPPOSITE directions. Fable verdicts here were "
         "collected through the Claude Code CLI on subscription auth "
         "(engine-identical invocation; no tools; system prompt replaced); "
         "the CLI exposes no effort control, so 'medium' in these ids is a "
         "join label, and Fable's always-on thinking makes its probes a "
         "ceiling on recall — see the module docstring.")
    emit()

    # ---------------------------------------------------- the forced probe
    ntf = res[res["arm"] == "notextf"].merge(
        uni[["symbol", "date", "fwd_bp", "cell", "year"]],
        on=["symbol", "date"], how="inner")
    ntf = ntf[ntf["recalled_next_session_direction"].isin(["up", "down"])]
    if not ntf.empty:
        ntf["said_up"] = ntf["recalled_next_session_direction"] == "up"
        ntf["went_up"] = ntf["fwd_bp"] > 0
        emit("## Forced memory probe (`notextf`) — ticker + date, no text, "
             "no abstention")
        emit()
        emit("| model | window | n | said up | accuracy | z | assoc (pp) |")
        emit("|---|---|---|---|---|---|---|")
        for mid in (FABLE, OPUS5, "phi-4-q8", "qwen3-30b-a3b-q4"):
            g = ntf[ntf["model"] == mid]
            if g.empty:
                continue
            cut = f"{CUT_OF[mid]}-31" if CUT_OF.get(mid) else None
            splits = [("all", g)]
            if cut:
                splits += [("in-corpus", g[g["date"] <= cut]),
                           ("post-cutoff", g[g["date"] > cut])]
            for label, gg in splits:
                if len(gg) < 30:
                    continue
                r = _probe_row(gg, label)
                assoc = "—" if r["assoc_pp"] is None else f"{r['assoc_pp']:+.1f}"
                emit(f"| {NAME_OF.get(mid, mid)} | {label} | {r['n']} | "
                     f"{r['said_up_pct']:.0f}% | {r['acc_pct']:.1f}% | "
                     f"{r['z']:+.2f} | {assoc} |")
        emit()
        f5 = ntf[ntf["model"] == FABLE]
        if not f5.empty:
            emit("### Fable recall by year — how deep does memory go?")
            emit()
            emit("| year | n | accuracy | z |")
            emit("|---|---|---|---|")
            for year, g in f5.groupby("year"):
                if len(g) < 30:
                    continue
                r = _probe_row(g, str(year))
                emit(f"| {year} | {r['n']} | {r['acc_pct']:.1f}% | "
                     f"{r['z']:+.2f} |")
            emit()
            emit("| confidence | n | accuracy |")
            emit("|---|---|---|")
            for conf in ("low", "medium", "high"):
                g = f5[f5["confidence"] == conf]
                if len(g) >= 15:
                    emit(f"| {conf} | {len(g)} | "
                         f"{(g['went_up'] == g['said_up']).mean() * 100:.1f}% |")
            emit()

    # ----------------------------------------------- free-response probe
    nt = res[(res["arm"] == "notext") & (res["model"] == FABLE)].merge(
        uni[["symbol", "date", "fwd_bp"]], on=["symbol", "date"], how="inner")
    if not nt.empty:
        claimed = nt[nt["recalls_event"] == "yes"]
        vol = nt[nt["recalled_next_session_direction"].isin(["up", "down"])]
        emit("## Free-response probe (`notext`) — disclosure, not recall")
        emit()
        emit(f"Fable claims recall on {len(claimed)}/{len(nt)} "
             f"({len(claimed) / len(nt) * 100:.1f}%); volunteered a direction "
             f"on {len(vol)} ({len(vol) / len(nt) * 100:.1f}%). Opus 5 for "
             "comparison: claimed 41.4%, volunteered 40.2% at 66.3% accuracy.")
        if len(vol) >= 30:
            acc = float(((vol["recalled_next_session_direction"] == "up")
                         == (vol["fwd_bp"] > 0)).mean())
            z = (acc - 0.5) / np.sqrt(0.25 / len(vol))
            emit(f"Volunteered-direction accuracy: {acc * 100:.1f}% "
                 f"(n={len(vol)}, z={z:+.2f}).")
        emit()

    # ------------------------------------------------------- the named arm
    named = res[(res["arm"] == "named") & (res["effort"] == "medium")].merge(
        uni[["symbol", "date", "move_pct", "fwd_bp", "cell"]],
        on=["symbol", "date"], how="inner")
    have_f5 = named[named["model"] == FABLE]
    if not have_f5.empty:
        named["side"] = np.where(named["direction"] == "bullish", 1,
                                 np.where(named["direction"] == "bearish", -1, 0))
        named["tape_ok"] = np.where(
            named["side"] == 0, np.nan,
            (named["side"] == np.sign(named["move_pct"])).astype(float))
        named["drift_ok"] = np.where(
            named["side"] == 0, np.nan,
            (named["side"] == np.sign(named["fwd_bp"])).astype(float))
        named["signed_bp"] = named["side"] * named["fwd_bp"]

        emit("## Named reading arm — the capability-inverted cells")
        emit()
        emit("`both_in` = through 2026-01-31 (in both corpora). `straddle` = "
             "2026-02-01..2026-05-31 (Fable out, Opus 5 in). `both_out` = "
             "after (in neither). Tape agreement is what the gate keys on; "
             "DRIFT accuracy is what pays.")
        emit()
        emit("| model | cell | n | neutral | tape acc | drift acc | signed bp |")
        emit("|---|---|---|---|---|---|---|")
        for mid in (FABLE, OPUS5, "claude-opus-4-8", "claude-opus-4-7"):
            for cell in ("both_in", "straddle", "both_out"):
                g = named[(named["model"] == mid) & (named["cell"] == cell)]
                if len(g) < 15:
                    continue
                emit(f"| {NAME_OF.get(mid, mid)} | {cell} | {len(g)} | "
                     f"{(g['side'] == 0).mean() * 100:.0f}% | "
                     f"{g['tape_ok'].mean() * 100:.1f}% | "
                     f"{g['drift_ok'].mean() * 100:.1f}% | "
                     f"{g['signed_bp'].mean():+.1f} |")
        emit()

        emit("### Within-event pairing, Opus 5 minus Fable 5")
        emit()
        emit("| cell | n events | drift-acc diff (pp) | se | signed-bp diff |")
        emit("|---|---|---|---|---|")
        rows = {}
        for cell in ("both_in", "straddle", "both_out"):
            a = named[(named["model"] == OPUS5) & (named["cell"] == cell)]
            b = named[(named["model"] == FABLE) & (named["cell"] == cell)]
            j = a.set_index(["symbol", "date"])[["drift_ok", "signed_bp"]] \
                 .join(b.set_index(["symbol", "date"])[["drift_ok", "signed_bp"]],
                       lsuffix="_o5", rsuffix="_f5", how="inner")
            d_acc = (j["drift_ok_o5"] - j["drift_ok_f5"]).dropna()
            d_bp = (j["signed_bp_o5"] - j["signed_bp_f5"]).dropna()
            if len(d_acc) < 15:
                continue
            se = float(d_acc.std(ddof=1) / np.sqrt(len(d_acc)))
            rows[cell] = (float(d_acc.mean()), se, len(d_acc))
            emit(f"| {cell} | {len(d_acc)} | {d_acc.mean() * 100:+.1f} | "
                 f"{se * 100:.1f} | {d_bp.mean():+.1f} |")
        emit()
        if "straddle" in rows and "both_in" in rows:
            (d1, s1, _), (d0, s0, _) = rows["straddle"], rows["both_in"]
            did = d1 - d0
            se = float(np.sqrt(s1 ** 2 + s0 ** 2))
            emit(f"**DiD (straddle minus both_in): {did * 100:+.1f}pp "
                 f"(se {se * 100:.1f}, z = {did / se:+.2f}).** Positive means "
                 "Opus 5 gains on Fable exactly where its corpus extends past "
                 "Fable's — the memorisation signature with capability "
                 "differenced out.")
            emit()

        for placebo_mid in ("claude-opus-4-8", "claude-opus-4-7"):
            rows_p = {}
            for cell in ("both_in", "straddle"):
                a = named[(named["model"] == placebo_mid)
                          & (named["cell"] == cell)]
                b = named[(named["model"] == FABLE) & (named["cell"] == cell)]
                j = a.set_index(["symbol", "date"])[["drift_ok"]] \
                     .join(b.set_index(["symbol", "date"])[["drift_ok"]],
                           lsuffix="_p", rsuffix="_f5", how="inner")
                d = (j["drift_ok_p"] - j["drift_ok_f5"]).dropna()
                if len(d) >= 15:
                    rows_p[cell] = (float(d.mean()),
                                    float(d.std(ddof=1) / np.sqrt(len(d))),
                                    len(d))
            if len(rows_p) == 2:
                (d1, s1, _), (d0, s0, _) = rows_p["straddle"], rows_p["both_in"]
                did = d1 - d0
                se = float(np.sqrt(s1 ** 2 + s0 ** 2))
                emit(f"Placebo {NAME_OF[placebo_mid]} minus Fable (both cut "
                     f"2026-01, no corpus contrast anywhere): DiD "
                     f"{did * 100:+.1f}pp (se {se * 100:.1f}). This is the "
                     "scale of a no-memorisation artefact.")
        emit()

        try:
            from research_out_of_training import balanced, panel, twfe
            cohort = (FABLE, OPUS5, "claude-opus-4-8", "claude-opus-4-7")
            bal = balanced(panel(models=cohort))
            if not bal.empty and bal["in_corpus"].nunique() == 2:
                for label, y in (("signed_bp", "signed_bp"),
                                 ("accuracy", "correct")):
                    est = twfe(bal, y)
                    if est.get("estimate") is not None:
                        unit = "bp" if label == "signed_bp" else " share"
                        emit(f"Two-way FE ({len(cohort)}-model cohort incl. "
                             f"Fable), outcome {label}: beta = "
                             f"{est['estimate']:+}{unit} (se {est['se']}, "
                             f"t = {est['t']}, {est['n_events']} events, "
                             f"90% CI {est['ci90']}).")
                emit()
        except SystemExit as exc:
            emit(f"(two-way FE unavailable: {exc})")
            emit()

    doc = NOTES / f"fable_memorization_{stamp}.md"
    doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {doc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["run", "analyze"])
    ap.add_argument("--arm", default="notextf",
                    choices=["notextf", "notext", "named"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sample", type=int, default=None,
                    help="year-stratified subsample (notext/named)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    {"run": stage_run, "analyze": stage_analyze}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
