"""The positive control for the forced memory probe.

WHY THIS EXISTS. `notextf` (research_local_gate.py) forces a model to call up
or down from ticker and date alone, with no abstention. Phi-4 scored 48.6% on
it and Qwen3 50.1% — both indistinguishable from chance, which I read as
"neither open model can recall these events."

That reading is only valid if the probe can detect recall WHEN IT IS THERE,
and nothing has ever shown that. Both models tested were expected to be
clean-ish; a probe that always returns 50% would have produced identical
output. The instrument is unvalidated and it is load-bearing.

Opus 5 is the positive control because its recall is independently
established: on the free-response `notext` arm it volunteered a direction on
41.4% of events and was 66.3% right on those (z = +4.31). If forcing simply
adds guessing on the events it would have declined, the predicted score is

    0.414 x 0.663 + 0.586 x 0.50 = 56.7%

against 50% at n=1,815, which is roughly z = 6. So this test has a specific
number to hit, not merely "above chance."

  - lands near 57%  -> the probe works, and the open models' 50% means clean
  - lands at 50%    -> the probe is blind, and every cleanliness claim built
                       on it today has to be withdrawn

CONFIGURATION MATCHES THE LOCAL RUNS, deliberately: sampled (temperature 1.0)
and NO extended thinking. The local models did not think, and greedy decoding
collapsed them onto a constant, which is why the local arm is sampled. Giving
Opus a scratchpad here would test a different instrument than the one whose
validity is in question — it could reason its way to an answer rather than
recall one, and the null result being validated came from models that had no
such option.
"""

from __future__ import annotations

import argparse
import json
import sys


from research_local_gate import NOTEXTF_SCHEMA
from research_llm_contamination import (
    MODEL,
    OUT,
    PRICE_IN,
    PRICE_OUT,
    _completed_ids,
    client,
    task_notext,
    universe_for,
)


def build(limit: int | None = None) -> list[dict]:
    from research_llm_contamination import HARDENED_SYSTEM

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
    u = universe_for(argparse.Namespace(panel="v2", windows="ten"))
    u["date"] = u["date"].astype(str)
    done = _completed_ids()
    reqs = []
    for _, row in u.iterrows():
        ymd = str(row["date"]).replace("-", "")
        # FOUR fields: request_key's back-compat rule says the study model
        # keeps the short form, and _load_results maps a four-field id to
        # MODEL. A five-field id here would be attributed to a tag that does
        # not exist.
        key = f"notextf_medium_{row['symbol']}_{ymd}"
        if key in done:
            continue
        reqs.append({
            "custom_id": key,
            "params": {
                "model": MODEL,
                "max_tokens": 1024,
                "system": system,
                "temperature": 1.0,
                "thinking": {"type": "disabled"},
                "output_config": {"format": {"type": "json_schema",
                                             "schema": NOTEXTF_SCHEMA}},
                "messages": [{"role": "user",
                              "content": task_notext(row["symbol"],
                                                     str(row["date"]))}],
            },
        })
        if limit and len(reqs) >= limit:
            break
    return reqs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["submit", "collect"])
    ap.add_argument("--batch", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--budget", type=float, default=12.0)
    args = ap.parse_args()
    api = client()

    if args.stage == "submit":
        reqs = build(args.limit)
        if not reqs:
            print("nothing to submit — every id is already on disk")
            return
        # Measured shapes: the probe prompt is tiny (system ~250 tok, task
        # ~60) and the verdict is a short JSON object.
        est = len(reqs) * (310 / 1e6 * PRICE_IN + 200 / 1e6 * PRICE_OUT)
        print(f"{len(reqs)} requests, estimated ${est:.2f}")
        if est > args.budget:
            raise SystemExit(f"refusing: ${est:.2f} exceeds --budget {args.budget}")
        batch = api.messages.batches.create(requests=reqs)
        print(f"submitted {batch.id}")
        with (OUT / "batches.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": batch.id, "arm": "notextf",
                                 "effort": "medium"}) + "\n")
        return

    bid = args.batch
    if not bid:
        for line in (OUT / "batches.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d.get("arm") == "notextf":
                bid = d["id"]
    b = api.messages.batches.retrieve(bid)
    print(f"{bid}: {b.processing_status} {b.request_counts}")
    if b.processing_status != "ended":
        return
    path = OUT / "results_notextf.jsonl"
    seen = _completed_ids()
    n = usd = 0
    with path.open("a", encoding="utf-8") as fh:
        for res in api.messages.batches.results(bid):
            if res.result.type != "succeeded" or res.custom_id in seen:
                continue
            msg = res.result.message
            text = next((x.text for x in msg.content if x.type == "text"), None)
            try:
                verdict = json.loads(text)
            except (TypeError, ValueError):
                continue
            u_ = msg.usage
            usd += u_.input_tokens / 1e6 * PRICE_IN + u_.output_tokens / 1e6 * PRICE_OUT
            fh.write(json.dumps({"custom_id": res.custom_id, "arm": "notextf",
                                 "effort": "medium", "verdict": verdict,
                                 "in": u_.input_tokens, "out": u_.output_tokens}) + "\n")
            n += 1
    print(f"wrote {n} verdicts to {path.name}; measured spend ${usd:.2f}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
