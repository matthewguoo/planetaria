/**
 * Live call-flow monitor: three streams from the backend's rolling call
 * log — data-source HTTP (EDGAR/Finnhub), internal engine chokepoints
 * (journal/bus/decisions), and broker REST (every AlpacaService.call).
 * Poll-based like the rest of the app; each column refreshes ~2s.
 */

import { useEffect, useState } from "react";
import { getMonitorCalls, type MonitorCall, type MonitorSnapshot } from "../../lib/api";

const POLL_MS = 2_000;

const COLUMNS = [
  {
    category: "data" as const,
    title: "DATA SOURCES",
    hint: "outbound HTTP: EDGAR getcurrent/index/exhibits, Finnhub calendar",
  },
  {
    category: "engine" as const,
    title: "ENGINE",
    hint: "journal writes · bus publishes · strategy decisions (timer ticks excluded)",
  },
  {
    category: "broker" as const,
    title: "ALPACA",
    hint: "every REST call through AlpacaService.call (streams counted in SYSTEM)",
  },
];

/** The three call streams side by side. Exported on its own because the ops
 * console embeds it inside the SYSTEM page rather than giving it a tab —
 * "what is the process saying to whom" is one of the tech stats, not a
 * separate destination. */
export function CallFlow() {
  return (
    <div className="flex min-h-0 flex-1 gap-px overflow-hidden">
      {COLUMNS.map((col) => (
        <CallColumn key={col.category} {...col} />
      ))}
    </div>
  );
}

export default function MonitorPage() {
  return <CallFlow />;
}

function CallColumn({
  category,
  title,
  hint,
}: {
  category: "data" | "engine" | "broker";
  title: string;
  hint: string;
}) {
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getMonitorCalls(category)
        .then((s) => {
          if (alive) {
            setSnap(s);
            setError(false);
          }
        })
        .catch(() => alive && setError(true));
    load();
    const t = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [category]);

  const count = snap?.status.counts[category] ?? 0;
  const errors = snap?.status.errors[category] ?? 0;

  return (
    <div className="panel flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="panel-title flex items-center justify-between">
        <span title={hint}>{title}</span>
        <span className="text-[10px] text-bb-muted" data-numeric>
          {count} calls{errors ? ` · ${errors} err` : ""}
        </span>
      </div>
      {error ? (
        <div className="flex h-16 items-center justify-center text-[11px] text-bb-loss">
          backend unreachable
        </div>
      ) : !snap ? (
        <div className="flex h-16 items-center justify-center text-[11px] text-bb-muted">
          loading…
        </div>
      ) : snap.calls.length === 0 ? (
        <div className="flex h-16 items-center justify-center px-3 text-center text-[11px] text-bb-muted">
          no calls yet — {hint}
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <table className="w-full border-collapse text-[10.5px]">
            <tbody>
              {snap.calls.map((c, i) => (
                <CallRow key={`${c.ts}-${i}`} call={c} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CallRow({ call }: { call: MonitorCall }) {
  const t = new Date(call.ts * 1000);
  const hh = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}:${String(t.getSeconds()).padStart(2, "0")}`;
  return (
    <tr className="border-b border-bb-border/40 hover:bg-bb-hover">
      <td className="whitespace-nowrap px-2 py-0.5 align-top text-bb-muted" data-numeric>
        {hh}
      </td>
      <td
        className={
          "max-w-0 truncate px-1 py-0.5 align-top " +
          (call.ok ? "text-bb-amber" : "text-bb-loss")
        }
        style={{ width: "45%" }}
        title={`${call.name}\n${call.detail}`}
      >
        {call.name}
      </td>
      <td className="max-w-0 truncate px-1 py-0.5 align-top text-bb-muted" title={call.detail}>
        {call.detail}
      </td>
      <td className="whitespace-nowrap px-2 py-0.5 text-right align-top text-bb-neutral" data-numeric>
        {call.ms != null ? `${call.ms}ms` : ""}
      </td>
    </tr>
  );
}
