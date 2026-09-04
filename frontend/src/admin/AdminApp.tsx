/**
 * The server administration window: one dense screen per engine, meant to
 * sit open on the box's own display. Left: the process, its vitals (request
 * latency and throughput, broker call latency, the ticker cache) and the
 * stores. Middle: every exit monitor the enforcer runs (the automations on
 * this server) and every call to Alpaca and the data sources as it happens.
 * Right: the engine's own feed - the plan journal (or one plan's activity
 * when a monitor row is selected) and the log tail. No controls that move
 * money; this is the instrument panel, not the cockpit.
 */

import { useEffect, useRef, useState } from "react";
import {
  getAdminEvents,
  getAdminLog,
  getAdminPlanFeed,
  getAdminSummary,
  getAdminVitals,
  getMonitorCalls,
  type AdminMonitor,
  type AdminPlanFeed,
  type AdminSummary,
  type AdminVitals,
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

function fmtIn(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds <= 0) return "due";
  const m = Math.floor(seconds / 60);
  if (m < 90) return `${m}m`;
  const h = seconds / 3600;
  return h < 48 ? `${h.toFixed(1)}h` : `${(h / 24).toFixed(1)}d`;
}

const ms = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(0)} ms`);

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

/** One exit monitor: what it guards, whether it is alive, what it holds. */
function MonitorRow({ m, selected, onSelect }: { m: AdminMonitor; selected: boolean; onSelect: () => void }) {
  const state = m.task === "running" ? (m.health.startsWith("no-mid") ? "NO MID" : m.parked ? "PARKED" : "ARMED") : m.task.toUpperCase();
  return (
    <tr
      className={"cursor-pointer border-b border-bb-border/30 hover:bg-bb-hover " + (selected ? "bg-bb-amber/10" : "") + (m.ok ? "" : " bg-bb-loss/10")}
      onClick={onSelect}
      title={`${m.plan_id} · task ${m.task} · health ${m.health}`}
    >
      <td className="w-4 px-2 py-0.5"><Dot ok={m.ok} warn={m.parked} /></td>
      <td className="px-1 py-0.5 text-white">{m.label} <span className="text-bb-muted">×{m.qty}</span></td>
      <td className={"w-16 px-1 py-0.5 tracking-widest " + (m.ok ? "text-bb-profit" : "text-bb-loss")}>{state}</td>
      <td className="w-24 px-1 py-0.5 text-bb-muted" data-numeric>
        <span className="text-bb-profit">{m.tp != null ? Math.abs(m.tp).toFixed(2) : "—"}</span> / <span className="text-bb-loss">{m.sl != null ? Math.abs(m.sl).toFixed(2) : "—"}</span>
      </td>
      <td className="w-14 px-1 py-0.5 text-bb-muted" data-numeric>T {fmtIn(m.time_stop_in_s)}</td>
      <td className="w-14 px-1 py-0.5 text-right text-bb-muted" data-numeric>{m.beat_age_s != null ? `♥ ${m.beat_age_s.toFixed(0)}s` : ""}</td>
      <td className="w-16 px-1 py-0.5 text-right text-[9px] text-bb-muted">
        {m.tp_resting ? "TP@BRK " : ""}{m.partial ? "PARTIAL " : ""}{m.ghost_keys ? `GHOST ${m.ghost_keys}` : ""}
      </td>
    </tr>
  );
}

export function AdminApp() {
  const [summary, setSummary] = useState<AdminSummary | null>(null);
  const [vitals, setVitals] = useState<AdminVitals | null>(null);
  const [calls, setCalls] = useState<MonitorCall[]>([]);
  const [callStatus, setCallStatus] = useState<{ counts: Record<string, number>; errors: Record<string, number> } | null>(null);
  const [events, setEvents] = useState<AdminEvent[]>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [category, setCategory] = useState<"all" | "broker" | "data" | "engine">("broker");
  const [selected, setSelected] = useState<string | null>(null);
  const [feed, setFeed] = useState<AdminPlanFeed | null>(null);
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
      const v = await getAdminVitals();
      if (alive()) setVitals(v);
    } catch {
      /* the summary poll reports reachability */
    }
  }, 3_000);
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
  usePoll(async (alive) => {
    if (!selected) {
      setFeed(null);
      return;
    }
    try {
      const f = await getAdminPlanFeed(selected);
      if (alive()) setFeed(f);
    } catch {
      if (alive()) setFeed(null);
    }
  }, 3_000, [selected]);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logLines, feed]);

  const sv = summary?.server;
  const sys = summary?.system;
  const live = sv?.mode === "live_manual";
  const feedAge = sys?.feed?.stream_age_s ?? null;
  const tasks = sys?.tasks ?? {};
  const monitorsWithoutMid = Object.keys(sys?.enforcer?.monitors_without_mid ?? {}).length;
  const errors = callStatus?.errors ?? {};
  const totalErrors = Object.values(errors).reduce((a, b) => a + b, 0);
  const r60 = vitals?.requests.last_60s;
  const r5 = vitals?.requests.last_5m;
  const brk = vitals?.broker.broker;
  const dat = vitals?.broker.data;
  const cache = vitals?.cache;
  const monitors = vitals?.monitors ?? [];
  // Paper's strategy plane: one row per running instance (never built on live).
  type StrategyRun = { task: string; queue_size: number; events_seen: number; errors: number; last_event_age_s: number | null };
  const strategies = (sys as { strategies?: { running?: Record<string, StrategyRun> } } | undefined)?.strategies?.running ?? {};
  const strategyRows = Object.entries(strategies);
  const journal: AdminEvent[] = feed ? (feed.events as AdminEvent[]) : events;

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
        <span className="text-bb-muted">RESP <span data-numeric className="text-white">{ms(r60?.avg_ms)}</span> · <span data-numeric className="text-white">{r60?.rps != null ? `${r60.rps.toFixed(1)}/s` : "—"}</span></span>
        <span className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={!!sys?.db?.ok} /> DB</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={!!sys?.redis?.ok} /> REDIS</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={feedAge != null && feedAge < 60} warn={feedAge == null} /> FEED</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={sys?.broker?.account_status === "ACTIVE"} /> BROKER</span>
          <span className="flex items-center gap-1 text-bb-muted"><Dot ok={monitors.length > 0 && vitals?.monitors_ok === monitors.length} warn={monitors.length === 0} /> MONITORS {vitals ? `${vitals.monitors_ok}/${monitors.length}` : ""}</span>
          {!reachable && <span className="text-bb-loss">ENGINE UNREACHABLE</span>}
          <span data-numeric className="text-bb-muted">{new Date().toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false })} ET</span>
        </span>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1.2fr)] gap-px">
        {/* STATS */}
        <div className="flex min-h-0 flex-col gap-px overflow-y-auto">
          <Panel title="PROCESS" className="shrink-0">
            <Row label="CPU" value={summary?.process?.cpu_pct != null ? `${summary.process.cpu_pct.toFixed(1)}%` : "—"} ok={(summary?.process?.cpu_pct ?? 0) < 50} />
            <Row label="RSS" value={summary?.process?.rss_mb != null ? `${summary.process.rss_mb.toFixed(0)} MB` : "—"} />
            <Row label="TASKS / THREADS" value={`${summary?.process?.tasks ?? "—"} / ${summary?.process?.threads ?? "—"}`} />
            <Row label="PID" value={sv?.pid ?? "—"} />
            <Row label="DB" value={`${sys?.db?.engine ?? "—"} · ${sys?.db?.latency_ms != null ? `${sys.db.latency_ms} ms` : "—"}`} ok={!!sys?.db?.ok} />
            <Row label="REDIS" value={sys?.redis?.ok ? "ok" : "down"} ok={!!sys?.redis?.ok} />
          </Panel>
          <Panel title="VITALS" className="shrink-0">
            <Row label="RESPONSE 60s" value={r60 ? `avg ${ms(r60.avg_ms)} · p95 ${ms(r60.p95_ms)} · max ${ms(r60.max_ms)}` : "—"} ok={(r60?.p95_ms ?? 0) < 500} />
            <Row label="RESPONSE 5m" value={r5 ? `avg ${ms(r5.avg_ms)} · p95 ${ms(r5.p95_ms)} · ${r5.count} req` : "—"} />
            <Row label="THROUGHPUT" value={r60 ? `${r60.rps?.toFixed(2) ?? "—"} req/s · ${r5?.rps?.toFixed(2) ?? "—"} /s over 5m` : "—"} />
            <Row label="ERRORS" value={r5 ? `${r5.errors_5xx} × 5xx · ${r5.errors_4xx} × 4xx (5m) · ${vitals?.requests.errors_5xx_total ?? 0} total` : "—"} ok={(r5?.errors_5xx ?? 0) === 0} />
            <Row label="ADMIN POLLS" value={vitals ? `${vitals.requests.admin_last_60s.rps?.toFixed(1) ?? "—"} /s · avg ${ms(vitals.requests.admin_last_60s.avg_ms)}` : "—"} />
            <Row label="BROKER" value={brk ? `avg ${ms(brk.avg_ms)} · p95 ${ms(brk.p95_ms)} · ${brk.per_min ?? "—"}/min` : "—"} ok={(brk?.errors ?? 0) === 0 && (brk?.p95_ms ?? 0) < 2000} />
            <Row label="BROKER SLOWEST" value={brk?.slowest?.length ? brk.slowest.map((s) => `${s.name} ${s.ms.toFixed(0)}`).join(" · ") : "—"} cls="text-bb-muted" />
            <Row label="DATA SOURCES" value={dat ? `avg ${ms(dat.avg_ms)} · ${dat.per_min ?? "—"}/min · ${dat.errors} err` : "—"} ok={(dat?.errors ?? 0) === 0} />
            {(vitals?.requests.top_routes_5m ?? []).slice(0, 4).map((t) => (
              <Row key={t.route} label={t.route.replace(/^GET /, "").slice(0, 18)} value={`${t.count} · avg ${ms(t.avg_ms)} · max ${ms(t.max_ms)}`} cls="text-bb-muted" />
            ))}
          </Panel>
          <Panel title={`CACHE · ${cache?.tickers_cached ?? "—"} TICKERS · ${cache?.bars_1m_total?.toLocaleString() ?? "—"} BARS`} className="shrink-0">
            <Row label="LOAD TIME" value={cache ? `avg ${ms(cache.backfill_avg_ms)} · max ${ms(cache.backfill_max_ms)}` : "—"} ok={(cache?.backfill_max_ms ?? 0) < 15_000} />
            <Row label="CONTRACTS" value={cache?.contracts ? `${cache.contracts.cached} cached · ${cache.contracts.inflight} in flight` : "—"} />
            <Row label="CHAINS" value={cache?.chains ? `${cache.chains.cached} cached` : "—"} />
            {(cache?.tickers ?? []).map((t) => (
              <Row
                key={t.symbol}
                label={t.symbol}
                value={`${t.bars_1m.toLocaleString()} bars · ${t.backfill_ms != null ? `${t.backfill_ms} ms` : t.loading ? "loading" : "—"}${t.subscribed ? " · stream" : ""}${t.quote ? " · quote" : ""}`}
                ok={!t.loading}
                warn={t.loading}
                cls="text-bb-muted"
              />
            ))}
          </Panel>
          <Panel title="FEED" className="shrink-0">
            <Row label="STREAM AGE" value={feedAge != null ? `${feedAge.toFixed(1)} s` : "no stream"} ok={feedAge != null && feedAge < 60} />
            <Row label="STOCKS" value={(sys?.feed?.stock_symbols ?? []).join(" ") || "—"} />
            <Row label="OPTIONS" value={sys?.feed?.option_symbols ?? 0} />
            <Row label="MARKET" value={sys?.broker?.market_clock?.known ? (sys.broker.market_clock.is_open ? "OPEN" : "CLOSED") : "unknown"} ok={!!sys?.broker?.market_clock?.is_open} warn={!sys?.broker?.market_clock?.known} />
          </Panel>
          <Panel title="ENFORCER" className="shrink-0">
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

        {/* MONITORS + CALLS */}
        <div className="flex min-h-0 flex-col gap-px">
          <Panel
            title={`MONITORS · ${monitors.length} ${live ? "ON LIVE" : "ON PAPER"}${strategyRows.length ? ` · ${strategyRows.length} STRATEGIES` : ""}`}
            className="max-h-[45%] shrink-0"
            right={selected && (
              <button className="px-2 text-[10px] tracking-widest text-bb-amber" onClick={() => setSelected(null)}>ALL PLANS ✕</button>
            )}
          >
            <table className="w-full border-collapse text-[10px]">
              <tbody>
                {monitors.map((m) => (
                  <MonitorRow key={m.plan_id} m={m} selected={selected === m.plan_id} onSelect={() => setSelected(selected === m.plan_id ? null : m.plan_id)} />
                ))}
                {!monitors.length && <tr><td colSpan={7} className="px-2 py-3 text-center text-bb-muted">no open plans · nothing to enforce</td></tr>}
                {strategyRows.map(([name, st]) => (
                  <tr key={name} className="border-b border-bb-border/30" title={`strategy instance ${name}`}>
                    <td className="w-4 px-2 py-0.5"><Dot ok={st.task === "running" && st.errors === 0} warn={st.task === "running"} /></td>
                    <td className="px-1 py-0.5 text-white">{name} <span className="text-bb-muted">strategy</span></td>
                    <td className={"w-16 px-1 py-0.5 tracking-widest " + (st.task === "running" ? "text-bb-profit" : "text-bb-loss")}>{st.task.toUpperCase()}</td>
                    <td className="px-1 py-0.5 text-bb-muted" colSpan={4} data-numeric>
                      {st.events_seen} events · queue {st.queue_size} · {st.errors} err · last {st.last_event_age_s != null ? `${st.last_event_age_s.toFixed(0)}s ago` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
          <Panel
            title={`CALLS · ${Object.entries(callStatus?.counts ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}${totalErrors ? ` · ${totalErrors} ERR` : ""}`}
            className="min-h-0 flex-1"
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
        </div>

        {/* ENGINE FEED */}
        <div className="flex min-h-0 flex-col gap-px">
          <Panel title={feed ? `PLAN ${feed.plan.underlying} · ${feed.plan.id.slice(0, 8)} · ${feed.plan.status.toUpperCase()}` : "JOURNAL"} className="min-h-0 flex-1">
            {feed && (
              <div className="border-b border-bb-border/40 px-2 py-1 text-[10px] text-bb-muted" data-numeric>
                entry {feed.plan.fill_premium ?? feed.plan.entry_limit} · TP {feed.plan.tp_premium ?? "—"} · SL {feed.plan.sl_premium ?? "—"} · time stop {feed.plan.time_stop_utc ? fmtClock(Date.parse(feed.plan.time_stop_utc) / 1000) : "—"}
                {feed.plan.exit_reason ? ` · exit ${feed.plan.exit_reason}` : ""}{feed.plan.realized_pnl != null ? ` · realized ${feed.plan.realized_pnl.toFixed(0)}` : ""}
              </div>
            )}
            <table className="w-full border-collapse text-[10px]">
              <tbody>
                {journal.map((e, i) => (
                  <tr key={e.id ?? i} className={"border-b border-bb-border/30 " + (e.applied ? "" : "text-bb-muted")}>
                    <td className="w-16 px-2 py-0.5 text-bb-muted" data-numeric>{e.ts ? fmtClock(Date.parse(e.ts) / 1000) : ""}</td>
                    {!feed && <td className="w-20 px-1 py-0.5 text-bb-amber">{(e.plan_id ?? "").slice(0, 8)}</td>}
                    <td className="px-1 py-0.5 text-white">{e.event}</td>
                    <td className="px-1 py-0.5 text-bb-muted">{e.source} → {e.target ?? "·"}</td>
                    <td className="max-w-[16rem] truncate px-1 py-0.5 text-bb-muted" title={e.detail ?? ""}>{e.detail ?? ""}</td>
                  </tr>
                ))}
                {!journal.length && <tr><td className="px-2 py-4 text-center text-bb-muted">—</td></tr>}
              </tbody>
            </table>
          </Panel>
          <Panel title={feed ? `LOG · plan ${feed.plan.id.slice(0, 8)} · ${feed.log.length} lines` : `LOG · ${sv?.log_file ?? "no file"}`} className="min-h-0 flex-1">
            <pre ref={logRef} className="h-full overflow-y-auto whitespace-pre-wrap break-all px-2 py-1 text-[10px] leading-[1.35] text-bb-muted">
              {(feed ? feed.log : logLines).map((l, i) => (
                <div key={i} className={/ERROR|Traceback|CRITICAL/.test(l) ? "text-bb-loss" : /WARNING/.test(l) ? "text-bb-orange" : ""}>{l}</div>
              ))}
            </pre>
          </Panel>
        </div>
      </div>
    </div>
  );
}
