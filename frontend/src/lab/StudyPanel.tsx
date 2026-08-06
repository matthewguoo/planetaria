import { useEffect, useState } from "react";

/** Offline research runs (Batch API) reporting into the deck.
 *
 * Fetched as a STATIC asset, not an API route: the study lives in
 * backend/scripts + docs/notes, and app/ must not grow a reader for
 * research artefacts just to make them visible. The harness writes
 * frontend/public/study-status.json; vite serves it at the root. */
type Batch = {
  id: string;
  arm: string;
  effort: string;
  n: number;
  est_usd: number;
  status: string;
  succeeded?: number;
  errored?: number;
  processing?: number;
  submitted: string;
};

type Status = {
  updated: string;
  model: string;
  spent_usd: number;
  budget_usd: number;
  requests_done: number;
  requests_total: number;
  results_rows: number;
  batches: Batch[];
};

const ARM_BLURB: Record<string, string> = {
  named: "the replication — live prompt, ticker named",
  blind: "identity ablation — can it still read without knowing who",
  notext: "memory probe — no release text, pure recall",
};

const et = (ts: string) =>
  new Date(ts).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false });

export default function StudyPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    const load = () =>
      fetch(`/study-status.json?t=${Date.now()}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("404"))))
        .then((d: Status) => {
          setStatus(d);
          setMissing(false);
        })
        .catch(() => setMissing(true));
    void load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, []);

  if (missing && !status)
    return (
      <div className="border-t border-bb-border px-3 py-2 text-[11px] text-bb-muted">
        no offline study running — the harness publishes progress here while a batch is in flight.
      </div>
    );
  if (!status) return null;

  const pct = status.requests_total
    ? Math.round((status.requests_done / status.requests_total) * 100)
    : 0;
  const budgetPct = Math.min(100, Math.round((status.spent_usd / status.budget_usd) * 100));
  const running = status.batches.filter((b) => b.status !== "ended");

  return (
    <div className="border-t border-bb-border">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">
          Offline study — <span className="text-bb-muted">{status.model} · batch api</span>
        </span>
        <span className="mono text-[11px] text-bb-muted">
          {running.length ? `${running.length} batch running` : "idle"} · updated {et(status.updated)} ET
        </span>
      </div>

      <div className="grid grid-cols-2 gap-px bg-bb-border">
        <div className="bg-bb-panel px-3 py-1">
          <div className="flex items-baseline justify-between">
            <span className="text-[10px] uppercase text-bb-muted">verdicts</span>
            <span className="mono text-[12px] text-white">
              {status.requests_done.toLocaleString()} / {status.requests_total.toLocaleString()}
            </span>
          </div>
          <div className="mt-1 h-1 w-full bg-bb-hover">
            <div className="h-1 bg-bb-amber" style={{ width: `${pct}%` }} />
          </div>
        </div>
        <div className="bg-bb-panel px-3 py-1">
          <div className="flex items-baseline justify-between">
            <span className="text-[10px] uppercase text-bb-muted">spend (measured)</span>
            <span
              className={`mono text-[12px] ${budgetPct > 90 ? "text-bb-loss" : "text-white"}`}
            >
              ${status.spent_usd.toFixed(2)} / ${status.budget_usd.toFixed(0)}
            </span>
          </div>
          <div className="mt-1 h-1 w-full bg-bb-hover">
            <div
              className={`h-1 ${budgetPct > 90 ? "bg-bb-loss" : "bg-bb-neutral"}`}
              style={{ width: `${budgetPct}%` }}
            />
          </div>
        </div>
      </div>

      <table className="w-full text-[11px]">
        <thead className="text-[10px] uppercase text-bb-muted">
          <tr>
            {["arm", "effort", "n", "done", "err", "status", "est $"].map((h) => (
              <th key={h} className="px-2 py-1 text-left font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="mono">
          {status.batches.map((b) => (
            <tr key={b.id} className="border-t border-bb-border/40">
              <td className="px-2 py-0.5 text-white" title={ARM_BLURB[b.arm]}>
                {b.arm}
              </td>
              <td className="px-2 py-0.5 text-bb-muted">{b.effort}</td>
              <td className="px-2 py-0.5 text-right text-white">{b.n}</td>
              <td className="px-2 py-0.5 text-right text-bb-profit">{b.succeeded ?? "—"}</td>
              <td className={`px-2 py-0.5 text-right ${b.errored ? "text-bb-loss" : "text-bb-muted"}`}>
                {b.errored ?? "—"}
              </td>
              <td
                className={`px-2 py-0.5 text-[10px] uppercase ${
                  b.status === "ended" ? "text-bb-muted" : "text-bb-orange"
                }`}
              >
                {b.status}
              </td>
              <td className="px-2 py-0.5 text-right text-bb-muted">${b.est_usd?.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-1 text-[10px] text-bb-muted">
        These never enter the engine's journal — the strategy runs on its own backend and is unaffected by
        this study. Results land in docs/notes.
      </p>
    </div>
  );
}
