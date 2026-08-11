"""GPU job #1 — self-supervised pretraining on the minute tape, embeddings
for the event panels. The brief §4 design, first execution.

The doctrine (authoring brief 2b + tonight's baseline): deep capacity
never touches a small labeled panel. The encoder trains on the UNLABELED
44M-bar cache; the 1,800-event wick panel only ever sees a frozen
64-dim embedding through a linear/GBM probe, walk-forward.

Leakage rule, pre-stated: the encoder trains on 2022-2023 tape ONLY, so
probe test years 2024-2026 are strictly future to everything the encoder
has seen. Earlier probe years would be scored by an encoder trained on
their own future — they are excluded from the comparison, full stop.

  pretrain   windows of 60 minutes, stride 15, per (symbol, day) from
             mtape_2022* / mtape_2023*; per-step features [1m log ret,
             minute-volume z (per window), hl range bp, vwap distance
             bp]; target = next-15-minute return (standardized).
             Model: 2-layer causal GRU, hidden 64 (~60k params), MSE,
             AdamW 3e-4, batch 4096, 8 epochs. RTX 3090.
  embed      the wick panel's first-`EMBED_MIN` minutes after the
             reaction minute -> frozen encoder -> 64-d embedding per
             event -> cache/wick_embeddings.parquet (joined by
             symbol+date downstream; the probe lives with the wick
             study, this script never reads labels).

Run (research/.venv-ml python):
    python scripts/research_mtape_pretrain.py pretrain
    python scripts/research_mtape_pretrain.py embed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402

CACHE = STUDY / "cache"
WICK_F = ROOT / "research" / "pead-llm-gate" / "cache" / "wick_minutes.parquet"
ENC_F = CACHE / "mtape_encoder.pt"
EMB_F = CACHE / "wick_embeddings.parquet"

WIN = 60
STRIDE = 15
FWD = 15
EMBED_MIN = 75
HIDDEN = 64
EPOCHS = 8
BATCH = 4096
LR = 3e-4
SEED = 20260811
PRETRAIN_GLOBS = ("mtape_2022-*.parquet", "mtape_2023-*.parquet")


def day_features(c: np.ndarray, v: np.ndarray, h: np.ndarray,
                 l: np.ndarray, vw: np.ndarray) -> np.ndarray:  # noqa: E741
    """Per-minute feature matrix [T, 4] from one session's arrays."""
    c = np.where(c > 0, c, np.nan)
    ret = np.zeros(len(c), dtype=np.float32)
    ret[1:] = np.diff(np.log(np.where(np.isfinite(c), c, np.nanmean(c))))
    vz = (v - np.nanmean(v)) / (np.nanstd(v) + 1e-9)
    rng = np.where(np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (c > 0),
                   (h - l) / c * 1e4, 0.0)
    dollars = np.cumsum(np.where(np.isfinite(vw), vw, 0.0) * v)
    shares = np.cumsum(v)
    sess_vwap = np.where(shares > 0, dollars / np.maximum(shares, 1e-9), c)
    vwd = np.where(np.isfinite(c) & (sess_vwap > 0),
                   (c / sess_vwap - 1) * 1e4, 0.0)
    X = np.stack([np.nan_to_num(ret) * 1e4, np.nan_to_num(vz),
                  np.nan_to_num(rng), np.nan_to_num(vwd)], axis=1)
    return np.clip(X, -50.0, 50.0).astype(np.float32)


class Encoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.gru = nn.GRU(4, HIDDEN, num_layers=2, batch_first=True)
        self.head = nn.Linear(HIDDEN, 1)

    def forward(self, x):
        out, _ = self.gru(x)
        z = out[:, -1, :]
        return self.head(z).squeeze(-1), z


def build_pretrain_windows() -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    files = sorted(f for g in PRETRAIN_GLOBS for f in CACHE.glob(g))
    for fi, f in enumerate(files):
        m = pd.read_parquet(f, columns=["symbol", "date", "minute",
                                        "c", "v", "h", "l", "vw"])
        for (_, _), d in m.groupby(["symbol", "date"], sort=False):
            d = d.sort_values("minute")
            mins = d["minute"].to_numpy()
            full = np.full(390, np.nan, dtype=np.float32)
            arrs = {}
            for col in ("c", "v", "h", "l", "vw"):
                a = full.copy()
                a[mins] = d[col].to_numpy(dtype=np.float32)
                arrs[col] = a
            arrs["v"] = np.nan_to_num(arrs["v"])
            X = day_features(arrs["c"], arrs["v"], arrs["h"], arrs["l"],
                             arrs["vw"])
            c = arrs["c"]
            for t0 in range(0, 390 - WIN - FWD, STRIDE):
                c_now, c_fwd = c[t0 + WIN - 1], c[t0 + WIN - 1 + FWD]
                if not (np.isfinite(c_now) and np.isfinite(c_fwd)
                        and c_now > 0):
                    continue
                xs.append(X[t0:t0 + WIN])
                ys.append(np.log(c_fwd / c_now) * 1e4)
        print(f"  {f.name}: windows so far {len(xs):,}", flush=True)
    return np.stack(xs), np.array(ys, dtype=np.float32)


def stage_pretrain(args) -> None:
    torch.manual_seed(SEED)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {dev} ({torch.cuda.get_device_name(0) if dev == 'cuda' else 'no gpu'})")
    X, y = build_pretrain_windows()
    y = (y - y.mean()) / (y.std() + 1e-9)
    print(f"pretraining on {len(X):,} windows from 2022-2023 only")
    model = Encoder().to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=LR)
    n = len(X)
    idx = np.arange(n)
    rng = np.random.default_rng(SEED)
    Xt = torch.from_numpy(X)
    yt = torch.from_numpy(y)
    for ep in range(EPOCHS):
        rng.shuffle(idx)
        tot, steps = 0.0, 0
        for b0 in range(0, n, BATCH):
            bi = idx[b0:b0 + BATCH]
            xb = Xt[bi].to(dev, non_blocking=True)
            yb = yt[bi].to(dev, non_blocking=True)
            pred, _ = model(xb)
            loss = nn.functional.mse_loss(pred, yb)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss)
            steps += 1
        print(f"epoch {ep + 1}/{EPOCHS}: mse {tot / steps:.4f}", flush=True)
    torch.save(model.state_dict(), ENC_F)
    print(f"saved encoder -> {ENC_F}")


def stage_embed(args) -> None:
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = Encoder().to(dev)
    model.load_state_dict(torch.load(ENC_F, map_location=dev))
    model.eval()
    w = pd.read_parquet(WICK_F)
    rows = []
    with torch.no_grad():
        for r in w.itertuples():
            c = np.asarray(r.c, dtype=np.float32)
            v = np.asarray(r.v, dtype=np.float32)
            h = np.asarray(r.h, dtype=np.float32)
            low = np.asarray(r.l, dtype=np.float32)
            react = int(r.react_min) if np.isfinite(r.react_min) else 0
            # window: EMBED_MIN steps from the reaction minute; pad short
            seg = slice(react, react + EMBED_MIN)
            cc, vv = c[seg], v[seg]
            hh, ll = h[seg], low[seg]
            if np.isfinite(cc).sum() < 20:
                continue
            X = day_features(cc, np.nan_to_num(vv), hh, ll,
                             np.where(np.isfinite(cc), cc, np.nan))
            if len(X) < EMBED_MIN:
                X = np.vstack([X, np.zeros((EMBED_MIN - len(X), 4),
                                           dtype=np.float32)])
            _, z = model(torch.from_numpy(X[None]).to(dev))
            rows.append({"symbol": r.symbol, "date": str(r.date),
                         **{f"e{j}": float(z[0, j]) for j in range(HIDDEN)}})
    df = pd.DataFrame(rows)
    df.to_parquet(EMB_F)
    print(f"embedded {len(df)} wick events -> {EMB_F}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["pretrain", "embed"])
    args = ap.parse_args()
    {"pretrain": stage_pretrain, "embed": stage_embed}[args.stage](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
