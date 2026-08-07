/**
 * SYSTEM — the tech-stats page. Equal billing with ACCOUNT and STRATEGIES,
 * because an engine that trades unattended fails silently by default: a dead
 * feed looks exactly like a quiet market, and a wedged consumer looks exactly
 * like no signals.
 *
 * Everything here answers "is the machine actually doing what it thinks it
 * is". Health of every subsystem, the state of every background task, the
 * signal feeds and the bus that carries them, and the live call flow — what
 * the process is saying to whom, right now.
 *
 * The blocks are shared with the options terminal's ⚙ drawer (SystemPanels).
 */

import { CallFlow } from "../Monitor/MonitorPage";
import { FeedSettingsPanel, HealthPanel, Row, useSystemState } from "./SystemPanels";

function Panel({
  title,
  hint,
  children,
  className = "",
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={"panel flex min-h-0 flex-col " + className}>
      <div className="panel-title" title={hint}>
        {title}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}

/** A background task is either "running" or it is a problem. `finished` is
 * included in that: a loop that returned is a loop that stopped working. */
function taskOk(state: string): { ok: boolean; warn: boolean } {
  if (state === "running") return { ok: true, warn: false };
  if (state === "not started") return { ok: false, warn: true };
  return { ok: false, warn: false };
}

/** The bus reports per EVENT TYPE, each with its own published count and one
 * entry per subscriber queue:
 *
 *   { estimate: { published, subscribers: [{ size, max, dropped, lossless }] } }
 *
 * so both totals have to be summed across the whole shape. Reading a flat
 * `bus.dropped` returns undefined and renders as a reassuring zero — which is
 * the precise failure this page exists to catch, so it is worth the loop. */
function busTotals(bus: Record<string, unknown>): { published: number; dropped: number; queued: number } {
  let published = 0;
  let dropped = 0;
  let queued = 0;
  for (const entry of Object.values(bus)) {
    const e = entry as { published?: number; subscribers?: { size?: number; dropped?: number }[] };
    published += e?.published ?? 0;
    for (const sub of e?.subscribers ?? []) {
      dropped += sub?.dropped ?? 0;
      queued += sub?.size ?? 0;
    }
  }
  return { published, dropped, queued };
}

/** Feeds do not agree on what to call their age field — the news stream has
 * `last_event_age_s`, EDGAR has `last_poll_age_s`, the calendar has
 * `last_fetch_age_s`. Take whichever is present rather than showing nothing
 * for two of the three. */
function feedAge(f: Record<string, unknown>): number | null {
  for (const key of ["last_event_age_s", "last_poll_age_s", "last_fetch_age_s"]) {
    const v = f[key];
    if (typeof v === "number") return v;
  }
  return null;
}

export default function SystemPage() {
  const state = useSystemState();
  const feeds = state?.signals?.feeds ?? {};
  // A lossless bus that has dropped anything means a consumer wedged and the
  // engine silently stopped seeing events. It is the loudest number here.
  const bus = busTotals(state?.signals?.event_bus ?? {});

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <div className="grid shrink-0 gap-px lg:grid-cols-3">
        <Panel title="HEALTH" hint="every subsystem the engine depends on">
          <HealthPanel state={state} />
        </Panel>

        <Panel title="BACKGROUND TASKS" hint="anything not 'running' is a problem, including 'finished'">
          {!state ? (
            <div className="px-2 py-3 text-[11px] text-bb-muted">loading…</div>
          ) : (
            Object.entries(state.tasks).map(([name, value]) => {
              const { ok, warn } = taskOk(value);
              return (
                <Row
                  key={name}
                  label={name.replace(/_/g, " ").toUpperCase()}
                  ok={ok}
                  warn={warn}
                  value={value}
                />
              );
            })
          )}
        </Panel>

        <Panel title="SIGNAL FEEDS" hint="a dead feed shows as a task state and a climbing event age, never as quiet non-trading">
          {!state ? (
            <div className="px-2 py-3 text-[11px] text-bb-muted">loading…</div>
          ) : Object.keys(feeds).length === 0 ? (
            <div className="px-2 py-3 text-[11px] text-bb-muted">no feeds registered</div>
          ) : (
            <>
              {Object.entries(feeds).map(([name, f]) => {
                const { ok, warn } = taskOk(String(f.task));
                const age = feedAge(f);
                const errors = typeof f.errors === "number" ? f.errors : 0;
                return (
                  <Row
                    key={name}
                    label={name.toUpperCase()}
                    ok={ok && errors === 0}
                    warn={warn || (ok && errors > 0)}
                    value={
                      String(f.task) +
                      (age != null ? ` · ${age.toFixed(0)}s ago` : "") +
                      (errors ? ` · ${errors} err` : "")
                    }
                    title={JSON.stringify(f, null, 1)}
                  />
                );
              })}
              <Row
                label="EVENT BUS"
                ok={bus.dropped === 0}
                value={
                  `${bus.published} published · ${bus.dropped} dropped` +
                  (bus.queued ? ` · ${bus.queued} queued` : "")
                }
                title={
                  bus.dropped === 0
                    ? "Lossless. A non-zero drop count means a consumer wedged."
                    : "A consumer wedged — events were published that nobody received."
                }
              />
            </>
          )}
        </Panel>
      </div>

      <div className="panel-title shrink-0" title="what the process is saying to whom, right now">
        CALL FLOW
      </div>
      <div className="flex h-80 shrink-0">
        <CallFlow />
      </div>

      <div className="grid shrink-0 gap-px lg:grid-cols-2">
        <Panel title="FEED / API SETTINGS">
          <FeedSettingsPanel />
        </Panel>
        <Panel title="ELSEWHERE">
          <a
            className="flex items-center justify-between border-b border-bb-border/40 px-2 py-2 text-[11px] text-bb-amber hover:bg-bb-hover"
            href="/terminal.html"
            title="The discretionary 0-3 DTE options cockpit: chart, chain, payoff designer, order ticket"
          >
            <span>OPTIONS TERMINAL</span>
            <span className="text-bb-muted">/terminal.html ↗</span>
          </a>
          <div className="px-2 py-2 text-[9px] leading-relaxed text-bb-muted">
            Manual trading lives in its own app now. It shares this backend,
            these accounts and the same exit enforcer — it is a different front
            end, not a different engine.
          </div>
        </Panel>
      </div>
    </div>
  );
}
