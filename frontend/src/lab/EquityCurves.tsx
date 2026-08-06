import { useMemo } from "react";
import type { Sim, SimPolicy } from "./api";

const COLORS: Record<string, string> = { vol: "#FFB000", flat: "#2196F3" };
const LABEL: Record<string, string> = {
  vol: "VOL-WEIGHTED (live sizing)",
  flat: "FLAT NOTIONAL",
};

/** Inline SVG rather than a chart dependency: two step series, shared axis,
 * no interaction beyond hover. A charting library here would be more code
 * than the chart. */
function Curves({ policies }: { policies: SimPolicy[] }) {
  const geom = useMemo(() => {
    // A curve of "start" and "now" with nothing between is a flat line that
    // reads as data. Until something actually resolves, say so instead.
    if (policies.every((p) => p.trades === 0)) return null;
    const pts = policies.map((p) =>
      p.curve
        .filter((c) => c.ts)
        .map((c) => ({ t: Date.parse(c.ts as string), e: c.equity })),
    );
    const all = pts.flat();
    if (all.length < 2) return null;
    const t0 = Math.min(...all.map((p) => p.t));
    const t1 = Math.max(...all.map((p) => p.t));
    const lo = Math.min(...all.map((p) => p.e));
    const hi = Math.max(...all.map((p) => p.e));
    const pad = (hi - lo) * 0.08 || Math.max(hi * 0.001, 1);
    const yLo = lo - pad;
    const yHi = hi + pad;
    const W = 1000;
    const H = 260;
    const x = (t: number) => (t1 === t0 ? W : ((t - t0) / (t1 - t0)) * W);
    const y = (e: number) => H - ((e - yLo) / (yHi - yLo)) * H;
    // Step-after: equity is flat between resolutions, not linearly drifting.
    const paths = pts.map((series) =>
      series
        .map((p, i) =>
          i === 0 ? `M${x(p.t)},${y(p.e)}` : `H${x(p.t)} V${y(p.e)}`,
        )
        .join(" "),
    );
    const base = policies[0]?.start_equity ?? 0;
    return { paths, W, H, yLo, yHi, baseY: y(base), base };
  }, [policies]);

  if (!geom)
    return (
      <div className="flex h-[260px] items-center justify-center text-bb-muted">
        no resolved trades yet — the curve draws itself as decisions settle
      </div>
    );

  return (
    <svg viewBox={`0 0 ${geom.W} ${geom.H}`} className="h-[260px] w-full" preserveAspectRatio="none">
      <line
        x1="0"
        x2={geom.W}
        y1={geom.baseY}
        y2={geom.baseY}
        stroke="#333"
        strokeDasharray="4 6"
        strokeWidth="1"
      />
      {geom.paths.map((d, i) => (
        <path
          key={policies[i].policy}
          d={d}
          fill="none"
          stroke={COLORS[policies[i].policy]}
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 px-3 py-1">
      <span className="text-[11px] uppercase tracking-wide text-bb-muted">{label}</span>
      <span className={`mono text-[13px] ${tone ?? "text-white"}`}>{value}</span>
    </div>
  );
}

const money = (n: number) => `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
const tone = (n: number) => (n > 0 ? "text-bb-profit" : n < 0 ? "text-bb-loss" : "text-white");

export default function EquityCurves({ sim }: { sim: Sim | null }) {
  if (!sim) return <div className="p-3 text-bb-muted">loading twin…</div>;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-bb-border px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">
          Paper twin — same decisions, two allocators
        </span>
        <span className="mono text-[11px] text-bb-muted">
          {sim.signals} signals · start {money(sim.params.equity)} · risk {sim.params.risk_pct}% · flat{" "}
          {sim.params.flat_pct}%
        </span>
      </div>
      <Curves policies={sim.policies} />
      <div className="grid grid-cols-2 gap-px border-t border-bb-border bg-bb-border">
        {sim.policies.map((p) => (
          <div key={p.policy} className="bg-bb-panel py-1">
            <div className="flex items-center gap-2 px-3 pb-1">
              <span className="inline-block h-2 w-3" style={{ background: COLORS[p.policy] }} />
              <span className="text-[11px] uppercase tracking-wide text-white">{LABEL[p.policy]}</span>
            </div>
            <Stat label="equity" value={money(p.equity)} tone={tone(p.equity - p.start_equity)} />
            <Stat label="return" value={`${p.return_pct >= 0 ? "+" : ""}${p.return_pct}%`} tone={tone(p.return_pct)} />
            <Stat label="realized / unreal" value={`${money(p.realized)} / ${money(p.unrealized)}`} />
            <Stat label="max drawdown" value={money(-p.max_drawdown)} tone={p.max_drawdown ? "text-bb-loss" : undefined} />
            <Stat
              label="trades (open)"
              value={`${p.closed} closed (${p.open} open)`}
            />
            <Stat label="win rate" value={p.win_rate === null ? "—" : `${Math.round(p.win_rate * 100)}%`} />
            <Stat label="exposure" value={money(p.exposure)} />
          </div>
        ))}
      </div>
    </div>
  );
}
