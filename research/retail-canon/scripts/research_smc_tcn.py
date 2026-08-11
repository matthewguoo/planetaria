"""The canon's last chance: a supervised sequence model on raw zone-touch
windows, RTX 3090.

If SMC zones encode anything the hand features miss, a net reading the
raw 60 minutes BEFORE each touch should beat the GBM on the same
walk-forward. This panel is large enough (~10^5+ touches) that deep
supervision is legitimate — unlike the 1,800-event panels the doctrine
keeps off the GPU. Fresh supervised model; the failed SSL encoder is not
involved.

  windows    60 bars ending AT the touch (past-only), per-step features
             [1m ret, vol z, hl range, vwap dist] (day_features from the
             pretrain module — same preprocessing, zero new choices).
  label      side-signed fwd-30m > 0 (the zones study's target).
  model      GRU 2x64 + linear head (~60k params), AdamW 3e-4, batch
             4096, 6 epochs per fold, walk-forward 2024/25/26 (train <
             test year). One train-label placebo fold per year.
  verdict    pre-declared: the net matters only if it beats the zones
             study's canon+generic GBM pooled AUC by >= 0.01; the canon
             matters only if either model finds net-of-costs value.

Run (research/.venv-ml python): python scripts/research_smc_tcn.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
sys.path.insert(0, str(ROOT / "research" / "minute-tape" / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

from research_mtape_pretrain import day_features  # noqa: E402

CACHE = STUDY / "cache"
NOTES = STUDY / "notes"
MTAPE = ROOT / "research" / "minute-tape" / "cache"
ET = ZoneInfo("America/New_York")
STAMP = datetime.now(ET).strftime("%Y%m%d_%H%M")
TOUCH_F = CACHE / "zone_touches.parquet"
WIN_F = CACHE / "zone_windows.npz"

WIN = 60
HIDDEN = 64
EPOCHS = 6
BATCH = 4096
LR = 3e-4
SEED = 20260811


class Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.gru = nn.GRU(5, HIDDEN, num_layers=2, batch_first=True)
        self.head = nn.Linear(HIDDEN, 1)

    def forward(self, x):
        out, _ = self.gru(x)
        return self.head(out[:, -1, :]).squeeze(-1)


def build_windows() -> None:
    """Extract per-touch past-only windows month by month."""
    df = pd.read_parquet(TOUCH_F)
    df["month"] = df["date"].str[:7]
    Xs = np.zeros((len(df), WIN, 5), dtype=np.float32)
    ok = np.zeros(len(df), dtype=bool)
    for month, g in df.groupby("month"):
        f = MTAPE / f"mtape_{month}.parquet"
        if not f.exists():
            continue
        m = pd.read_parquet(f, columns=["symbol", "date", "minute",
                                        "c", "v", "h", "l", "vw"])
        m = m[(m["minute"] >= 0) & (m["minute"] < 390)]
        for (sym, day), gg in g.groupby(["symbol", "date"]):
            d = m[(m["symbol"] == sym) & (m["date"] == day)].sort_values("minute")
            if d.empty:
                continue
            arr = {}
            for col in ("c", "v", "h", "l", "vw"):
                a = np.full(390, np.nan, dtype=np.float32)
                a[d["minute"].to_numpy()] = d[col].to_numpy(dtype=np.float32)
                arr[col] = a
            F = day_features(arr["c"], np.nan_to_num(arr["v"]),
                             arr["h"], arr["l"], arr["vw"])
            for ridx, r in gg.iterrows():
                j = int(r["touch"])
                lo = max(0, j - WIN + 1)
                w = F[lo:j + 1]
                if len(w) < WIN:
                    w = np.vstack([np.zeros((WIN - len(w), 4),
                                            dtype=np.float32), w])
                side_col = np.full((WIN, 1), float(r["side"]),
                                   dtype=np.float32)
                Xs[df.index.get_loc(ridx)] = np.hstack([w, side_col])
                ok[df.index.get_loc(ridx)] = True
        print(f"  {month}: {int(ok.sum()):,} windows", flush=True)
    np.savez_compressed(WIN_F, X=Xs, ok=ok)
    print(f"saved {int(ok.sum()):,} windows -> {WIN_F}")


def main() -> None:
    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if not WIN_F.exists():
        build_windows()
    df = pd.read_parquet(TOUCH_F)
    z = np.load(WIN_F)
    X, ok = z["X"], z["ok"]
    y = ((df["side"] * df["fwd_bp"]) > 0).astype(int).to_numpy()
    yrs = df["date"].str[:4].astype(int).to_numpy()
    m = ok & np.isfinite(df["fwd_bp"].to_numpy())
    X, y, yrs = X[m], y[m], yrs[m]

    lines: list[str] = []

    def emit(t=""):
        print(t, flush=True)
        lines.append(t)

    emit(f"# SMC zones: supervised GRU on raw windows — {STAMP}")
    emit()
    emit(f"{len(y):,} touch windows, device {dev}. Walk-forward; one "
         "train-label placebo per year.")
    emit()
    emit("| fold | n test | AUC | placebo AUC |")
    emit("|---|---|---|---|")

    def train_eval(tr, te, shuffle=False):
        ytr = y[tr].copy()
        if shuffle:
            rng.shuffle(ytr)
        net = Net().to(dev)
        opt = torch.optim.AdamW(net.parameters(), lr=LR)
        Xtr = torch.from_numpy(X[tr])
        Ttr = torch.from_numpy(ytr.astype(np.float32))
        n = len(ytr)
        idx = np.arange(n)
        for _ in range(EPOCHS):
            rng.shuffle(idx)
            for b0 in range(0, n, BATCH):
                bi = idx[b0:b0 + BATCH]
                xb = Xtr[bi].to(dev)
                yb = Ttr[bi].to(dev)
                loss = nn.functional.binary_cross_entropy_with_logits(
                    net(xb), yb)
                opt.zero_grad()
                loss.backward()
                opt.step()
        net.eval()
        preds = []
        with torch.no_grad():
            Xte = torch.from_numpy(X[te])
            for b0 in range(0, len(Xte), BATCH):
                preds.append(torch.sigmoid(
                    net(Xte[b0:b0 + BATCH].to(dev))).cpu().numpy())
        return np.concatenate(preds)

    pooled_p, pooled_y = [], []
    for t in (2024, 2025, 2026):
        tr, te = yrs < t, yrs == t
        if te.sum() == 0 or tr.sum() < 5000:
            continue
        pr = train_eval(tr, te)
        prp = train_eval(tr, te, shuffle=True)
        auc = roc_auc_score(y[te], pr)
        aucp = roc_auc_score(y[te], prp)
        pooled_p.append(pr)
        pooled_y.append(y[te])
        emit(f"| {t} | {int(te.sum()):,} | {auc:.3f} | {aucp:.3f} |")
    pooled = roc_auc_score(np.concatenate(pooled_y),
                           np.concatenate(pooled_p))
    emit(f"| pooled | {sum(len(a) for a in pooled_y):,} | **{pooled:.3f}** | |")
    emit()
    emit("Verdict rule (docstring): beats the zones-study GBM by >= 0.01 "
         "pooled or the raw-window channel is closed too.")

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(STUDY)).stdout.strip() or "?"
    except Exception:
        rev = "?"
    lines.append("")
    lines.append(f"_Provenance: `research_smc_tcn.py` at {rev}, "
                 f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}_")
    NOTES.mkdir(parents=True, exist_ok=True)
    out = NOTES / f"smc_tcn_{STAMP}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
