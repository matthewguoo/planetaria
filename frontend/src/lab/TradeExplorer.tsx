import { useEffect, useMemo, useState } from "react";

/** Every scored event, taken or declined, under each exit policy.
 *
 * The declined rows are the point: this strategy's measured value is
 * refusal, not selection, so a view that shows only executed trades hides
 * the mechanism. Static asset, same reasoning as the other study panels. */
type Trade = {
  sym: string;
  date: string;
  year: string;
  side: number;
  move: number;
  run5d: number | null;
  dvM: number | null;
  anchor: number;
  entry: number;
  dir: string;
  conf: string;
  guid: string;
  flags: string[];
  summary: string;
  gated: boolean;
  why: string;
  ret: Record<string, number>;
  exit: Record<string, string>;
};

type Payload = {
  updated: string;
  effort: string;
  costs_bp: number;
  policies: Record<string, string>;
  n: number;
  gated: number;
  trades: Trade[];
};

const DIR_TONE: Record<string, string> = {
  bullish: "text-bb-profit",
  bearish: "text-bb-loss",
  neutral: "text-bb-muted",
};
const EXIT_TONE: Record<string, string> = {
  sl: "text-bb-loss",
  tp: "text-bb-profit",
  close: "text-bb-muted",
};

const bp = (n: number | undefined) =>
  n === undefined ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(0)}`;
const tone = (n: number | undefined) =>
  n === undefined ? "" : n > 0 ? "text-bb-profit" : n < 0 ? "text-bb-loss" : "";

export default function TradeExplorer() {
  const [data, setData] = useState<Payload | null>(null);
  const [taken, setTaken] = useState<"all" | "taken" | "declined">("all");
  const [year, setYear] = useState("all");
  const [dir, setDir] = useState("all");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ k: string; asc: boolean }>({ k: "date", asc: false });
  const [open, setOpen] = useState<string | null>(null);
  const [policy, setPolicy] = useState("t1");

  useEffect(() => {
    fetch(`/study-trades.json?t=${Date.now()}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("404"))))
      .then(setData)
      .catch(() => undefined);
  }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    const needle = q.trim().toUpperCase();
    const out = data.trades.filter(
      (t) =>
        (taken === "all" || (taken === "taken" ? t.gated : !t.gated)) &&
        (year === "all" || t.year === year) &&
        (dir === "all" || t.dir === dir) &&
        (!needle || t.sym.includes(needle)),
    );
    const dirn = sort.asc ? 1 : -1;
    return out.sort((a, b) => {
      const get = (t: Trade) =>
        sort.k === "ret" ? (t.ret[policy] ?? 0) : (t as unknown as Record<string, number | string>)[sort.k];
      const av = get(a), bv = get(b);
      return av === bv ? 0 : (av > bv ? 1 : -1) * dirn;
    });
  }, [data, taken, year, dir, q, sort, policy]);

  const summary = useMemo(() => {
    if (!rows.length) return null;
    const rs = rows.map((t) => t.ret[policy] ?? 0);
    const takenRows = rows.filter((t) => t.gated);
    return {
      n: rows.length,
      mean: rs.reduce((a, b) => a + b, 0) / rs.length,
      win: (rs.filter((r) => r > 0).length / rs.length) * 100,
      takenMean: takenRows.length
        ? takenRows.reduce((a, t) => a + (t.ret[policy] ?? 0), 0) / takenRows.length
        : 0,
    };
  }, [rows, policy]);

  if (!data)
    return (
      <div className="border-b border-bb-border px-3 py-2 text-[11px] text-bb-muted">
        no trade export yet — run <span className="mono">research_holding_period.py trades</span>.
      </div>
    );

  const years = Array.from(new Set(data.trades.map((t) => t.year))).sort();
  const th = (k: string, label: string, cls = "") => (
    <th
      className={`cursor-pointer px-2 py-1 text-right font-normal hover:text-bb-amber ${cls}`}
      onClick={() => setSort((s) => ({ k, asc: s.k === k ? !s.asc : false }))}
    >
      {label}
      {sort.k === k ? (sort.asc ? " ▲" : " ▼") : ""}
    </th>
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-bb-border px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">
          Trade explorer
        </span>
        <span className="mono text-[10px] text-bb-muted">
          {data.gated.toLocaleString()} taken of {data.n.toLocaleString()} events
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          {(["all", "taken", "declined"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setTaken(f)}
              className={`border px-2 py-0.5 text-[10px] uppercase ${
                taken === f ? "border-bb-amber text-bb-amber" : "border-bb-border text-bb-muted"
              }`}
            >
              {f}
            </button>
          ))}
          <select
            value={year}
            onChange={(e) => setYear(e.target.value)}
            className="mono border border-bb-border bg-black px-1 py-0.5 text-[10px] text-bb-muted"
          >
            <option value="all">all years</option>
            {years.map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
          <select
            value={dir}
            onChange={(e) => setDir(e.target.value)}
            className="mono border border-bb-border bg-black px-1 py-0.5 text-[10px] text-bb-muted"
          >
            <option value="all">any verdict</option>
            <option value="bullish">bullish</option>
            <option value="bearish">bearish</option>
            <option value="neutral">neutral</option>
          </select>
          <select
            value={policy}
            onChange={(e) => setPolicy(e.target.value)}
            className="mono border border-bb-border bg-black px-1 py-0.5 text-[10px] text-bb-amber"
          >
            {Object.entries(data.policies).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="ticker"
            className="mono w-20 border border-bb-border bg-black px-1 py-0.5 text-[10px] text-white placeholder:text-bb-muted"
          />
        </div>
      </div>

      {summary && (
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-b border-bb-border/60 px-3 py-1 text-[10px] text-bb-muted">
          <span>
            showing <b className="mono text-white">{summary.n.toLocaleString()}</b>
          </span>
          <span>
            mean <b className={`mono ${tone(summary.mean)}`}>{bp(summary.mean)}bp</b>
          </span>
          <span>
            of those taken <b className={`mono ${tone(summary.takenMean)}`}>{bp(summary.takenMean)}bp</b>
          </span>
          <span>
            win <b className="mono text-white">{summary.win.toFixed(1)}%</b>
          </span>
          <span className="text-bb-faint">
            declined rows show what the trade WOULD have returned
          </span>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 z-[1] bg-bb-panel text-[10px] uppercase text-bb-muted">
            <tr className="border-b border-bb-border">
              {th("date", "date", "text-left")}
              {th("sym", "sym", "text-left")}
              <th className="px-2 py-1 text-left font-normal">verdict</th>
              {th("move", "reaction")}
              {th("run5d", "run 5d")}
              {th("dvM", "$vol M")}
              <th className="px-2 py-1 text-center font-normal">taken</th>
              <th className="px-2 py-1 text-left font-normal">exit</th>
              {th("ret", "P&L bp")}
            </tr>
          </thead>
          <tbody className="mono">
            {rows.slice(0, 600).map((t) => {
              const id = `${t.sym}-${t.date}`;
              const r = t.ret[policy];
              return (
                <tr
                  key={id}
                  onClick={() => setOpen(open === id ? null : id)}
                  className={`cursor-pointer border-b border-bb-border/30 hover:bg-bb-hover ${
                    open === id ? "bg-bb-hover" : ""
                  } ${t.gated ? "" : "opacity-60"}`}
                >
                  <td className="px-2 py-0.5 text-bb-muted">{t.date}</td>
                  <td className="px-2 py-0.5 text-white">{t.sym}</td>
                  <td className="px-2 py-0.5">
                    <span className={DIR_TONE[t.dir]}>{t.dir}</span>
                    <span className="text-bb-faint"> · {t.conf}</span>
                    {t.flags.length > 0 && <span className="text-bb-orange"> · {t.flags.length}⚑</span>}
                  </td>
                  <td className={`px-2 py-0.5 text-right ${t.move >= 0 ? "text-bb-profit" : "text-bb-loss"}`}>
                    {t.move >= 0 ? "+" : ""}{t.move.toFixed(1)}%
                  </td>
                  <td className="px-2 py-0.5 text-right text-bb-muted">
                    {t.run5d === null ? "—" : `${t.run5d >= 0 ? "+" : ""}${t.run5d.toFixed(1)}%`}
                  </td>
                  <td className="px-2 py-0.5 text-right text-bb-muted">{t.dvM ?? "—"}</td>
                  <td className="px-2 py-0.5 text-center">
                    {t.gated ? (
                      <span className={t.side > 0 ? "text-bb-profit" : "text-bb-loss"}>
                        {t.side > 0 ? "LONG" : "SHORT"}
                      </span>
                    ) : (
                      <span className="text-bb-muted">—</span>
                    )}
                  </td>
                  <td className={`px-2 py-0.5 text-[10px] uppercase ${EXIT_TONE[t.exit[policy]] ?? ""}`}>
                    {t.exit[policy]}
                  </td>
                  <td className={`px-2 py-0.5 text-right ${tone(r)}`}>{bp(r)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length > 600 && (
          <p className="px-3 py-2 text-[10px] text-bb-muted">
            showing the first 600 of {rows.length.toLocaleString()} — narrow the filters to see the rest.
          </p>
        )}
        {open && (() => {
          const t = rows.find((x) => `${x.sym}-${x.date}` === open);
          if (!t) return null;
          return (
            <div className="sticky bottom-0 border-t-2 border-bb-amber bg-bb-bg2 px-3 py-2 text-[11px]">
              <div className="mb-1 flex flex-wrap items-baseline gap-x-4">
                <b className="mono text-white">{t.sym}</b>
                <span className="mono text-bb-muted">{t.date}</span>
                <span className={DIR_TONE[t.dir]}>{t.dir} / {t.conf}</span>
                <span className="text-bb-muted">guidance {t.guid}</span>
                <span className="mono text-bb-muted">
                  {t.anchor.toFixed(2)} → {t.entry.toFixed(2)}
                </span>
                {!t.gated && <span className="text-bb-loss">DECLINED — {t.why}</span>}
              </div>
              {t.flags.length > 0 && (
                <div className="mb-1 text-[10px] text-bb-orange">
                  flags: {t.flags.join(", ").replace(/_/g, " ")}
                </div>
              )}
              <p className="mb-1 text-white">{t.summary}</p>
              <div className="flex flex-wrap gap-x-4 text-[10px] text-bb-muted">
                {Object.entries(t.ret).map(([k, v]) => (
                  <span key={k}>
                    {k}: <b className={`mono ${tone(v)}`}>{bp(v)}bp</b> ({t.exit[k]})
                  </span>
                ))}
              </div>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
