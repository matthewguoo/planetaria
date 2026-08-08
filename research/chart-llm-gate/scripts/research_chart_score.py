"""Did reading the chart do anything?

THE STATISTIC. For each strategy the gate KEEPS a trade when the model's
directional read agrees with the flagged side and VETOES it otherwise, and
the headline is the SPREAD — mean net bp on kept trades minus mean net bp on
vetoed ones. A spread of zero means the model's opinion is uncorrelated with
what happened next. It is the same statistic `research/pead-llm-gate` reports,
deliberately, so the two studies can be read against each other.

THREE NULLS, BECAUSE A RAW SPREAD PROVES NOTHING ON ITS OWN.

  permutation      Shuffle the verdict labels within (strategy, month) and
                   recompute. This is the null for "the model has no skill but
                   some months and some strategies pay better than others",
                   which a raw t-test on pooled trades would happily mistake
                   for skill.

  shuffled arm     The model's verdict on a DIFFERENT chart, scored against
                   this trade's outcome. Empirical rather than synthetic: it
                   runs the whole pipeline end to end with the one causal link
                   cut. If the spread survives here it was never about the
                   chart, and every other number in this file is void.

  feature-matched  A logistic regression on six numbers any chart-reader can
                   see — trailing return, realised vol, position in the day's
                   range, relative volume, minutes since the open, distance
                   from VWAP — fit strictly walk-forward, then thresholded to
                   keep the SAME fraction of trades the LLM kept. This is the
                   null nobody wants to run, and it is the one that decides
                   what the result means. If a six-feature logistic gates as
                   well as a 14B language model, then "the model reads charts"
                   is not the right description of what happened; "the model
                   computes momentum, slowly" is.

Run:
    python scripts/research_chart_score.py            # writes notes/gate_<date>.md
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from _paths import NOTES, VERDICTS

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from research_chart_data import COST_BP
from research_chart_gate import COLD_HORIZON, COLD_PANEL, GATE_PANEL
from research_chart_setups import STRATEGIES

N_PERM = 2000
FEATURES = ["f_ret20_bp", "f_rvol_bp", "f_pos_in_day", "f_vol_ratio",
            "f_min", "f_vwap_dist_bp", "r_bp"]


# -------------------------------------------------------------------- load

def load(arm: str, model: str) -> pd.DataFrame:
    p = VERDICTS / f"results_{arm}_{model}.jsonl"
    if not p.exists():
        return pd.DataFrame()
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:                                    # noqa: BLE001
            continue
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    # Dedupe on identity, loudly. A silent double-count is exactly the failure
    # `_RunLock` exists to prevent, and belt-and-braces means checking here too.
    before = len(d)
    d = d.drop_duplicates(subset=["id"], keep="first")
    if before != len(d):
        print(f"WARNING: {arm}/{model}: dropped {before - len(d)} duplicate "
              f"verdict rows — a writer raced; check for orphan processes")
    return d


def gate_panel(arm: str, model: str) -> pd.DataFrame:
    """Verdicts merged onto the setup panel, with the keep flag applied.

    TWO KEEP RULES, because the two question forms are not interchangeable.
    A `direction` verdict keeps when the read agrees with the flagged side; a
    `resolves` verdict keeps when the model expects the target before the
    stop. The second exists because the first came back at a 97% keep rate —
    see the `outcome` arm's note in `research_chart_gate.py`.
    """
    v = load(arm, model)
    if v.empty:
        return v
    panel = pd.read_parquet(GATE_PANEL)
    m = v.merge(panel, left_on="id", right_on="setup_id", how="inner")
    if "resolves" in m.columns:
        m["keep"] = m["resolves"] == "target"
        m["bull"] = m["side"] > 0          # the arm never re-judges direction
    else:
        m["bull"] = m["direction"] == "bullish"
        m["keep"] = np.where(m["side"] > 0, m["bull"],
                             m["direction"] == "bearish")
    return m


# -------------------------------------------------------------------- null

def spread(g: pd.DataFrame) -> float:
    k, v = g.loc[g["keep"], "net_bp"], g.loc[~g["keep"], "net_bp"]
    if not len(k) or not len(v):
        return np.nan
    return float(k.mean() - v.mean())


def permutation_p(g: pd.DataFrame, rng: np.random.Generator,
                  n: int = N_PERM) -> tuple[float, float]:
    """p(spread >= observed) under within-(strategy, month) label shuffles.

    Shuffling WITHIN blocks rather than globally is the whole point: a model
    that never learns anything but happens to say 'bullish' more often in a
    month that trended up would show a global-shuffle p-value well under 0.05
    on pure calendar luck.
    """
    obs = spread(g)
    if np.isnan(obs):
        return np.nan, np.nan
    g = g.copy()
    g["_blk"] = g["strategy"] + g["date"].str.slice(0, 7)
    blocks = [idx.to_numpy() for _, idx in g.groupby("_blk").groups.items()]
    keep = g["keep"].to_numpy()
    net = g["net_bp"].to_numpy()
    pos = {v: i for i, v in enumerate(g.index)}
    blocks = [np.array([pos[i] for i in b]) for b in blocks]
    draws = np.empty(n)
    for t in range(n):
        k = keep.copy()
        for b in blocks:
            k[b] = rng.permutation(k[b])
        draws[t] = net[k].mean() - net[~k].mean() if 0 < k.sum() < len(k) else 0
    return float((draws >= obs).mean()), float(draws.std())


# ------------------------------------------------------- feature-matched null

def _fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 1.0) -> np.ndarray:
    """L2-regularised logistic regression by direct minimisation.

    Hand-rolled because this venv has neither sklearn nor statsmodels, and
    adding a dependency to a research tree to fit seven coefficients is a
    worse trade than twelve lines of scipy.
    """
    X = np.column_stack([np.ones(len(X)), X])

    def nll(w):
        z = X @ w
        # log(1+exp(z)) computed stably.
        ll = np.sum(y * z - np.logaddexp(0, z))
        return -ll + l2 * np.sum(w[1:] ** 2)

    def grad(w):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = -X.T @ (y - p)
        g[1:] += 2 * l2 * w[1:]
        return g

    r = minimize(nll, np.zeros(X.shape[1]), jac=grad, method="L-BFGS-B")
    return r.x


def feature_gate(g: pd.DataFrame, keep_rate: float) -> pd.DataFrame:
    """Walk-forward P(win) from cheap features, thresholded to `keep_rate`.

    STRICTLY OUT OF SAMPLE. For each month, the model is fit on every EARLIER
    month only, and standardisation uses training-set statistics alone. An
    in-sample version of this null would beat any LLM and would prove nothing;
    the whole value of the comparison is that both sides are forecasting.

    The threshold is set on the training distribution too — using the test
    month's own quantile would leak the month's difficulty into the decision.
    """
    g = g.sort_values("date").copy()
    g["_m"] = g["date"].str.slice(0, 7)
    months = sorted(g["_m"].unique())
    strat = pd.get_dummies(g["strategy"], prefix="s").astype(float)
    base = np.column_stack([g[FEATURES].to_numpy(float),
                            g["side"].to_numpy(float),
                            strat.to_numpy()])
    y = (g["net_bp"] > 0).to_numpy().astype(float)
    out = np.full(len(g), np.nan)
    # Three months of burn-in: a logistic on 10 columns fit to one month of a
    # single strategy is noise wearing a coefficient's clothes.
    for k in range(3, len(months)):
        tr = g["_m"].isin(months[:k]).to_numpy()
        te = (g["_m"] == months[k]).to_numpy()
        if tr.sum() < 100 or te.sum() == 0:
            continue
        mu, sd = base[tr].mean(0), base[tr].std(0)
        sd[sd == 0] = 1
        w = _fit_logit((base[tr] - mu) / sd, y[tr])
        z = np.column_stack([np.ones(te.sum()), (base[te] - mu) / sd]) @ w
        p_te = 1 / (1 + np.exp(-z))
        z_tr = np.column_stack([np.ones(tr.sum()), (base[tr] - mu) / sd]) @ w
        thr = np.quantile(1 / (1 + np.exp(-z_tr)), 1 - keep_rate)
        out[te] = (p_te >= thr).astype(float)
    g["keep"] = out
    return g[~np.isnan(out)].assign(keep=lambda d: d["keep"].astype(bool))


# ------------------------------------------------------------------- report

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="phi4,qwen3")
    args = ap.parse_args()

    rng = np.random.default_rng(20260807)
    lines: list[str] = []

    def emit(t: str = "") -> None:
        print(t)
        lines.append(t)

    models = [m for m in args.models.split(",")
              if not gate_panel("gate", m).empty]
    if not models:
        raise SystemExit("no gate verdicts on disk")

    emit(f"# Reading anonymised charts — {date.today()}")
    emit("")
    emit("Charts carry no ticker, no date and no price level; every bar is a "
         "percentage of the decision bar's close. There is no outcome-paired "
         "text for these windows in any corpus, so unlike the PEAD study "
         "there is nothing here for a model to recall. See "
         "`research_chart_render.py` for the full statement of that claim and "
         "its measured limit.")
    emit("")
    emit(f"Costs {COST_BP:.0f}bp round trip. `spread` = mean net bp on trades "
         f"the gate KEPT minus mean net bp on trades it VETOED.")
    emit("")

    # ------------------------------------------------------------ the gate
    ARM_NOTE = {
        "gate": ("The model reads the chart and calls it bullish or bearish; "
                 "the gate keeps the trade when that agrees with the flagged "
                 "side. Watch the `keeps` column before reading anything "
                 "else — a keep rate near 100% means the model is agreeing "
                 "with whatever it was handed, and the spread then has almost "
                 "no vetoed trades under it."),
        "outcome": ("The same charts and the same brackets, but the model is "
                    "asked which barrier price reaches FIRST. The setup's "
                    "direction does not telegraph this answer, so unlike "
                    "`gate` it can actually discriminate. This is the arm the "
                    "gate result rests on."),
    }
    for model in models:
        for arm in ("gate", "outcome"):
            g = gate_panel(arm, model)
            if len(g) < 50:
                continue
            emit(f"## `{model}` / `{arm}` — gate spread by strategy")
            emit("")
            emit(ARM_NOTE[arm])
            emit("")
            emit("| strategy | n | keeps | ungated | kept | vetoed | spread | "
                 "perm p | tape acc |")
            emit("|---|---|---|---|---|---|---|---|---|")
            for name in [*STRATEGIES, "ALL"]:
                sub = g if name == "ALL" else g[g["strategy"] == name]
                if len(sub) < 20:
                    continue
                sp = spread(sub)
                p, _ = permutation_p(sub.reset_index(drop=True), rng)
                acc = float((sub["bull"] == (sub["gross_bp"] * sub["side"] > 0)
                             ).mean())
                k = sub["keep"]
                kept = sub.loc[k, "net_bp"].mean() if k.any() else np.nan
                veto = sub.loc[~k, "net_bp"].mean() if (~k).any() else np.nan
                emit(f"| {'**ALL**' if name == 'ALL' else name} | "
                     f"{len(sub):,} | {k.mean() * 100:.0f}% | "
                     f"{sub['net_bp'].mean():+.1f} | {kept:+.1f} | "
                     f"{veto:+.1f} | {sp:+.1f} | {p:.3f} | {acc * 100:.1f}% |")
            emit("")
            emit(f"`perm p` is p(spread >= observed) over {N_PERM} shuffles "
                 f"of the verdict labels within (strategy, month). "
                 f"`tape acc` is how often the flagged side matched the sign "
                 f"of the move, which is a property of the SCREEN and is "
                 f"identical across arms.")
            emit("")

            # The outcome arm has a ground truth the direction arm does not:
            # the trade really did resolve at one barrier or the other on the
            # subset that resolved at all. Scoring that directly is better
            # powered than the bp spread, because one bracket is worth
            # hundreds of bp and the variance swamps the mean.
            if arm == "outcome":
                res = g[g["exit_reason"].isin(["target", "stop"])]
                if len(res) > 30:
                    said = res["resolves"] == "target"
                    truth = res["exit_reason"] == "target"
                    base = float(truth.mean())
                    hit = float((said == truth).mean())
                    se = np.sqrt(0.25 / len(res))
                    emit(f"On the {len(res):,} trades that actually resolved "
                         f"at a barrier (the rest timed out or closed with "
                         f"the session), {base * 100:.1f}% hit the target. "
                         f"The model said `target` on "
                         f"{said.mean() * 100:.1f}% and was right on "
                         f"{hit * 100:.1f}% of calls "
                         f"(z = {(hit - 0.5) / se:+.2f} against a coin flip). "
                         f"Always saying `stop` would have scored "
                         f"{(1 - base) * 100:.1f}%.")
                    emit("")

        g = gate_panel("gate", model)

        # ------------------------------------------------- the shuffled arm
        sh = gate_panel("shuffled", model)
        if len(sh) > 50:
            emit(f"### `{model}` — shuffled-chart control")
            emit("")
            emit("Same proposal, same outcome, but the model was shown some "
                 "OTHER setup's chart. The causal link is cut; the spread "
                 "must collapse to zero. If it does not, the pipeline is "
                 "leaking and the table above means nothing.")
            emit("")
            emit("| arm | n | keeps | spread | perm p |")
            emit("|---|---|---|---|---|")
            common = set(sh["id"]) & set(g["id"])
            for label, sub in (("gate (paired)", g[g["id"].isin(common)]),
                               ("shuffled", sh[sh["id"].isin(common)])):
                if len(sub) < 20:
                    continue
                p, _ = permutation_p(sub.reset_index(drop=True), rng)
                emit(f"| {label} | {len(sub):,} | "
                     f"{sub['keep'].mean() * 100:.0f}% | "
                     f"{spread(sub):+.1f} | {p:.3f} |")
            emit("")

        # ------------------------------------------- the feature-matched null
        emit(f"### `{model}` — versus six numbers in a spreadsheet")
        emit("")
        emit("A logistic regression on trailing return, realised vol, "
             "position in the day's range, relative volume, minutes since "
             "open and VWAP distance — fit walk-forward, never on the month "
             "it grades — thresholded to keep the same fraction of trades the "
             "model kept.")
        emit("")
        emit("| gate | n | keeps | spread |")
        emit("|---|---|---|---|")
        fg = feature_gate(g, float(g["keep"].mean()))
        emit(f"| {model} | {len(g):,} | {g['keep'].mean() * 100:.0f}% | "
             f"{spread(g):+.1f} |")
        if len(fg) > 50:
            emit(f"| logistic (walk-forward) | {len(fg):,} | "
                 f"{fg['keep'].mean() * 100:.0f}% | {spread(fg):+.1f} |")
        emit("")

    # -------------------------------------------------------- the cold read
    cold = pd.read_parquet(COLD_PANEL) if COLD_PANEL.exists() else pd.DataFrame()
    rows = []
    for model in args.models.split(","):
        v = load("coldread", model)
        if v.empty or cold.empty:
            continue
        m = v.merge(cold, on="id", how="inner")
        up = m["direction"] == "up"
        hit = float(((m["fwd_bp"] > 0) == up).mean())
        n = len(m)
        se = np.sqrt(0.25 / n)
        rows.append((model, n, float(up.mean()), hit, (hit - 0.5) / se, se))
    if rows:
        emit("## Cold read — no setup, forced call, 60 minutes ahead")
        emit("")
        emit("A random window of tape and nothing else. There is no proposal "
             "to agree with and no abstention available, so a model with "
             "nothing to say scores exactly 50%. This, not the gate, is the "
             "direct test of whether reading a chart is worth anything.")
        emit("")
        emit("| model | n | said up | accuracy | z | 95% CI |")
        emit("|---|---|---|---|---|---|")
        for model, n, up, hit, z, se in rows:
            emit(f"| {model} | {n:,} | {up * 100:.0f}% | {hit * 100:.1f}% | "
                 f"{z:+.2f} | [{(hit - 1.96 * se) * 100:.1f}, "
                 f"{(hit + 1.96 * se) * 100:.1f}] |")
        emit("")
        emit(f"Horizon is {COLD_HORIZON} bars ({COLD_HORIZON * 5} minutes). "
             f"An interval covering 50% is a model that cannot read a chart.")
        emit("")

    # -------------------------------------------------------- the noise arm
    emit("## Synthetic control — the same question asked of a random walk")
    emit("")
    emit("Geometric Brownian motion at the universe's own volatility, "
         "rendered through the identical formatter. There is provably nothing "
         "to find. The accuracy is 50% by construction and is not the point; "
         "the CONVICTION DISTRIBUTION is. A model as confident on noise as on "
         "real tape is not deriving its confidence from the tape.")
    emit("")
    emit("| model | arm | n | high conv | medium | low |")
    emit("|---|---|---|---|---|---|")
    for model in args.models.split(","):
        for arm in ("coldread", "synthetic"):
            v = load(arm, model)
            if v.empty:
                continue
            c = v["conviction"].value_counts(normalize=True) * 100
            emit(f"| {model} | {arm} | {len(v):,} | {c.get('high', 0):.0f}% | "
                 f"{c.get('medium', 0):.0f}% | {c.get('low', 0):.0f}% |")
    emit("")

    # ---------------------------------------------- the abstention record
    for model in args.models.split(","):
        v = load("gateopt", model)
        if v.empty:
            continue
        n = len(v)
        neu = (v["direction"] == "neutral").mean() * 100
        emit(f"- `{model}` with an abstention option available: "
             f"{neu:.0f}% neutral on {n} charts. This is why the gate arm "
             f"forces a side — see the schema note in `research_chart_gate.py`.")
    emit("")

    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"gate_{date.today():%Y%m%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
