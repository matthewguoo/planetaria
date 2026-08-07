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

function num(v: unknown): string {
  return typeof v === "number" ? String(v) : "—";
}

export default function SystemPage() {
  const state = useSystemState();
  const feeds = state?.signals?.feeds ?? {};
  const bus = state?.signals?.event_bus ?? {};
  // A lossless bus that has dropped anything means a consumer wedged and the
  // engine silently stopped seeing events. It is the loudest number here.
  const dropped = Number(bus.dropped ?? 0);

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
                const age = f.last_event_age_s;
                return (
                  <Row
                    key={name}
                    label={name.toUpperCase()}
                    ok={ok}
                    warn={warn}
                    value={
                      String(f.task) +
                      (typeof age === "number" ? ` · ${age.toFixed(0)}s ago` : "")
                    }
                    title={JSON.stringify(f, null, 1)}
                  />
                );
              })}
              <Row
                label="EVENT BUS"
                ok={dropped === 0}
                value={`${num(bus.published)} published · ${dropped} dropped`}
                title={
                  dropped === 0
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
