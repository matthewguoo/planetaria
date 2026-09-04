/**
 * The server administration window: one dense screen per engine, meant to
 * sit open on the box's own display. Left: what the process and its stores
 * are doing. Middle: every call to Alpaca and the data sources as it
 * happens. Right: the engine's own feed - the plan journal and the log
 * tail. No controls; this is the instrument panel, not the cockpit.
 */

import { useEffect, useRef, useState } from "react";
import {
  getAdminEvents,
  getAdminLog,
  getAdminSummary,
  getMonitorCalls,
  type AdminSummary,
  type MonitorCall,
  type PlanEvent,
} from "../lib/api";
import { usePoll } from "../lib/usePoll";

type AdminEvent = PlanEvent & { id?: number; plan_id?: string };

function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
}

function fmtClock(tsSec: number): string {
  return new Date(tsSec * 1000).toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function Dot({ ok, warn = false }: { ok: boolean; warn?: boolean }) {
  return <span className={"inline-block h-2 w-2 shrink-0 rounded-full " + (ok ? "bg-bb-profit" : warn ? "bg-bb-orange" : "bg-bb-loss")} />;
}

function Row({ label, value, ok, warn = false, cls = "text-white" }: { label: string; value: React.ReactNode; ok?: boolean; warn?: boolean; cls?: string }) {
  return (
    <div className="flex items-center gap-2 border-b border-bb-border/40 px-2 py-1">
      {ok !== undefined && <Dot ok={ok} warn={warn} />}
      <span className="w-28 shrink-0 text-[10px] tracking-widest text-bb-muted">{label}</span>
      <span data-numeric className={"min-w-0 flex-1 truncate text-right text-[11px] " + cls}>{value}</span>
    </div>
  );
}

function Panel({ title, right, children, className = "" }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={"panel flex min-h-0 flex-col " + className}>
      <div className="panel-title flex items-center justify-between">
        <span>{title}</span>
        {right}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}

const fmtUsd = (v: number | null | undefined) => (v == null ? "—" : `$${Math.round(v).toLocaleString()}`);

export function AdminApp() {
  const [summary, setSummary] = useState<AdminSummary | null>(null);
  const [calls, setCalls] = useState<MonitorCall[]>([]);
  const [callStatus, setCallStatus] = useState<{ counts: Record<string, number>; errors: Record<string, number> } | null>(null);
  const [events, setEvents] = useState<AdminEvent[]>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [category, setCategory] = useState<"all" | "broker" | "data" | "engine">("broker");
  const [reachable, setReachable] = useState(true);
  const logRef = useRef<HTMLPreElement>(null);

  usePoll(async (alive) => {
    try {
      const s = await getAdminSummary();
      if (!alive()) return;
      setSummary(s);
      setReachable(true);
    } catch {
      if (alive()) setReachable(false);
    }
  }, 2_000);
  usePoll(async (alive) => {
    try {
      const snap = await getMonitorCalls(category === "all" ? undefined : category, 80);
      if (!alive()) return;
      setCalls(snap.calls);
      setCallStatus(snap.status);
    } catch {
      /* the summary poll reports reachability */
    }
  }, 2_000, [category]);
  usePoll(async (alive) => {
    try {
      const e = await getAdminEvents(40);
      if (alive()) setEvents(e);
    } catch {
      /* keep the last feed */
    }
  }, 5_000);
  usePoll(async (alive) => {
    try {
      const l = await getAdminLog(120);
      if (alive()) setLogLines(l.lines);
    } catch {
      /* keep the last tail */
    }
  }, 5_000);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  const sv = summary?.server;
  const sys = summary?.system;
  const live = sv?.mode === "live_manual";
  const feedAge = sys?.feed?.stream_age_s ?? null;
  const tasks = sys?.tasks ?? {};
  const monitorsWithoutMid = Object.keys(sys?.enforcer?.monitors_without_mid ?? {}).length;
  const errors = callStatus?.errors ?? {};
  const totalErrors = Object.values(errors).reduce((a, b) => a + b, 0);

  return (
    <div className="flex h-[100dvh] flex-col gap-px bg-bb-black p-px text-white">
      <header className={"flex h-9 items-center gap-4 border-b px-3 text-[11px] " + (live ? "border-bb-loss bg-bb-loss/10" : "border-bb-border bg-bb-panel")}>
        <span className="text-[13px] font-semibold tracking-widest">PLANETARIA</span>
        <span className={"border px-2 py-0.5 tracking-widest " + (live ? "border-bb-loss text-bb-loss" : "border-bb-orange text-bb-orange")}>
          {live ? "LIVE" : "PAPER"}
        </span>
        <span className="text-bb-muted">{sv?.account ?? "—"} · {sv?.host ?? "—"}{sv?.port ? `:${sv.port}` : ""}</span>
        <span className="text-bb-muted">v <span data-numeric className="text-white">{sv?.version ?? "—"}</span></span>
        <span className="text-bb-muted">up <span data-numeric className="text-white">{sv ? fmtUptime(sv.uptime_s) : "—"}</span></span>
        <span className="text-bb-muted">EQUITY <span data-numeric className="text-white">{fmtUsd(summary?.account?.equity)}</span></span>
        <span className="text-bb-muted">CASH <span data-numeric className="text-white">{fmtUsd(summary?.account?.cash)}</span></span>
        <span className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={!!sys?.db?.ok} /> DB</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={!!sys?.redis?.ok} /> REDIS</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={feedAge != null && feedAge < 60} warn={feedAge == null} /> FEED</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={sys?.broker?.account_status === "ACTIVE"} /> BROKER</span>
          {!reachable && <span className="text-bb-loss">ENGINE UNREACHABLE</span>}
          <span data-numeric className="text-bb-muted">{new Date().toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false })} ET</span>
        </span>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1.2fr)] gap-px">
        {/* STATS */}
        <div className="flex min-h-0 flex-col gap-px">
          <Panel title="PROCESS" className="shrink-0">
            <Row label="CPU" value={summary?.process?.cpu_pct != null ? `${summary.process.cpu_pct.toFixed(1)}%` : "—"} ok={(summary?.process?.cpu_pct ?? 0) < 50} />
            <Row label="RSS" value={summary?.process?.rss_mb != null ? `${summary.process.rss_mb.toFixed(0)} MB` : "—"} />
            <Row label="TASKS / THREADS" value={`${summary?.process?.tasks ?? "—"} / ${summary?.process?.threads ?? "—"}`} />
            <Row label="PID" value={sv?.pid ?? "—"} />
            <Row label="DB" value={`${sys?.db?.engine ?? "—"} · ${sys?.db?.latency_ms != null ? `${sys.db.latency_ms} ms` : "—"}`} ok={!!sys?.db?.ok} />
            <Row label="REDIS" value={sys?.redis?.ok ? "ok" : "down"} ok={!!sys?.redis?.ok} />
          </Panel>
          <Panel title="FEED" className="shrink-0">
            <Row label="STREAM AGE" value={feedAge != null ? `${feedAge.toFixed(1)} s` : "no stream"} ok={feedAge != null && feedAge < 60} />
            <Row label="STOCKS" value={(sys?.feed?.stock_symbols ?? []).join(" ") || "—"} />
            <Row label="OPTIONS" value={sys?.feed?.option_symbols ?? 0} />
            <Row label="SOURCES" value={Object.entries(sys?.feed?.sources ?? {}).map(([k, v]) => `${k}:${v}`).join(" ") || "—"} />
            <Row label="MARKET" value={sys?.broker?.market_clock?.known ? (sys.broker.market_clock.is_open ? "OPEN" : "CLOSED") : "unknown"} ok={!!sys?.broker?.market_clock?.is_open} warn={!sys?.broker?.market_clock?.known} />
          </Panel>
          <Panel title="ENFORCER">
            <Row label="MONITORS" value={sys?.enforcer?.monitors ?? "—"} ok={monitorsWithoutMid === 0} />
            <Row label="OPEN PLANS" value={summary?.open_plans ?? "—"} ok={(summary?.open_plans ?? 0) === (sys?.enforcer?.monitors ?? 0)} />
            <Row label="NO MID" value={monitorsWithoutMid} ok={monitorsWithoutMid === 0} />
            <Row label="PARKED" value={(sys?.enforcer?.parked_exits ?? []).length} ok={(sys?.enforcer?.parked_exits ?? []).length === 0} />
            <Row label="GHOST KEYS" value={sys?.enforcer?.ghost_keys ?? 0} ok={(sys?.enforcer?.ghost_keys ?? 0) === 0} />
            <Row label="RECONCILE" value={sys?.enforcer?.last_reconcile_age_s != null ? `${sys.enforcer.last_reconcile_age_s.toFixed(0)} s ago` : "—"} ok={(sys?.enforcer?.last_reconcile_age_s ?? 999) < 120} />
            {Object.entries(tasks).map(([name, state]) => (
              <Row key={name} label={name.toUpperCase()} value={state} ok={state === "running" || state === "finished"} warn={state === "finished"} />
            ))}
            {summary?.capabilities && (
              <Row label="CAPABILITIES" value={`L${summary.capabilities.derived?.options_level ?? "?"} · ${summary.capabilities.derived?.equity_shorts ? "shorts" : "long-only"} · ${summary.capabilities.level_provenance}`} />
            )}
          </Panel>
        </div>

        {/* ALPACA */}
        <Panel
          title={`CALLS · ${Object.entries(callStatus?.counts ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}${totalErrors ? ` · ${totalErrors} ERR` : ""}`}
          right={
            <span className="flex gap-px">
              {(["broker", "data", "engine", "all"] as const).map((c) => (
                <button key={c} onClick={() => setCategory(c)} className={"px-2 py-0.5 text-[10px] tracking-widest " + (category === c ? "bg-bb-amber text-black" : "text-bb-muted hover:text-bb-amber")}>
                  {c.toUpperCase()}
                </button>
              ))}
            </span>
          }
        >
          {summary?.last_error && (
            <div className="border-b border-bb-loss/60 bg-bb-loss/10 px-2 py-1 text-[10px] text-bb-loss">
              LAST ERROR {fmtClock(summary.last_error.ts)} · {summary.last_error.name} · {summary.last_error.detail}
            </div>
          )}
          <table className="w-full border-collapse text-[10px]">
            <tbody>
              {calls.map((c, i) => (
                <tr key={`${c.ts}-${i}`} className={"border-b border-bb-border/30 " + (c.ok ? "" : "bg-bb-loss/10")}>
                  <td className="w-16 px-2 py-0.5 text-bb-muted" data-numeric>{fmtClock(c.ts)}</td>
                  <td className="w-4 px-1 py-0.5"><Dot ok={c.ok} /></td>
                  <td className="w-12 px-1 py-0.5 text-bb-muted">{c.category}</td>
                  <td className="px-1 py-0.5 text-white">{c.name}</td>
                  <td className="px-1 py-0.5 text-bb-muted">{c.detail}</td>
                  <td className="w-14 px-2 py-0.5 text-right text-bb-muted" data-numeric>{c.ms != null ? `${c.ms.toFixed(0)} ms` : ""}</td>
                </tr>
              ))}
              {!calls.length && <tr><td className="px-2 py-4 text-center text-bb-muted">—</td></tr>}
            </tbody>
          </table>
        </Panel>

        {/* ENGINE FEED */}
        <div className="flex min-h-0 flex-col gap-px">
          <Panel title="JOURNAL" className="min-h-0 flex-1">
            <table className="w-full border-collapse text-[10px]">
              <tbody>
                {events.map((e, i) => (
                  <tr key={e.id ?? i} className={"border-b border-bb-border/30 " + (e.applied ? "" : "text-bb-muted")}>
                    <td className="w-16 px-2 py-0.5 text-bb-muted" data-numeric>{e.ts ? fmtClock(Date.parse(e.ts) / 1000) : ""}</td>
                    <td className="w-20 px-1 py-0.5 text-bb-amber">{(e.plan_id ?? "").slice(0, 8)}</td>
                    <td className="px-1 py-0.5 text-white">{e.event}</td>
                    <td className="px-1 py-0.5 text-bb-muted">{e.source} → {e.target ?? "·"}</td>
                    <td className="max-w-[16rem] truncate px-1 py-0.5 text-bb-muted" title={e.detail ?? ""}>{e.detail ?? ""}</td>
                  </tr>
                ))}
                {!events.length && <tr><td className="px-2 py-4 text-center text-bb-muted">—</td></tr>}
              </tbody>
            </table>
          </Panel>
          <Panel title={`LOG · ${sv?.log_file ?? "no file"}`} className="min-h-0 flex-1">
            <pre ref={logRef} className="h-full overflow-y-auto whitespace-pre-wrap break-all px-2 py-1 text-[10px] leading-[1.35] text-bb-muted">
              {logLines.map((l, i) => (
                <div key={i} className={/ERROR|Traceback|CRITICAL/.test(l) ? "text-bb-loss" : /WARNING/.test(l) ? "text-bb-orange" : ""}>{l}</div>
              ))}
            </pre>
          </Panel>
        </div>
      </div>
    </div>
  );
}
