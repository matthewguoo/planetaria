import { useEffect, useMemo, useState } from "react";

/** 5-year study result: the LLM-gated stream compounded against SPY
 * buy-and-hold, with the daily-return regression that separates "it went
 * up" from alpha. Static asset, same reasoning as StudyPanel. */
type Stats = {
  total_return_pct: number;
  cagr_pct: number;
  alpha_annual_pct: number;
  beta: number;
  sharpe: number;
  max_drawdown_pct: number;
  days_traded: number;
  trades: number;
};

type Curve = {
  updated: string;
  model: string;
  effort: string;
  span: [string, string];
  note: string;
  dates: string[];
  series: { vol: number[]; flat: number[]; spy: number[] };
  stats: Record<string, Stats>;
};

const SERIES = [
  { key: "vol", label: "VOL-WEIGHTED", color: "#FFB000" },
  { key: "flat", label: "FLAT 10%/NAME", color: "#00C853" },
  { key: "spy", label: "SPY BUY & HOLD", color: "#2196F3" },
] as const;

export default function BenchmarkChart() {
  const [data, setData] = useState<Curve | null>(null);
  const [logScale, setLogScale] = useState(true);

  useEffect(() => {
    const load = () =>
      fetch(`/study-curve.json?t=${Date.now()}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("404"))))
        .then(setData)
        .catch(() => undefined);
    void load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  const geom = useMemo(() => {
    if (!data) return null;
    const all = [...data.series.vol, ...data.series.flat, ...data.series.spy];
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const W = 1000;
    const H = 300;
    const tx = (v: number) => (logScale ? Math.log(v) : v);
    const [yLo, yHi] = [tx(lo), tx(hi)];
    const x = (i: number) => (i / (data.dates.length - 1)) * W;
    const y = (v: number) => H - ((tx(v) - yLo) / (yHi - yLo || 1)) * H;
    const paths = SERIES.map((s) =>
      data.series[s.key].map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" "),
    );
    // One gridline per calendar year so the shape is readable in time.
    const ticks: { x: number; label: string }[] = [];
    let year = "";
    data.dates.forEach((d, i) => {
      if (d.slice(0, 4) !== year) {
        year = d.slice(0, 4);
        ticks.push({ x: x(i), label: year });
      }
    });
    return { paths, W, H, ticks, baseY: y(1) };
  }, [data, logScale]);

  if (!data || !geom)
    return (
      <div className="border-b border-bb-border px-3 py-2 text-[11px] text-bb-muted">
        no study curve yet — run `research_llm_contamination.py curve` after the batches land.
      </div>
    );

  const fmt = (n: number, unit = "%") => `${n >= 0 ? "+" : ""}${n.toFixed(n >= 100 ? 0 : 2)}${unit}`;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-bb-border px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">
          5-year study vs S&amp;P 500 —{" "}
          <span className="text-bb-muted">
            {data.model} · {data.effort} · {data.stats.vol.trades} gated trades
          </span>
        </span>
        <div className="flex items-center gap-2">
          <span className="mono text-[11px] text-bb-muted">
            {data.span[0]} → {data.span[1]}
          </span>
          <button
            onClick={() => setLogScale((v) => !v)}
            className="border border-bb-border px-2 py-0.5 text-[10px] uppercase text-bb-muted hover:text-bb-amber"
          >
            {logScale ? "log" : "linear"}
          </button>
        </div>
      </div>

      <svg viewBox={`0 0 ${geom.W} ${geom.H}`} className="h-[300px] w-full" preserveAspectRatio="none">
        {geom.ticks.map((t) => (
          <line key={t.label} x1={t.x} x2={t.x} y1="0" y2={geom.H} stroke="#333" strokeWidth="1" />
        ))}
        <line x1="0" x2={geom.W} y1={geom.baseY} y2={geom.baseY} stroke="#555" strokeDasharray="4 6" />
        {geom.paths.map((d, i) => (
          <path
            key={SERIES[i].key}
            d={d}
            fill="none"
            stroke={SERIES[i].color}
            strokeWidth="1.6"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <div className="flex justify-between px-3 text-[9px] text-bb-muted">
        {geom.ticks.map((t) => (
          <span key={t.label}>{t.label}</span>
        ))}
      </div>

      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-bb-muted">
          <tr>
            {["", "total", "cagr", "alpha p.a.", "beta", "sharpe", "max dd", "days in"].map((h) => (
              <th key={h} className="px-2 py-1 text-right font-normal first:text-left">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="mono">
          {SERIES.map((s) => {
            const st = data.stats[s.key];
            if (!st) return null;
            return (
              <tr key={s.key} className="border-t border-bb-border/40">
                <td className="px-2 py-0.5">
                  <span className="mr-1.5 inline-block h-2 w-3 align-middle" style={{ background: s.color }} />
                  <span className="text-white">{s.label}</span>
                </td>
                <td className="px-2 py-0.5 text-right text-white">{fmt(st.total_return_pct)}</td>
                <td className="px-2 py-0.5 text-right text-white">{fmt(st.cagr_pct)}</td>
                <td
                  className={`px-2 py-0.5 text-right ${
                    st.alpha_annual_pct > 0 ? "text-bb-profit" : "text-bb-muted"
                  }`}
                >
                  {s.key === "spy" ? "—" : fmt(st.alpha_annual_pct)}
                </td>
                <td className="px-2 py-0.5 text-right text-white">{st.beta.toFixed(3)}</td>
                <td className="px-2 py-0.5 text-right text-white">{st.sharpe.toFixed(2)}</td>
                <td className="px-2 py-0.5 text-right text-bb-loss">-{st.max_drawdown_pct.toFixed(1)}%</td>
                <td className="px-2 py-0.5 text-right text-bb-muted">{st.days_traded}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="px-3 py-1 text-[10px] text-bb-muted">{data.note}.</p>
    </div>
  );
}
