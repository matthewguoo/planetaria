import { useEffect, useMemo, useState } from "react";

/** 5-year study result: the LLM-gated stream compounded against SPY
 * buy-and-hold, with the daily-return regression that separates "it went
 * up" from alpha. Static asset, same reasoning as StudyPanel. */
type Stats = {
  total_pct: number;
  cagr_pct: number;
  alpha_pct: number;
  beta: number;
  sharpe: number;
  max_dd_pct: number;
  avg_concurrent?: number;
  weight_pct?: number;
  taken?: number;
};

type Curve = {
  updated: string;
  model: string;
  effort: string;
  corrected?: boolean;
  span: [string, string];
  note: string;
  dates: string[];
  labels?: Record<string, string>;
  series: Record<string, number[]>;
  stats: Record<string, Stats>;
};

/** Fixed order, fixed hues — a series never changes colour because another
 * one appeared or was filtered out.
 *
 * These are NOT the bb terminal tokens, and that is deliberate. The previous
 * trio was bb-amber / bb-profit / bb-loss, which fails the dataviz palette
 * validator: #FFB000 against #00C853 is protanope ΔE 2.9 (OKLab x100, floor
 * 6, target 8) — an amber line and a green line are the same line to a
 * red-blind reader. Adding a fifth series made it worse. These five are the
 * validated dark-mode categorical steps, in the published order, and clear
 * every gate on the #111111 panel: worst adjacent CVD ΔE 8.4, worst
 * normal-vision ΔE 19.3, all >= 3:1 contrast.
 *
 *   node scripts/validate_palette.js \
 *     "#3987e5,#d95926,#199e70,#c98500,#d55181" --mode dark --surface "#111111"
 *
 * The two baselines are dashed as well as hued: identity should not rest on
 * colour alone, and "reference, not result" is exactly what a dash says. */
const SERIES = [
  { key: "best", color: "#3987e5", dash: "" },
  { key: "alt", color: "#d95926", dash: "" },
  { key: "shipped", color: "#199e70", dash: "6 4" },
  { key: "t1plain", color: "#c98500", dash: "2 3" },
  { key: "spy", color: "#d55181", dash: "" },
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
    const keys = SERIES.filter((s) => data.series[s.key]).map((s) => s.key);
    const all = keys.flatMap((k) => data.series[k]);
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const W = 1000;
    const H = 300;
    const tx = (v: number) => (logScale ? Math.log(v) : v);
    const [yLo, yHi] = [tx(lo), tx(hi)];
    const x = (i: number) => (i / (data.dates.length - 1)) * W;
    const y = (v: number) => H - ((tx(v) - yLo) / (yHi - yLo || 1)) * H;
    const paths = keys.map((k) =>
      data.series[k].map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" "),
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
    return { paths, keys, W, H, ticks, baseY: y(1) };
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
            {data.model} · {data.effort}
            {data.corrected ? " · corrected exits" : ""}
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
        {geom.paths.map((d, i) => {
          const s = SERIES.find((x) => x.key === geom.keys[i])!;
          return (
            <path
              key={s.key}
              d={d}
              fill="none"
              stroke={s.color}
              strokeWidth="2"
              strokeDasharray={s.dash || undefined}
              vectorEffect="non-scaling-stroke"
            />
          );
        })}
      </svg>
      <div className="flex justify-between px-3 text-[9px] text-bb-muted">
        {geom.ticks.map((t) => (
          <span key={t.label}>{t.label}</span>
        ))}
      </div>

      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-bb-muted">
          <tr>
            {["", "total", "cagr", "alpha p.a.", "beta", "sharpe", "max dd", "size/name"].map((h) => (
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
                  {/* The swatch repeats the dash so the legend and the line
                      agree — a solid chip next to a dashed line is a lie. */}
                  <svg className="mr-1.5 inline-block align-middle" width="14" height="8" aria-hidden>
                    <line
                      x1="0"
                      y1="4"
                      x2="14"
                      y2="4"
                      stroke={s.color}
                      strokeWidth="2"
                      strokeDasharray={s.dash || undefined}
                    />
                  </svg>
                  <span className="text-white">{data.labels?.[s.key] ?? s.key}</span>
                </td>
                <td className="px-2 py-0.5 text-right text-white">{fmt(st.total_pct)}</td>
                <td className="px-2 py-0.5 text-right text-white">{fmt(st.cagr_pct)}</td>
                <td className={`px-2 py-0.5 text-right ${st.alpha_pct > 0 ? "text-bb-profit" : "text-bb-muted"}`}>
                  {s.key === "spy" ? "—" : fmt(st.alpha_pct)}
                </td>
                <td className="px-2 py-0.5 text-right text-white">{st.beta.toFixed(3)}</td>
                <td className="px-2 py-0.5 text-right text-white">{st.sharpe.toFixed(2)}</td>
                <td className="px-2 py-0.5 text-right text-bb-loss">-{st.max_dd_pct.toFixed(1)}%</td>
                <td className="px-2 py-0.5 text-right text-bb-muted">
                  {st.weight_pct ? `${st.weight_pct.toFixed(1)}%` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="px-3 py-1 text-[10px] text-bb-muted">{data.note}.</p>
    </div>
  );
}
