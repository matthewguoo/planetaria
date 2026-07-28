import type { Designer } from "../../lib/useDesigner";

function Row({
  label,
  value,
  cls,
  title,
}: {
  label: string;
  value: string;
  cls?: string;
  title?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2" title={title}>
      <span className="shrink-0 text-[11px] text-bb-muted">{label}</span>
      <span data-numeric className={"truncate text-[12px] " + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);

export function ProbabilityPanel({ designer }: { designer: Designer }) {
  const p = designer.probabilities;
  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">PROBABILITY</div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
        {p ? (
          <>
            <Row
              label="P(PROFIT @EXP)"
              value={pct(p.pProfitExpiry)}
              cls={p.pProfitExpiry >= 0.5 ? "text-bb-profit" : "text-white"}
              title="Risk-neutral probability terminal price lands in a profitable region"
            />
            <Row
              label="P(TOUCH TP)"
              value={pct(p.pTouchTp)}
              cls="text-bb-profit"
              title={`First-passage w/ drift; TP barrier ≈ ${p.tpBarrier?.toFixed(2) ?? "—"} (evaluated at half-horizon)`}
            />
            <Row
              label="P(TOUCH SL)"
              value={pct(p.pTouchSl)}
              cls="text-bb-loss"
              title={`First-passage w/ drift; SL barrier ≈ ${p.slBarrier?.toFixed(2) ?? "—"} (evaluated at half-horizon)`}
            />
            <Row
              label="EV / CONTRACT"
              value={`${p.evPerContract >= 0 ? "+" : ""}$${p.evPerContract.toFixed(0)}`}
              cls={p.evPerContract >= 0 ? "text-bb-profit" : "text-bb-loss"}
              title="Terminal-distribution EV truncated at TP/SL. Short-dated long premium usually carries negative EV — the variance risk premium is real."
            />
            <Row
              label="R : R"
              value={p.rr === null ? "—" : `${p.rr.toFixed(2)} : 1`}
              title="From actual TP/SL premium levels"
            />
            <Row
              label="BREAKEVEN"
              value={p.breakevens.map((b) => b.toFixed(2)).join(" / ") || "—"}
            />
            <div
              className="mt-auto truncate text-[9px] text-bb-muted"
              title={`IV used: ${(p.sigmaUsed * 100).toFixed(1)}% (entry-weighted, sticky-strike). Model: risk-neutral GBM. Feed: indicative quotes.`}
            >
              σ={(p.sigmaUsed * 100).toFixed(1)}% · model assumptions ⓘ
            </div>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-[11px] text-bb-muted">
            no strategy
          </div>
        )}
      </div>
    </div>
  );
}
