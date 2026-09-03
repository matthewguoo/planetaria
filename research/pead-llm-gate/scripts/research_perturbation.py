"""Counterfactual perturbation: does the verdict follow the text, or the corpus?

WHY EVERY EARLIER TEST STALLED. The arms (5.7), the four-model design (5.9),
the decade holdout (5.8) and Appendix B share one structural defect: on a real
release, reading and remembering predict the SAME verdict. A model that read a
strong quarter says bullish; a model that recalls the stock rose says bullish.
No amount of sample size separates two hypotheses that make identical
predictions, which is why those tests could only ever produce bounds.

THE DESIGN THAT SEPARATES THEM. Feed the model a release whose economics have
been surgically reversed — a beat rewritten as a comparable miss, guidance
raised rewritten as cut — and the two hypotheses finally disagree:

    reading      -> the verdict follows the edited document and flips
    remembering  -> the verdict follows the true outcome and does not

The flip rate IS the reading share, measured per event and paired, which is
enormously better powered than any between-model contrast.

EDITS AS FIND/REPLACE, NOT AS A REWRITTEN DOCUMENT. The obvious construction
asks a model to emit the whole edited release. That costs ~4,900 output tokens
per variant, and worse, it lets the generator silently restyle prose it was
never asked to touch — at which point a verdict flip could be a response to
the rewrite rather than to the economics. Here the generator returns a LIST OF
REPLACEMENTS, applied programmatically, and every one is verified to match
exactly once before the variant is admitted. That is four times cheaper, and
it makes the perturbation auditable: the diff is the experiment.

THE PLACEBO IS NOT DECORATION. If verdicts also flip when employee counts and
dial-in numbers change, the model is keying on surface noise and the headline
flip rate means nothing. The placebo sets the floor that the reversal must
clear, and the reading share is the DIFFERENCE, not the raw rate.

THE GENERATOR IS NEVER TOLD THE OUTCOME. It sees the release and the edit
instruction. It does not see the next session's return, the tape reaction, or
which direction the study would like. A generator that knew the answer could
manufacture either result.

Stages:
    plan      choose the sample and price it            (no API calls)
    generate  ask for edit lists, apply and verify them (API)
    submit    score every variant through the study's own named-arm request
    report    flip rates, dose response, and the text-sensitive backtest
"""
from __future__ import annotations

import argparse
import json
import random

import pandas as pd

from _paths import CACHE
from research_llm_contamination import (
    MODEL,
    TEXTS,
    _load_results,
)

OUT = CACHE / "perturbation"
GEN_MODEL = MODEL          # the editor and the scorer are the same model; a
                           # weaker editor would confound "did not flip" with
                           # "the edit was botched"

# Variants. `reverse` is the experiment, `placebo` is its floor, and `mild` is
# the dose rung — a small miss where `reverse` is a large one. A reader's
# confidence should track the dose; a rememberer's output cannot, because it is
# not consuming the input.
VARIANTS = ("reverse", "placebo", "mild", "rescale")

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["find", "replace", "why"],
                "additionalProperties": False,
            },
        },
        "economics_before": {"type": "string"},
        "economics_after": {"type": "string"},
        "feasible": {"type": "boolean"},
    },
    "required": ["edits", "economics_before", "economics_after", "feasible"],
    "additionalProperties": False,
}

EDITOR_SYSTEM = (
    "You edit corporate earnings releases for a controlled experiment. You "
    "return ONLY a list of exact find/replace pairs, never a rewritten "
    "document.\n\n"
    "Rules that make the experiment valid:\n"
    "1. Every `find` string must appear EXACTLY ONCE in the source, verbatim, "
    "including punctuation and spacing. Prefer longer, unambiguous spans.\n"
    "2. Change ONLY what the instruction names. Formatting, tone, section "
    "order, boilerplate and every unrelated figure stay untouched.\n"
    "3. Keep the result internally consistent: if a headline figure changes, "
    "any restatement of it elsewhere — tables, bullet summaries, the CEO "
    "quote, percentage changes computed from it — must change to match. An "
    "internally contradictory release is a broken experiment.\n"
    "4. Keep magnitudes plausible and comparable. Reversing a 12% beat means "
    "roughly a 12% miss, not a collapse.\n"
    "5. Set `feasible` false and return no edits if the release does not "
    "contain what the instruction asks you to change.\n\n"
    "You are not told what happened to the stock and must not speculate about "
    "it. This is a text-editing task only."
)

INSTRUCTION = {
    "reverse": (
        "REVERSE the economics. Any EPS beat becomes a miss of comparable "
        "magnitude and vice versa; revenue likewise; guidance raised becomes "
        "guidance cut and vice versa. Update every restatement of those "
        "figures and the surrounding characterisation so the document reads "
        "as a coherent quarter of the opposite sign."
    ),
    "mild": (
        "SOFTEN the economics by roughly two thirds, preserving the SIGN. A "
        "large beat becomes a slight beat; a large miss becomes a slight "
        "miss; strongly raised guidance becomes marginally raised. Update "
        "every restatement so the document stays coherent. Do not cross zero."
    ),
    "placebo": (
        "Change ONLY economically irrelevant numbers: employee headcount, "
        "shares outstanding where it appears in boilerplate, conference-call "
        "dial-in numbers and PINs, webcast replay dates, the investor-"
        "relations phone number, street addresses. Do NOT touch revenue, "
        "EPS, margins, guidance, segment results, cash flow, or any figure a "
        "reader would use to judge the quarter."
    ),
    # THE ARM THE STUDY'S OWN SCRUB SHOULD HAVE BEEN. Section 5.7 removed
    # names and dates and the model still identified the issuer with HIGH
    # confidence on 436 of 437 releases, because it never touched the
    # magnitudes — and $94.9bn of quarterly revenue is an identity as surely
    # as a ticker is. Consistent rescaling destroys that fingerprint while
    # preserving every quantity a judgement actually rests on: margins,
    # growth rates, beat size, segment mix, the ratio of guidance to the
    # quarter just reported are all invariant under a single multiplier.
    "rescale": (
        "ANONYMISE this release so its issuer cannot be identified, while "
        "preserving everything a reader needs to judge the quarter.\n"
        "1. RESCALE every currency amount in the document — revenue, EPS, "
        "segment results, cash, guidance, prior-year comparatives, every one "
        "— by the SAME multiplier {k}. Round naturally. Because the "
        "multiplier is common to all of them, margins, growth rates, beat "
        "sizes and every ratio survive unchanged; only the absolute scale "
        "moves, and the absolute scale is the fingerprint.\n"
        "2. Replace the company name, ticker, brand names, product and "
        "segment names, executive names and headquarters city with neutral "
        "equivalents ('the Company', 'the flagship hardware line', 'the "
        "Chief Executive Officer'). Keep segment structure and relative size "
        "intact.\n"
        "3. Remove or generalise every date, quarter label and fiscal-year "
        "reference.\n"
        "4. Do NOT change unit COUNTS that are not currency (subscribers, "
        "shipments) unless they are distinctive enough to name the issuer, "
        "in which case rescale them by {k} as well.\n"
        "Percentages, margins and growth rates must be left exactly as they "
        "are — rescaling them would destroy the economics rather than the "
        "identity."
    ),
}


def _sample(n: int, seed: int) -> pd.DataFrame:
    """Events with a cached release AND a cached verdict, spread across years.

    Stratified because a flip rate pooled over a decade would be dominated by
    the recent years the panel is thickest in, and corpus depth varies with
    age — which is the very thing under test.
    """
    res = _load_results()
    res = res[(res["arm"] == "named") & (res["effort"] == "medium")]
    res = res[res["direction"].isin(["bullish", "bearish"])]
    have = {p.stem for p in TEXTS.glob("*.txt")}
    res = res[[f"{s}_{str(d)[:10]}" in have
               for s, d in zip(res["symbol"], res["date"])]]
    res["year"] = pd.to_datetime(res["date"]).dt.year
    rng = random.Random(seed)
    years = sorted(res["year"].unique())
    per = max(1, n // len(years))
    picked = []
    for y in years:
        rows = res[res["year"] == y].to_dict("records")
        rng.shuffle(rows)
        picked.extend(rows[:per])
    rng.shuffle(picked)
    return pd.DataFrame(picked[:n])


def apply_edits(text: str, edits: list[dict]) -> tuple[str, list[str]]:
    """Apply find/replace pairs, refusing anything ambiguous.

    A `find` that matches zero times means the generator hallucinated the
    span; more than once means the replacement would hit an unintended site.
    Both are rejected rather than best-effort applied, because a variant built
    on a mis-applied edit is indistinguishable from one built correctly once
    it reaches the scorer.
    """
    out, problems = text, []
    for i, e in enumerate(edits):
        f, r = e.get("find", ""), e.get("replace", "")
        if not f:
            problems.append(f"edit {i}: empty find")
            continue
        c = out.count(f)
        if c != 1:
            problems.append(f"edit {i}: find matched {c}x")
            continue
        out = out.replace(f, r, 1)
    return out, problems


def plan(args) -> dict:
    """Sample and price. No API calls — this is the step that decides whether
    the budget buys enough events to answer the question."""
    n = args.n
    sample = _sample(n, args.seed)
    variants = [v for v in VARIANTS if v != "mild" or args.dose]
    n_dose = args.dose if args.dose else 0
    gen = len(sample) * (len(variants) - (1 if args.dose else 0)) + n_dose
    score = gen
    # Batch price, Opus: $2.50/M in, $12.50/M out. Edit lists run short
    # BECAUSE they are edit lists; a rewritten document would be ~4,900.
    IN_TOK, EDIT_OUT, SCORE_OUT = 3600, 700, 1000
    gen_usd = gen * (IN_TOK * 2.5 + EDIT_OUT * 12.5) / 1e6
    score_usd = score * (IN_TOK * 2.5 + SCORE_OUT * 12.5) / 1e6
    p = 0.5
    se = (p * (1 - p) / len(sample)) ** 0.5
    return {"events": len(sample), "by_year": sample["year"].value_counts().sort_index().to_dict(),
            "variants": variants, "generations": gen, "scorings": score,
            "gen_usd": round(gen_usd, 2), "score_usd": round(score_usd, 2),
            "total_usd": round(gen_usd + score_usd, 2),
            "flip_rate_se_pct": round(100 * se, 1),
            "ci90_halfwidth_pct": round(100 * 1.645 * se, 1)}


def stage_plan(args) -> None:
    p = plan(args)
    print(f"sample: {p['events']} events with both a cached release and a "
          f"cached directional verdict")
    print("  per year:", ", ".join(f"{k}:{v}" for k, v in p["by_year"].items()))
    print(f"  variants: {', '.join(p['variants'])}")
    print("\ncost at batch prices")
    print(f"  {p['generations']:>4} edit-list generations   ${p['gen_usd']:>6.2f}")
    print(f"  {p['scorings']:>4} variant scorings        ${p['score_usd']:>6.2f}")
    print(f"  {'':>4} {'':<24} ${p['total_usd']:>6.2f}")
    print("\npower on the headline flip rate")
    print(f"  binomial SE at p=0.5: {p['flip_rate_se_pct']}%   "
          f"90% CI half-width: ±{p['ci90_halfwidth_pct']}%")
    print(f"  reading and remembering predict ~85% and ~15%; this resolves "
          f"them {'comfortably' if p['ci90_halfwidth_pct'] < 15 else 'marginally'}")


def _key(variant: str, sym: str, day) -> str:
    """custom_id, and the dedupe key. Underscore-delimited like the study's,
    with a variant tag that cannot collide with a real arm."""
    return f"pert{variant[:3]}_{sym}_{str(day).replace('-', '')[:8]}"


def _wait(api, batch_id: str, label: str) -> None:
    import time
    while True:
        b = api.messages.batches.retrieve(batch_id)
        c = b.request_counts
        done = c.succeeded + c.errored + c.canceled + c.expired
        print(f"  {label}: {b.processing_status} {done}/{done + c.processing}",
              flush=True)
        if b.processing_status == "ended":
            return
        time.sleep(30)


def _instruction(variant: str, row) -> str:
    """The edit instruction, with the rescale multiplier bound per event.

    Deterministic in (symbol, date) so a re-run reproduces the same document,
    and drawn across a wide range so no single scale becomes the new
    fingerprint.
    """
    if variant != "rescale":
        return INSTRUCTION[variant]
    rng = random.Random(f"{row['symbol']}_{row['date']}")
    k = round(rng.choice([0.11, 0.17, 0.23, 0.31, 0.43, 2.7, 3.9, 5.3, 7.1, 9.4]), 2)
    return INSTRUCTION["rescale"].replace("{k}", f"x{k}")


def _variants_for(args) -> list[tuple[str, pd.Series]]:
    """(variant, event) pairs to build. `mild` runs on a prefix of the sample
    so the dose rung is a strict subset — the same events carry both rungs,
    which is what makes the comparison paired."""
    sample = _sample(args.n, args.seed)
    jobs = []
    for i, (_, row) in enumerate(sample.iterrows()):
        jobs.append(("reverse", row))
        jobs.append(("placebo", row))
        jobs.append(("rescale", row))
        if args.dose and i < args.dose:
            jobs.append(("mild", row))
    return jobs


def stage_generate(args) -> None:
    """Ask for edit lists, then apply and verify them locally."""
    import anthropic  # noqa: F401  (surfaces a clear ImportError early)
    from research_llm_contamination import client

    OUT.mkdir(parents=True, exist_ok=True)
    jobs = _variants_for(args)
    if args.only:
        jobs = [(v, r) for v, r in jobs if v == args.only]
    reqs = []
    for variant, row in jobs:
        path = TEXTS / f"{row['symbol']}_{str(row['date'])[:10]}.txt"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        reqs.append({
            "custom_id": _key(variant, row["symbol"], row["date"]),
            "params": {
                "model": GEN_MODEL, "max_tokens": 8192,
                "system": EDITOR_SYSTEM,
                "thinking": {"type": "adaptive"},
                "output_config": {"format": {"type": "json_schema",
                                             "schema": EDIT_SCHEMA},
                                  "effort": "medium"},
                "messages": [{"role": "user", "content":
                              f"{_instruction(variant, row)}\n\n<release>\n"
                              f"{text}\n</release>"}],
            },
        })
    print(f"generate: {len(reqs)} edit lists")
    if args.dry_run:
        return
    api = client()
    batch = api.messages.batches.create(requests=reqs)
    (OUT / "gen_batch.json").write_text(json.dumps({"id": batch.id}), "utf-8")
    print(f"  submitted {batch.id}")
    _wait(api, batch.id, "generate")

    kept, rejected = 0, 0
    with (OUT / "variants.jsonl").open("w", encoding="utf-8") as fh:
        for r in api.messages.batches.results(batch.id):
            if r.result.type != "succeeded":
                rejected += 1
                continue
            msg = r.result.message
            body = next((b.text for b in msg.content if b.type == "text"), None)
            try:
                doc = json.loads(body)
            except (TypeError, ValueError):
                rejected += 1
                continue
            # The id carries a 3-letter tag, not the variant name. Storing the
            # tag looked harmless and silently emptied every downstream
            # groupby, because they all key off the full name.
            tag, sym, ymd = r.custom_id.split("_")
            variant = next((v for v in VARIANTS if tag == f"pert{v[:3]}"), tag)
            day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            src = TEXTS / f"{sym}_{day}.txt"
            if not doc.get("feasible") or not doc.get("edits") or not src.exists():
                rejected += 1
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
            edited, problems = apply_edits(text, doc["edits"])
            # A variant with ANY unapplied edit is discarded rather than
            # scored. Half-applied economics is neither the original nor the
            # counterfactual, and it would land in the flip rate as noise.
            if problems or edited == text:
                rejected += 1
                continue
            fh.write(json.dumps({
                "custom_id": r.custom_id, "variant": variant, "symbol": sym,
                "date": day, "n_edits": len(doc["edits"]),
                "chars_changed": sum(abs(len(e["replace"]) - len(e["find"]))
                                     for e in doc["edits"]),
                "economics_before": doc.get("economics_before", ""),
                "economics_after": doc.get("economics_after", ""),
                "text": edited}) + "\n")
            kept += 1
    print(f"  kept {kept}, rejected {rejected} "
          f"(infeasible, unparseable, or an edit that did not apply cleanly)")


def stage_submit(args) -> None:
    """Score every variant through the study's OWN named-arm request."""
    from research_llm_contamination import build_request, client

    rows = [json.loads(x) for x in
            (OUT / "variants.jsonl").read_text(encoding="utf-8").splitlines()]
    reqs = []
    for r in rows:
        arm = "blind" if r["variant"] == "rescale" else "named"
        if arm == "blind":
            # build_request's blind path re-anonymises and would reject text
            # that is already scrubbed, so the request is assembled directly
            # with the same system and schema it would have used.
            from research_llm_contamination import (
                BLIND_SCHEMA, BLIND_SYSTEM, task_blind,
            )
            req = {"custom_id": r["custom_id"], "params": {
                "model": MODEL, "max_tokens": 4096, "system": BLIND_SYSTEM,
                "thinking": {"type": "adaptive"},
                "output_config": {"format": {"type": "json_schema",
                                             "schema": BLIND_SCHEMA},
                                  "effort": "medium"},
                "messages": [{"role": "user", "content":
                              task_blind(None) + "\n\n<data>\n"
                              + r["text"] + "\n</data>"}]}}
        else:
            req = build_request({"symbol": r["symbol"], "date": r["date"],
                                 "run5d": None},
                                "named", r["text"], r["symbol"], "medium")
        if req is None:
            continue
        # Same system, same schema, same task text, same model, same effort —
        # only the release differs. The id carries the variant so the scored
        # verdicts cannot mix with the study's own.
        req["custom_id"] = r["custom_id"]
        reqs.append(req)
    print(f"submit: {len(reqs)} variant scorings")
    if args.dry_run:
        return
    api = client()
    batch = api.messages.batches.create(requests=reqs)
    (OUT / "score_batch.json").write_text(json.dumps({"id": batch.id}), "utf-8")
    print(f"  submitted {batch.id}")
    _wait(api, batch.id, "score")
    n = 0
    with (OUT / "scores.jsonl").open("w", encoding="utf-8") as fh:
        for r in api.messages.batches.results(batch.id):
            if r.result.type != "succeeded":
                continue
            body = next((b.text for b in r.result.message.content
                         if b.type == "text"), None)
            try:
                v = json.loads(body)
            except (TypeError, ValueError):
                continue
            fh.write(json.dumps({"custom_id": r.custom_id, **v}) + "\n")
            n += 1
    print(f"  collected {n} scored variants")


SIDE = {"bullish": 1, "bearish": -1, "neutral": 0}


def _joined() -> pd.DataFrame:
    """Scored variants beside the study's original verdict for the same event."""
    scores = [json.loads(x) for x in
              (OUT / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
    s = pd.DataFrame(scores)
    meta = pd.DataFrame([json.loads(x) for x in
                         (OUT / "variants.jsonl").read_text(encoding="utf-8")
                         .splitlines()])[["custom_id", "variant", "symbol",
                                          "date", "n_edits"]]
    s = s.merge(meta, on="custom_id", how="inner")
    orig = _load_results()
    orig = orig[(orig["arm"] == "named") & (orig["effort"] == "medium")][
        ["symbol", "date", "direction", "confidence", "eps_vs_consensus"]]
    orig["date"] = pd.to_datetime(orig["date"]).dt.strftime("%Y-%m-%d")
    orig = orig.rename(columns={"direction": "orig_direction",
                                "confidence": "orig_confidence",
                                "eps_vs_consensus": "orig_eps"})
    s = s.merge(orig, on=["symbol", "date"], how="inner")
    s["side"] = s["direction"].map(SIDE).fillna(0)
    s["orig_side"] = s["orig_direction"].map(SIDE).fillna(0)
    s["flipped"] = (s["side"] != 0) & (s["orig_side"] != 0) & \
                   (s["side"] != s["orig_side"])
    s["went_neutral"] = (s["side"] == 0) & (s["orig_side"] != 0)
    return s


def _wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (100 * (c - h), 100 * (c + h))


def stage_report(args) -> None:
    s = _joined()
    print("=" * 78)
    print("COUNTERFACTUAL PERTURBATION — does the verdict follow the text?")
    print("=" * 78)
    print(f"scored variants: {len(s)} across {s['symbol'].nunique()} issuers, "
          f"{s['date'].str[:4].nunique()} years\n")
    print(f"  {'variant':<10}{'n':>5}{'flipped':>9}{'rate':>8}"
          f"{'90% CI':>16}{'->neutral':>11}")
    rates = {}
    for v in VARIANTS:
        k = s[s["variant"] == v]
        if not len(k):
            continue
        f, n = int(k["flipped"].sum()), len(k)
        lo, hi = _wilson(f, n)
        rates[v] = f / n
        print(f"  {v:<10}{n:>5}{f:>9}{100*f/n:>7.1f}%"
              f"{f'[{lo:.1f}, {hi:.1f}]':>16}{100*k['went_neutral'].mean():>10.1f}%")

    if "reverse" in rates and "placebo" in rates:
        a = s[s["variant"] == "reverse"]["flipped"]
        b = s[s["variant"] == "placebo"]["flipped"]
        d = a.mean() - b.mean()
        se = (a.var(ddof=1)/len(a) + b.var(ddof=1)/len(b)) ** 0.5
        print(f"\n  READING SHARE = reverse - placebo = {100*d:.1f}pp "
              f"(se {100*se:.1f}, z = {d/se:.2f})")
        print("  The placebo is the floor: a model keying on surface noise "
              "would flip on both.")

    if "mild" in rates:
        print(f"\n  DOSE RESPONSE  placebo {100*rates.get('placebo', 0):.1f}% "
              f"< mild {100*rates['mild']:.1f}% < reverse "
              f"{100*rates['reverse']:.1f}%")
        ok = rates.get("placebo", 0) < rates["mild"] < rates["reverse"]
        print(f"  monotone in dose: {ok} — a rememberer's output cannot track "
              "a dose it is not consuming")

    print("\n-- confidence, by variant " + "-" * 45)
    for v in VARIANTS:
        k = s[s["variant"] == v]
        if not len(k):
            continue
        share = (k["confidence"] == "high").mean()
        print(f"  {v:<10} high-confidence {100*share:>5.1f}%  "
              f"(original was {100*(k['orig_confidence']=='high').mean():.1f}%)")

    # THE PART THAT CONNECTS TO THE MONEY.
    try:
        sens = s[s["variant"] == "reverse"].set_index(["symbol", "date"])["flipped"]
        _text_sensitive_backtest(sens)
    except Exception as exc:                      # noqa: BLE001
        print(f"\n  (text-sensitive backtest unavailable: "
              f"{type(exc).__name__}: {exc})")
    print("=" * 78)


def _text_sensitive_backtest(sens: pd.Series) -> None:
    """Re-measure the gated edge on the events that demonstrably READ.

    If the edge survives where the model provably responded to the document,
    memorisation is not what is producing it — the subset is conditioned on
    reading. If instead it concentrates where the verdict ignored the text,
    that is the bad answer, and it is equally worth knowing.
    """
    import numpy as np
    from research_holding_period import paths_file, resolve, scored_events

    p = pd.read_parquet(paths_file("v2"))
    meta = scored_events("medium", False, "v2")[
        ["symbol", "date", "move_pct", "gated"]]
    m = p.drop(columns=["gated"], errors="ignore").merge(
        meta, on=["symbol", "date"], how="inner")
    m["date"] = pd.to_datetime(m["date"]).dt.strftime("%Y-%m-%d")
    ret, _ = resolve(m.assign(side=np.sign(m["move_pct"].to_numpy())),
                     np.ones(len(m), int), None, None)
    m["bp"] = ret * 1e4
    key = list(zip(m["symbol"], m["date"]))
    m["sensitive"] = [sens.get(k, None) for k in key]
    k = m[m["sensitive"].notna()]

    def spread(sub):
        g = sub[sub["gated"]]["bp"].to_numpy()
        r = sub[~sub["gated"]]["bp"].to_numpy()
        if len(g) < 8 or len(r) < 8:
            return None
        sp = g.mean() - r.mean()
        se = (g.var(ddof=1) / len(g) + r.var(ddof=1) / len(r)) ** 0.5
        return sp, se, g.mean(), r.mean(), len(g), len(r)

    print("\n-- WHAT IT IS WORTH IN BASIS POINTS " + "-" * 38)
    print(f"  {'subset':<33}{'n':>5}{'gated':>9}{'refused':>9}"
          f"{'spread':>9}{'se':>7}{'t':>6}")
    rows = [("whole panel, for reference", m),
            ("the perturbation sample", k),
            ("  of it: verdict FOLLOWED text", k[k["sensitive"].astype(bool)]),
            ("  of it: verdict IGNORED text", k[~k["sensitive"].astype(bool)])]
    got = {}
    for lab, sub in rows:
        res = spread(sub)
        if res is None:
            print(f"  {lab:<33}{len(sub):>5}   too few to report")
            continue
        sp, se, gm, rm, ng, nr = res
        got[lab.strip()] = (sp, se)
        print(f"  {lab:<33}{ng + nr:>5}{gm:>9.1f}{rm:>9.1f}"
              f"{sp:>9.1f}{se:>7.1f}{sp / se:>6.2f}")

    a = got.get("of it: verdict FOLLOWED text")
    b = got.get("of it: verdict IGNORED text")
    if a and b:
        d = a[0] - b[0]
        sed = (a[1] ** 2 + b[1] ** 2) ** 0.5
        print(f"\n  reading premium: {d:+.1f}bp (se {sed:.1f}, z = {d / sed:+.2f})")
        print("  A positive premium means the edge is LARGER where the verdict")
        print("  provably followed the document, which is the reverse of what a")
        print("  memorisation story predicts.")
    if a:
        print("\n  CARRY AWAY: conditioned on demonstrated reading, the gate earns")
        print(f"  {a[0]:+.0f}bp of selection spread, se {a[1]:.0f}. The study's")
        print("  headline is +278.9bp on the full 1,787-event panel; this rests on")
        print("  the perturbation sample alone, so its interval is far wider and")
        print("  the two are not a like-for-like comparison.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["plan", "generate", "submit", "report"])
    ap.add_argument("--n", type=int, default=240)
    ap.add_argument("--dose", type=int, default=120,
                    help="how many of the sample also get the mild rung")
    ap.add_argument("--seed", type=int, default=20260807)
    ap.add_argument("--budget", type=float, default=25.0)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--only", default="", help="restrict to one variant")
    a = ap.parse_args()
    {"plan": stage_plan, "generate": stage_generate,
     "submit": stage_submit, "report": stage_report}[a.stage](a)


if __name__ == "__main__":
    main()
