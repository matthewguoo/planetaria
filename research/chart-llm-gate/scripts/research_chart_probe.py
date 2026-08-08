"""Can it read the table at all? A comprehension floor for the null.

THE INTERPRETIVE PROBLEM THIS SOLVES. Every null in this study has two
explanations that predict the same number:

    A   chart reading carries no forward information
    B   a 14B model cannot parse a 48-row numeric table

The study's plan for separating them is the qwen3 capability control — run a
different, larger model and see whether it does better. That is a good control
and it is still worth running, but it is indirect and it is expensive: it
answers "is another model better at PREDICTING", from which capability is
inferred.

There is a direct measurement available and it costs about four minutes. Ask
the model questions about the chart that have VERIFIABLE ANSWERS computable
from the same window it was shown — the highest high, the close of a
particular bar, how many of the last twelve bars closed up. No forecasting, no
judgement, nothing that depends on the future. Pure reading comprehension of
the exact artefact the reading arms hand it.

    scores well, predicts nothing   ->  it reads fine; world A. The null is
                                        about the market, not the instrument.
    scores badly                    ->  world B. Every other number in this
                                        study is a capability floor and says
                                        nothing about whether charts are
                                        readable, and the honest move is a
                                        bigger model or a different encoding.

The questions are deliberately easy — extraction and counting, not
arithmetic-heavy. A model that cannot do THESE cannot have been evaluating
anything in the reading arms.

Run:
    python scripts/research_chart_probe.py --model phi4 --n 150
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from _paths import BACKEND, NOTES, VERDICTS

sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research_chart_data import bars_path  # noqa: E402
from research_chart_gate import (  # noqa: E402
    LOCAL,
    ENDPOINT,
    payload,
    slot_ctx,
    wait_healthy,
)
from research_chart_render import WINDOW, snapshot  # noqa: E402
from research_chart_setups import SETUPS, TF, prep  # noqa: E402

PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "highest_high": {"type": "number"},
        "lowest_low": {"type": "number"},
        "close_of_bar_minus_10": {"type": "number"},
        "up_closes_last_12": {"type": "integer"},
        "final_close_vs_vwap": {"type": "string", "enum": ["above", "below"]},
    },
    "required": ["highest_high", "lowest_low", "close_of_bar_minus_10",
                 "up_closes_last_12", "final_close_vs_vwap"],
    "additionalProperties": False,
}

SYSTEM = (
    "You read numeric price tables precisely. Every answer is a fact stated "
    "in the table in front of you. Do not estimate, do not round beyond the "
    "table's own two decimal places, and do not reason about what the prices "
    "imply — just read them off."
)

# THE PASS BAR, WRITTEN DOWN BEFORE ANY MODEL'S SCORE WAS SEEN except phi-4's,
# which is what motivated the probe and is recorded here as the failing case.
# Declaring it up front is the whole point: a threshold chosen after seeing
# candidates is a threshold chosen to admit the candidate you liked.
#
#   mean >= 85%      Extraction from a visible table is not a hard task. A
#                    model below this is not reading the chart in the reading
#                    arms either, whatever its verdicts look like.
#   worst >= 60%     A catastrophic failure on any single dimension means the
#                    model is blind to a whole class of chart feature, and the
#                    mean would hide it. phi-4 averaged 64.9% while scoring
#                    11.3% on counting twelve rows — the mean alone made that
#                    look like a merely mediocre reader rather than one that
#                    cannot count at all.
#
# phi-4 Q8_0, 2026-08-07: mean 64.9%, worst 11.3%. FAILS both.
PASS_MEAN = 0.85
PASS_WORST = 0.60


def _questions(df: pd.DataFrame, i: int, anchor: float) -> tuple[str, dict]:
    """The prompt's questions and their exact answers.

    Ground truth is computed from the SAME rounded values the table prints, so
    a correct reader scores 100% and any miss is the model's, not a
    float-formatting artefact.
    """
    lo = max(0, i - WINDOW + 1)
    w = df.iloc[lo:i + 1]
    r = lambda x: round((x / anchor - 1) * 100, 2)          # noqa: E731
    highs = [r(x) for x in w["h"]]
    lows = [r(x) for x in w["l"]]
    closes = [r(x) for x in w["c"]]
    opens = [r(x) for x in w["o"]]
    last12 = list(zip(opens[-12:], closes[-12:]))
    truth = {
        "highest_high": max(highs),
        "lowest_low": min(lows),
        # Bar -10 counting back from the final bar, which is bar 0.
        "close_of_bar_minus_10": closes[-11] if len(closes) >= 11 else closes[0],
        "up_closes_last_12": sum(1 for o, c in last12 if c > o),
        "final_close_vs_vwap": ("above" if float(df["c"].iat[i])
                                > float(df["vwap"].iat[i]) else "below"),
    }
    q = (
        f"session VWAP is {r(float(df['vwap'].iat[i])):+.2f}%.\n\n"
        "Read these five values off the table:\n"
        "  1. the highest HIGH anywhere in the window\n"
        "  2. the lowest LOW anywhere in the window\n"
        "  3. the CLOSE of the bar labelled -10\n"
        "  4. how many of the last 12 bars closed above their own open\n"
        "  5. whether the final close is above or below the session VWAP"
    )
    return q, truth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=list(LOCAL), default="phi4")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--slots", type=int, default=4)
    # Thinking models bill their reasoning against this. 256 was enough for a
    # non-thinking model and dropped 150/150 on Qwen3.6-27B; the default now
    # follows the model's own `thinks` flag.
    ap.add_argument("--max-tokens", type=int, default=None)
    args = ap.parse_args()

    wait_healthy()
    s = pd.read_parquet(SETUPS)
    s = s[s["i"] >= WINDOW].sample(args.n, random_state=20260807)
    frames = {x: prep(pd.read_parquet(bars_path(x)), TF)
              for x in s["symbol"].unique()}

    items = []
    for _, row in s.iterrows():
        df = frames[row["symbol"]]
        i = int(row["i"])
        snap = snapshot(df, i, WINDOW)
        q, truth = _questions(df, i, snap["anchor"])
        items.append({
            "id": row["setup_id"], "truth": truth, "system": SYSTEM,
            "schema": PROBE_SCHEMA, "temp": 0.0,
            "max_tokens": args.max_tokens or
            (4096 if LOCAL[args.model].get("thinks") else 256),
            "task": "\n".join([
                "Below is a price chart. Each row is one 5-minute candle. "
                "All prices are percentages relative to the final bar's "
                "close, so the final close is exactly 0.00. Bars are labelled "
                "in the BAR column, counting back from 0 at the final bar.",
                "", snap["table"], "", q,
            ]),
        })

    slot = slot_ctx(args.model)
    rows = []
    t0 = time.time()

    def one(it):
        p = payload(it["task"], it["system"], it["schema"], it["temp"],
                    it["max_tokens"], slot)
        try:
            r = httpx.post(f"{ENDPOINT}/v1/chat/completions", json=p,
                           timeout=600)
            r.raise_for_status()
            return {"id": it["id"], "truth": it["truth"],
                    "said": json.loads(
                        r.json()["choices"][0]["message"]["content"])}
        except Exception as exc:                             # noqa: BLE001
            print(f"  DROP {it['id']}: {type(exc).__name__} {str(exc)[:60]}")
            return None

    with ThreadPoolExecutor(max_workers=args.slots) as pool:
        for fut in as_completed([pool.submit(one, it) for it in items]):
            r = fut.result()
            if r:
                rows.append(r)
    print(f"{len(rows)}/{len(items)} answered in {(time.time()-t0)/60:.1f}min")

    # Prices are graded to +-0.02%, which is one unit in the table's last
    # printed digit plus one for slack. Counts and the VWAP side are exact.
    fields = ["highest_high", "lowest_low", "close_of_bar_minus_10",
              "up_closes_last_12", "final_close_vs_vwap"]
    score = {f: [] for f in fields}
    for r in rows:
        for f in fields:
            t, sd = r["truth"][f], r["said"].get(f)
            if f == "final_close_vs_vwap":
                score[f].append(sd == t)
            elif f == "up_closes_last_12":
                score[f].append(sd == t)
            else:
                score[f].append(sd is not None and abs(float(sd) - t) <= 0.02)

    lines = [f"# Can it read the table? — {args.model}", "",
             f"{len(rows)} anonymised charts, identical to what the reading "
             f"arms are shown. Every question has a verifiable answer stated "
             f"in the table; none requires forecasting.", "",
             "| question | correct | n |", "|---|---|---|"]
    for f in fields:
        v = np.array(score[f])
        lines.append(f"| {f.replace('_', ' ')} | {v.mean()*100:.1f}% | {len(v)} |")
    per = {f: float(np.mean(score[f])) for f in fields}
    allv, worst = float(np.mean(list(per.values()))), min(per.values())
    ok = allv >= PASS_MEAN and worst >= PASS_WORST
    lines += ["", f"**Mean {allv*100:.1f}%, worst dimension "
              f"{worst*100:.1f}% ({min(per, key=per.get).replace('_', ' ')}).**",
              "",
              f"Pre-registered bar: mean >= {PASS_MEAN*100:.0f}% and every "
              f"dimension >= {PASS_WORST*100:.0f}%. "
              f"**{'PASS' if ok else 'FAIL'}** — this model "
              f"{'may' if ok else 'must NOT'} be used for the reading arms.",
              "",
              "A model that scores well here reads the artefact correctly, "
              "and any null in the reading arms is a fact about the market "
              "rather than about the instrument. A model that scores badly "
              "makes every other number in this study a capability floor.", ""]
    print(f"\n{'PASS' if ok else 'FAIL'}: mean {allv*100:.1f}% "
          f"(bar {PASS_MEAN*100:.0f}%), worst {worst*100:.1f}% "
          f"(bar {PASS_WORST*100:.0f}%)")
    out = "\n".join(lines)
    print("\n" + out)
    NOTES.mkdir(parents=True, exist_ok=True)
    (NOTES / f"comprehension_{args.model}.md").write_text(out, encoding="utf-8")
    VERDICTS.mkdir(parents=True, exist_ok=True)
    with (VERDICTS / f"probe_{args.model}.jsonl").open("w",
                                                       encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
