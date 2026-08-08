# Can it read the table? — qwen36 — RUN VOID, NOT A RESULT

**This file previously held a FAIL verdict with `nan` scores over n=0. That was
an instrument bug, not a measurement, and it is recorded here rather than
deleted because a silently-vanishing failed run is how a study loses track of
what it actually tested.**

What happened, 2026-08-07: the first probe of Qwen3.6-27B answered 0 of 150
charts and the report duly computed a mean over an empty set, printed `nan%`,
compared it to the pre-registered 85% bar, and declared FAIL. Every part of
that pipeline behaved as written. The conclusion was still wrong.

The cause is that **Qwen3.6-27B is a thinking model** and the probe capped
`max_tokens` at 256, a value chosen for the non-thinking models that came
before it. Measured directly: the model spends 223 completion tokens on
`reply with the JSON object {"n": 7} and nothing else`. On a real 48-row chart
the reasoning ran past the cap before it reached the JSON, so every generation
came back truncated and unparseable, and every request was dropped.

Two fixes, both in the harness now:

  - `max_tokens` follows the model's own `thinks` flag (4096 rather than 256).
  - Slots are few and deep instead of many and shallow: 2 x 7168 rather than
    4 x 3584, the same 14336 total the card was measured to hold at 22.0GB,
    so one request holds ~2k of prompt, ~4k of thinking and the answer.

The reporting itself also earned a fix: a scorer that divides by zero should
refuse to render a verdict, not render one made of `nan`. A pass bar that
cannot distinguish "scored badly" from "produced no data" is not a gate.

**Qwen3.6-27B has not been fairly measured yet.** Re-run:

```
python scripts/research_chart_gate.py serve --model qwen36
python scripts/research_chart_probe.py --model qwen36 --n 150 --slots 2
```

This file is overwritten by that run.
