import { useState } from "react";
import type { Analysis } from "./api";

const et = (ts: string) =>
  new Date(ts).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false });

/** Every ctx.analyze() round trip, success or failure. Failures are the
 * point as much as successes: a refusal or a timeout is why a name went
 * untraded, and it belongs on the same surface as the verdicts. */
export default function CallsPanel({ analyses }: { analyses: Analysis[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const errors = analyses.filter((a) => a.payload?.error).length;
  const latencies = analyses
    .map((a) => a.payload?.latency_ms)
    .filter((n): n is number => typeof n === "number");
  const median = latencies.length
    ? [...latencies].sort((a, b) => a - b)[Math.floor(latencies.length / 2)]
    : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-bb-border px-3 py-1.5">
        <span className="text-[11px] uppercase tracking-widest text-bb-amber">LLM calls</span>
        <span className="mono text-[11px] text-bb-muted">
          {analyses.length} calls · {errors} failed
          {median !== null && ` · median ${(median / 1000).toFixed(1)}s`}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {analyses.length === 0 && <p className="p-3 text-bb-muted">no analyses journaled yet.</p>}
        {analyses.map((a) => {
          const err = a.payload?.error;
          const result = a.payload?.result as Record<string, string> | null | undefined;
          return (
            <div key={a.id} className="border-b border-bb-border/50">
              <button
                onClick={() => setOpen(open === a.id ? null : a.id)}
                className="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-bb-hover"
              >
                <span className="mono w-16 shrink-0 text-[11px] text-bb-muted">{et(a.ts)}</span>
                <span className={`w-12 shrink-0 text-[10px] uppercase ${err ? "text-bb-loss" : "text-bb-profit"}`}>
                  {err ? "fail" : "ok"}
                </span>
                <span className="mono w-14 shrink-0 text-white">{a.symbols?.[0] ?? "—"}</span>
                <span className="mono w-14 shrink-0 text-right text-bb-muted">
                  {a.payload?.latency_ms ? `${(a.payload.latency_ms / 1000).toFixed(1)}s` : "—"}
                </span>
                <span className="w-28 shrink-0 truncate text-[10px] text-bb-muted">{a.payload?.model ?? ""}</span>
                <span className="truncate text-[11px] text-bb-muted">
                  {err ?? result?.summary ?? a.source}
                </span>
              </button>
              {open === a.id && (
                <div className="border-l-2 border-bb-neutral bg-bb-bg2 px-3 py-2 text-[11px]">
                  <p className="mb-1 text-bb-muted">task</p>
                  <pre className="mb-2 whitespace-pre-wrap text-[10px] text-white">{a.payload?.task}</pre>
                  <p className="mb-1 text-bb-muted">structured result</p>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap text-[10px] text-white">
                    {JSON.stringify(a.payload?.result ?? { error: err }, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
