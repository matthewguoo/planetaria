import { useMemo, useState } from "react";
import type { McResult } from "../../lib/mcSim";
import { useMonteCarlo, type McInputs } from "../../lib/useMonteCarlo";
import type { Designer } from "../../lib/useDesigner";
import { useStrategyStore } from "../../store/strategyStore";

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
const usd = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

export function ProbabilityPanel({ designer }: { designer: Designer }) {
  const p = designer.probabilities;
  const volShift = useStrategyStore((s) => s.volShift);
  const setVolShift = useStrategyStore((s) => s.setVolShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const setSkewBeta = useStrategyStore((s) => s.setSkewBeta);
  const [mc, setMc] = useState<McResult | null>(null);

  const mcInputs: McInputs | null = useMemo(() => {
    if (!designer.ready || !designer.legs) return null;
    return {
      legs: designer.legs,
      entry: designer.entry,
      tpPremium: designer.tpPremium,
      slPremium: designer.slPremium,
      hoursToExpiry: designer.hoursToExpiry,
      timeStopHours: designer.timeStopHours,
      spot: designer.spot,
      smiles: designer.smiles,
      volShift,
      skewBeta,
      frictionPerSet: designer.frictionPerSet,
    };
  }, [designer, volShift, skewBeta]);

  useMonteCarlo(mcInputs, setMc);

  return (
    <div className="panel flex min-w-0 flex-col">
      <div className="panel-title">PROBABILITY · SIMULATION</div>
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
        {p ? (
          <>
            {mc && (
              <>
                <div
                  className="text-[9px] tracking-widest text-bb-muted"
                  title="Monte Carlo over GBM paths in trading time with the EXACT exit rules the enforcer runs (TP limit at threshold, SL at first observed breach incl. gap-through, hard time stop, expiry payoff), net of round-trip bid/ask friction. Scenario vols: smile + IV shock + skew beta."
                >
                  MC · {mc.paths} PATHS · EXITS ENFORCED ⓘ
                </div>
                <Row
                  label="EV / SET (NET)"
                  value={usd(mc.evPerSet)}
                  cls={mc.evPerSet >= 0 ? "text-bb-profit" : "text-bb-loss"}
                  title={`Net of ≈$${designer.frictionPerSet.toFixed(0)} round-trip spread cost`}
                />
                <Row
                  label="WIN RATE"
                  value={pct(mc.winRate)}
                  cls={mc.winRate >= 0.5 ? "text-bb-profit" : "text-white"}
                />
                <Row
                  label="EXIT MIX"
                  value={`TP ${Math.round(mc.pTp * 100)} · SL ${Math.round(mc.pSl * 100)} · T ${Math.round(
                    (mc.pTime + mc.pExpiry) * 100,
                  )}%`}
                  title="Share of paths exiting via take-profit / stop-loss / time-stop-or-expiry"
                />
                <Row
                  label="P5 · P50 · P95"
                  value={`${usd(mc.p5)} · ${usd(mc.p50)} · ${usd(mc.p95)}`}
                  title="Realized P/L percentiles per contract-set"
                />
                <Row
                  label="AVG TIME IN"
                  value={
                    mc.avgMinutesInTrade >= 90
                      ? `${(mc.avgMinutesInTrade / 60).toFixed(1)}h`
                      : `${Math.round(mc.avgMinutesInTrade)}m`
                  }
                />
                <div className="my-0.5 border-t border-bb-border/60" />
              </>
            )}
            <Row
              label="P(PROFIT @EXP)"
              value={pct(p.pProfitExpiry)}
              cls={p.pProfitExpiry >= 0.5 ? "text-bb-profit" : "text-white"}
              title="Risk-neutral probability terminal price lands in a profitable region (analytic)"
            />
            <Row
              label="P(TOUCH TP) / SL"
              value={`${pct(p.pTouchTp)} / ${pct(p.pTouchSl)}`}
              title={`First-passage w/ drift; barriers ≈ ${p.tpBarrier?.toFixed(2) ?? "—"} / ${p.slBarrier?.toFixed(2) ?? "—"} (half-horizon)`}
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
            <div className="mt-auto flex items-center justify-between gap-2 border-t border-bb-border/60 pt-1">
              <label
                className="flex items-center gap-1 text-[10px] text-bb-muted"
                title="Parallel IV shock applied to every scenario vol (surface, contours, MC). Stress vega: what if IV crushes -20% or spikes +20%?"
              >
                IV
                <input
                  data-numeric
                  type="number"
                  step={5}
                  min={-50}
                  max={50}
                  className="w-12 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-amber outline-none focus:border-bb-amber"
                  value={Math.round(volShift * 100)}
                  onChange={(e) => setVolShift(Number(e.target.value) / 100)}
                  aria-label="IV shock percent"
                />
                %
              </label>
              <label
                className="flex items-center gap-1 text-[10px] text-bb-muted"
                title="Skew beta: directional vol response derived from the chain's own skew slope — selloffs raise scenario vols, rallies crush them (index-realistic). Off = pure smile-riding."
              >
                <input
                  type="checkbox"
                  checked={skewBeta}
                  onChange={(e) => setSkewBeta(e.target.checked)}
                  aria-label="Apply skew-derived spot-vol beta"
                />
                SKEW β
              </label>
              <span
                className="truncate text-[9px] text-bb-muted"
                title={`IV used (analytic rows): ${(p.sigmaUsed * 100).toFixed(1)}%. Model: risk-neutral GBM diffusion; no overnight jumps; no early assignment. Friction ≈$${designer.frictionPerSet.toFixed(0)}/set round trip.`}
              >
                σ={(p.sigmaUsed * 100).toFixed(1)}% ⓘ
              </span>
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
