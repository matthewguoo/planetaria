/**
 * Strategy runtime management: the thin control surface over the backend
 * StrategyRunner. Everything here calls the same /api/strategies control
 * plane the headless engine exposes — the page holds no privileged path.
 * Poll-based (house pattern): instances + signals + selected journal ~5s.
 */

import { useCallback, useEffect, useState } from "react";
import {
  apiError,
  createStrategy,
  getStrategyCatalog,
  getStrategyPerformance,
  getStrategySource,
  getStrategyTwin,
  type Allocation,
  type Capabilities,
  type CircuitBreaker,
  type LadderState,
  type RegisteredBlock,
  type SignalRecord,
  type StrategyDecision,
  type StrategyInstance,
  type StrategyKind,
  type StrategyPerformance,
  type StrategyTwin,
} from "../../lib/api";
import { dateTimeShort, fmtUsd, hhmmss, pnlCls } from "../../lib/format";
import {
  actions,
  decisionColor,
  decisionSummary,
  runtimeSummary,
  stateColor,
  useStrategyRunnerStore,
} from "../../store/strategyRunnerStore";

const POLL_MS = 5_000;

/** A capability a strategy declares it needs, coloured by whether the
 * platform currently provides it. The point is not decoration: an earnings
 * strategy journalling "no fresh quote" every night because the account has
 * no SIP entitlement looks identical to a quiet market without this. */
function RequirementBadges({
  requires,
  capabilities,
}: {
  requires: string[];
  capabilities: Capabilities | null;
}) {
  if (!requires.length) return null;
  return (
    <span className="ml-2 inline-flex gap-1 align-middle">
      {requires.map((cap) => {
        const state = capabilities?.[cap];
        const met = state?.met ?? true;   // unknown reads as neutral, not broken
        return (
          <span
            key={cap}
            title={
              state
                ? `${met ? "available" : "NOT AVAILABLE"} — ${state.detail}`
                : `this strategy requires ${cap}`
            }
            className={
              "border px-1 text-[9px] uppercase tracking-wider " +
              (state == null
                ? "border-bb-border text-bb-muted"
                : met
                  ? "border-bb-profit/60 text-bb-profit"
                  : "border-bb-loss text-bb-loss")
            }
          >
            {cap}
            {state && !met ? " ✕" : ""}
          </span>
        );
      })}
    </span>
  );
}

export default function StrategiesPage() {
  const instances = useStrategyRunnerStore((s) => s.instances);
  const decisions = useStrategyRunnerStore((s) => s.decisions);
  const signals = useStrategyRunnerStore((s) => s.signals);
  const selectedId = useStrategyRunnerStore((s) => s.selectedId);
  const loaded = useStrategyRunnerStore((s) => s.loaded);
  const refresh = useStrategyRunnerStore((s) => s.refresh);
  const select = useStrategyRunnerStore((s) => s.select);
  const [error, setError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

  // Capabilities move only on a restart or a settings change, so they are
  // fetched once rather than on the instance poll.
  useEffect(() => {
    getStrategyCatalog()
      .then((c) => setCapabilities(c.capabilities))
      .catch(() => setCapabilities(null));
  }, []);

  const run = useCallback(async (p: Promise<string | null>) => {
    setError(await p);
  }, []);

  const selected = instances.find((i) => i.id === selectedId) ?? null;
  const enabled = instances.filter((i) => i.state === "enabled").length;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      {error && (
        <div className="flex items-center justify-between border border-bb-loss bg-bb-loss/20 px-2 py-1 text-[11px] text-bb-loss">
          <span>{error}</span>
          <button className="px-1 hover:bg-bb-loss hover:text-black" onClick={() => setError(null)}>
            ✕
          </button>
        </div>
      )}

      <div className="panel flex shrink-0 items-center justify-between px-2 py-1">
        <div className="flex items-center gap-3 text-[11px]">
          <span className="tracking-widest text-bb-muted">STRATEGY RUNTIME</span>
          <span className="text-bb-amber" data-numeric>
            {enabled}/{instances.length} enabled
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            title="Pause every enabled strategy. Open plans stay managed by the exit enforcer."
            className="border border-bb-orange px-1.5 text-[10px] text-bb-orange hover:bg-bb-orange hover:text-black"
            onClick={() => void run(actions.killAll(false))}
          >
            KILL ALL
          </button>
          <button
            title="Pause every strategy AND close all their open plans via the enforcer."
            className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
            onClick={() => {
              if (window.confirm("Kill all strategies and flatten their open plans?"))
                void run(actions.killAll(true));
            }}
          >
            KILL + FLATTEN
          </button>
        </div>
      </div>

      <div className="panel flex shrink-0 flex-col">
        <div className="panel-title">INSTANCES ({instances.length})</div>
        {instances.length === 0 ? (
          <div className="flex h-16 items-center justify-center text-[11px] text-bb-muted">
            {loaded ? "no strategy instances — create one below" : "loading…"}
          </div>
        ) : (
          <table className="w-full border-collapse text-[11px]">
            <thead className="text-[10px] text-bb-muted">
              <tr className="border-b border-bb-border">
                <th className="px-2 py-1 text-left">NAME</th>
                <th className="px-2 py-1 text-left">KIND</th>
                <th className="px-2 py-1 text-left">STATE</th>
                <th className="px-2 py-1 text-right" title="capital this strategy may deploy">
                  ALLOCATION
                </th>
                <th
                  className="px-2 py-1 text-right"
                  title="drawdown against the circuit breaker that would flatten it"
                >
                  DRAWDOWN
                </th>
                <th className="px-2 py-1 text-right">PLANS</th>
                <th className="px-2 py-1 text-left">RUNTIME</th>
                <th className="px-2 py-1 text-right">ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {instances.map((inst) => (
                <InstanceRow
                  key={inst.id}
                  inst={inst}
                  capabilities={capabilities}
                  selected={inst.id === selectedId}
                  onSelect={() => select(inst.id === selectedId ? null : inst.id)}
                  onAction={run}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selected && <CapitalPanel key={`cap-${selected.id}`} inst={selected} onAction={run} />}
      {selected && <ParamsPanel key={selected.id} inst={selected} onAction={run} />}
      {selected && <CommandPanel key={`cmd-${selected.id}`} inst={selected} onAction={run} />}
      {selected && <PerformancePanel key={`perf-${selected.id}`} inst={selected} />}
      {selected && <TwinPanel key={`twin-${selected.id}`} inst={selected} />}
      {selected && <DecisionsPanel inst={selected} decisions={decisions} />}
      {selected && <SourcePanel key={`src-${selected.kind}`} kind={selected.kind} />}

      <CreatePanel onAction={run} />
      <SignalsPanel signals={signals} />
    </div>
  );
}

function InstanceRow({
  inst,
  capabilities,
  selected,
  onSelect,
  onAction,
}: {
  inst: StrategyInstance;
  capabilities: Capabilities | null;
  selected: boolean;
  onSelect: () => void;
  onAction: (p: Promise<string | null>) => void;
}) {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  const cap = inst.capital;
  const bs = cap?.breaker;
  return (
    <tr
      className={
        "cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover " +
        (selected ? "bg-bb-orange/5" : "")
      }
      onClick={onSelect}
      title="Click to inspect params and the decision journal"
    >
      <td className="px-2 py-1 text-bb-amber">
        {inst.name}
        <RequirementBadges requires={inst.requires ?? []} capabilities={capabilities} />
      </td>
      <td className="px-2 py-1 text-bb-muted">{inst.kind}</td>
      <td className={"px-2 py-1 " + stateColor(inst.state, inst.runtime?.task ?? "")}>
        {inst.state}
        {inst.runtime?.task?.startsWith("DEAD") ? " (task dead)" : ""}
      </td>
      <td
        className="px-2 py-1 text-right"
        data-numeric
        title={
          cap
            ? `${fmtUsd(cap.deployed)} deployed of ${fmtUsd(cap.allocated)} allocated`
            : ""
        }
      >
        {cap ? fmtUsd(cap.allocated) : "—"}
      </td>
      <td
        className={
          "px-2 py-1 text-right " +
          (bs?.tripped ? "text-bb-loss" : bs && bs.drawdown > 0 ? "text-bb-orange" : "text-bb-muted")
        }
        data-numeric
        title={
          bs
            ? `drawdown ${fmtUsd(bs.drawdown)} of a ${fmtUsd(bs.limit)} limit`
            : ""
        }
      >
        {bs ? (bs.tripped ? "TRIPPED" : `${fmtUsd(bs.drawdown)}/${fmtUsd(bs.limit)}`) : "—"}
      </td>
      <td className="px-2 py-1 text-right" data-numeric>
        {inst.open_plans}
      </td>
      <td className="px-2 py-1 text-bb-muted">{runtimeSummary(inst.runtime)}</td>
      <td className="px-2 py-1 text-right">
        <div className="flex justify-end gap-1" onClick={stop}>
          {inst.state === "enabled" ? (
            <button
              className="border border-bb-orange px-1.5 text-[10px] text-bb-orange hover:bg-bb-orange hover:text-black"
              onClick={() => void onAction(actions.pause(inst.id))}
            >
              PAUSE
            </button>
          ) : (
            <button
              className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black"
              onClick={() => void onAction(actions.enable(inst.id))}
            >
              ENABLE
            </button>
          )}
          <button
            title="Pause this instance and close its open plans via the enforcer"
            className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black disabled:opacity-30"
            disabled={inst.open_plans === 0 && inst.state !== "enabled"}
            onClick={() => {
              if (window.confirm(`Flatten ${inst.name}: pause + close ${inst.open_plans} plan(s)?`))
                void onAction(actions.flatten(inst.id));
            }}
          >
            FLATTEN
          </button>
          <button
            title="Delete this instance and its decision journal. Refused while it holds open plans."
            className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
            onClick={() => {
              if (
                window.confirm(
                  `Delete ${inst.name} permanently?\n\nIts decision journal goes with it — ` +
                    "that history is not recoverable.",
                )
              )
                void onAction(actions.remove(inst.id));
            }}
          >
            DELETE
          </button>
        </div>
      </td>
    </tr>
  );
}

function ParamsPanel({
  inst,
  onAction,
}: {
  inst: StrategyInstance;
  onAction: (p: Promise<string | null>) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(inst.params, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        PARAMS — {inst.name.toUpperCase()}
      </div>
      <textarea
        className="h-40 w-full border-0 bg-black p-2 font-mono text-[11px] text-bb-amber outline-none"
        spellCheck={false}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setParseError(null);
        }}
      />
      <div className="flex items-center justify-between border-t border-bb-border px-2 py-1">
        <span className="text-[10px] text-bb-loss">{parseError ?? ""}</span>
        <button
          className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black"
          onClick={() => {
            try {
              const params = JSON.parse(text) as Record<string, unknown>;
              void onAction(actions.saveParams(inst.id, params));
            } catch (err) {
              setParseError(`invalid JSON: ${String(err)}`);
            }
          }}
        >
          SAVE PARAMS
        </button>
      </div>
    </div>
  );
}

/**
 * Operator commands. This replaced the TRIGGER button, which posted an empty
 * payload — for an autonomous strategy that is a no-op that looks like an
 * action, and it filled the signals feed with `manual`/`api-trigger` rows
 * that did nothing.
 *
 * Strategies are autonomous; this is not a trading path. It exists for the
 * commands a strategy genuinely understands, like the flagship's late-boot
 * watchlist salvage after an engine restart past 15:30 ET.
 */
function CommandPanel({
  inst,
  onAction,
}: {
  inst: StrategyInstance;
  onAction: (p: Promise<string | null>) => void;
}) {
  const [text, setText] = useState('{"cmd": "build_watchlist"}');
  const [parseError, setParseError] = useState<string | null>(null);
  const disabled = inst.state !== "enabled";

  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        COMMAND — {inst.name.toUpperCase()}
      </div>
      <div className="flex items-center gap-2 px-2 py-2">
        <input
          className="min-w-0 flex-1 border border-bb-border bg-black px-1 py-0.5 font-mono text-[11px] text-bb-amber outline-none focus:border-bb-amber"
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setParseError(null);
          }}
          aria-label="Command payload"
        />
        <button
          className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black disabled:opacity-30"
          disabled={disabled}
          title={disabled ? "the instance must be enabled to receive commands" : ""}
          onClick={() => {
            try {
              const payload = JSON.parse(text) as Record<string, unknown>;
              void onAction(actions.trigger(inst.id, payload));
            } catch (err) {
              setParseError(`invalid JSON: ${String(err)}`);
            }
          }}
        >
          SEND
        </button>
      </div>
      {parseError && (
        <div className="px-2 pb-2 text-[10px] text-bb-loss">{parseError}</div>
      )}
    </div>
  );
}

function DecisionsPanel({
  inst,
  decisions,
}: {
  inst: StrategyInstance;
  decisions: StrategyDecision[];
}) {
  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        DECISION JOURNAL — {inst.name.toUpperCase()} ({decisions.length})
      </div>
      {decisions.length === 0 ? (
        <div className="flex h-12 items-center justify-center text-[11px] text-bb-muted">
          no decisions journaled yet
        </div>
      ) : (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <tbody>
              {decisions.map((d) => (
                <tr key={d.id} className="border-b border-bb-border/50 hover:bg-bb-hover">
                  <td className="whitespace-nowrap px-2 py-1 text-bb-muted" data-numeric>
                    {hhmmss(d.ts)}
                  </td>
                  <td className={"whitespace-nowrap px-2 py-1 " + decisionColor(d.action)}>
                    {d.action}
                  </td>
                  <td className="px-2 py-1 text-bb-muted" title={JSON.stringify(d.detail)}>
                    {decisionSummary(d)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 text-right text-[10px] text-bb-muted">
                    {d.signal_ids?.length ? `sig ${d.signal_ids.join(",")}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/**
 * The pre-reg ladder as a progress bar: the registered band — FROZEN at the
 * pre-registration commit — against the instance's own record on the SAME
 * metric. Deliberately absent: a live Sharpe. At ladder sample sizes a
 * Sharpe is noise with a confidence interval wider than itself, and an
 * early scary/euphoric one beside a frozen backtest number is goal-pressure
 * by UI (docs/briefs/fund-capital-scheduling.md §7).
 */
function RegistrationPanel({
  name,
  registered,
  ladder,
}: {
  name: string;
  registered: RegisteredBlock | null;
  ladder: LadderState | null;
}) {
  if (!registered || !ladder) {
    return (
      <div className="panel flex shrink-0 flex-col">
        <div className="panel-title">PRE-REGISTRATION — {name.toUpperCase()}</div>
        <div
          className="px-2 py-2 text-[11px] text-bb-orange"
          title="Write docs/pre-registration-<kind>.md and freeze it on the strategy class before this kind ever goes live."
        >
          UNREGISTERED — this kind carries no frozen expectation band
        </div>
      </div>
    );
  }
  const progress =
    ladder.target_sessions != null && ladder.target_sessions > 0
      ? Math.min(ladder.sessions_observed / ladder.target_sessions, 1)
      : null;
  const bandCls =
    ladder.band_status === "in"
      ? "text-bb-profit"
      : ladder.band_status === "below"
        ? "text-bb-loss"
        : ladder.band_status === "above"
          ? "text-bb-orange"
          : "text-bb-muted";
  const [lo, hi] = registered.band;
  return (
    <div className="panel flex shrink-0 flex-col">
      <div
        className="panel-title"
        title={`${registered.doc} @ ${registered.registered_commit} — frozen; live results never edit it`}
      >
        PRE-REGISTRATION — {name.toUpperCase()}
      </div>
      <div className="flex flex-col gap-1 px-2 py-1.5 text-[11px]">
        <div className="flex items-center gap-4" data-numeric>
          <span className="text-bb-muted">
            stage <span className="text-bb-amber">{ladder.stage}</span>
          </span>
          <span
            className="text-bb-muted"
            title="ET dates with journal or plan activity — the gate sample, not a score"
          >
            sessions{" "}
            <span className="text-bb-amber">
              {ladder.sessions_observed}
              {ladder.target_sessions != null ? `/${ladder.target_sessions}` : ""}
            </span>
          </span>
          <span className="text-bb-muted">
            closed <span className="text-bb-amber">{ladder.trades_closed}</span>
          </span>
          <span className="text-bb-muted">
            band{" "}
            <span className="text-white">
              {lo}–{hi} {ladder.metric_label ?? registered.metric_label}
            </span>
          </span>
          <span className="text-bb-muted">
            running{" "}
            <span className={bandCls}>
              {!ladder.metric_computed
                ? "not yet measured by the engine"
                : ladder.running_metric == null
                  ? "no closed trades yet"
                  : `${ladder.running_metric > 0 ? "+" : ""}${ladder.running_metric.toFixed(1)} (${ladder.band_status})`}
            </span>
          </span>
        </div>
        {progress != null && (
          <div className="h-1.5 w-full bg-bb-border/40" title="sessions toward this stage's gate">
            <div className="h-full bg-bb-amber" style={{ width: `${progress * 100}%` }} />
          </div>
        )}
        <div
          className="text-[10px] text-bb-muted"
          title={`evidence: ${registered.source_note}`}
        >
          backtest {registered.backtest.value > 0 ? "+" : ""}
          {registered.backtest.value} ({registered.backtest.basis}) · window{" "}
          {registered.window} · costs {registered.costs_assumed}
        </div>
      </div>
    </div>
  );
}

function PerformancePanel({ inst }: { inst: StrategyInstance }) {
  const [perf, setPerf] = useState<StrategyPerformance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getStrategyPerformance(inst.id)
        .then((p) => alive && setPerf(p))
        .catch((err) => alive && setError(apiError(err)));
    load();
    const t = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [inst.id]);

  return (
    <>
      {perf && (
        <RegistrationPanel
          name={inst.name}
          registered={perf.registered}
          ladder={perf.ladder}
        />
      )}
      <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        PERFORMANCE — {inst.name.toUpperCase()}
      </div>
      {error ? (
        <div className="px-2 py-2 text-[11px] text-bb-loss">{error}</div>
      ) : !perf ? (
        <div className="px-2 py-2 text-[11px] text-bb-muted">loading…</div>
      ) : (
        <>
          <div className="flex items-center gap-4 px-2 py-1 text-[11px]" data-numeric>
            <span className="text-bb-muted">
              open <span className="text-bb-amber">{perf.open}</span>
            </span>
            <span className="text-bb-muted">
              closed <span className="text-bb-amber">{perf.closed}</span>
            </span>
            <span className="text-bb-muted">
              win rate{" "}
              <span className="text-bb-amber">
                {perf.win_rate == null ? "—" : `${Math.round(perf.win_rate * 100)}%`}
              </span>
            </span>
            <span className="text-bb-muted">
              realized{" "}
              <span className={pnlCls(perf.realized_pnl)}>
                {perf.realized_pnl.toFixed(2)}
              </span>
            </span>
            <span className="text-bb-muted">
              avg win{" "}
              <span className={pnlCls(perf.avg_win)}>
                {perf.avg_win == null ? "—" : perf.avg_win.toFixed(2)}
              </span>
            </span>
            <span className="text-bb-muted">
              avg loss{" "}
              <span className={pnlCls(perf.avg_loss)}>
                {perf.avg_loss == null ? "—" : perf.avg_loss.toFixed(2)}
              </span>
            </span>
          </div>
          {perf.plans.length === 0 ? (
            <div className="flex h-10 items-center justify-center text-[11px] text-bb-muted">
              no plans yet
            </div>
          ) : (
            <div className="max-h-48 overflow-y-auto">
              <table className="w-full border-collapse text-[11px]">
                <thead className="text-[10px] text-bb-muted">
                  <tr className="border-b border-bb-border">
                    <th className="px-2 py-1 text-left">TIME</th>
                    <th className="px-2 py-1 text-left">SYMBOL</th>
                    <th className="px-2 py-1 text-left">STATUS</th>
                    <th className="px-2 py-1 text-right">QTY</th>
                    <th className="px-2 py-1 text-right">ENTRY</th>
                    <th className="px-2 py-1 text-right">EXIT</th>
                    <th className="px-2 py-1 text-right">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {perf.plans.map((p) => (
                    <tr key={p.id} className="border-b border-bb-border/50 hover:bg-bb-hover">
                      <td className="whitespace-nowrap px-2 py-1 text-bb-muted" data-numeric>
                        {dateTimeShort(p.created_at)}
                      </td>
                      <td className="px-2 py-1 text-bb-amber">{p.underlying}</td>
                      <td className="px-2 py-1 text-bb-muted">{p.status}</td>
                      <td className="px-2 py-1 text-right" data-numeric>
                        {p.qty}
                      </td>
                      <td className="px-2 py-1 text-right" data-numeric>
                        {p.entry_limit?.toFixed(2) ?? "-"}
                      </td>
                      <td className="px-2 py-1 text-right" data-numeric>
                        {p.exit_premium == null ? "—" : p.exit_premium.toFixed(2)}
                      </td>
                      <td className={"px-2 py-1 text-right " + pnlCls(p.realized_pnl)} data-numeric>
                        {p.realized_pnl == null ? "—" : p.realized_pnl.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      </div>
    </>
  );
}

/**
 * CAPITAL — the two dials that decide how much this strategy can lose.
 *
 * `allocation` is the universe the runner sees: ctx.account() reports it as
 * the strategy's equity, so the strategy sizes against its own book rather
 * than the account, and execute_intent refuses anything past what is left.
 *
 * `circuit breaker` is what replaced the per-position stop for strategies
 * that run unbracketed. A stop bounds one trade and costs edge on every
 * trade; a breaker bounds the strategy and costs nothing until it fires,
 * at which point the book is flattened and the instance paused.
 */
function CapitalPanel({
  inst,
  onAction,
}: {
  inst: StrategyInstance;
  onAction: (p: Promise<string | null>) => void;
}) {
  const [alloc, setAlloc] = useState<Allocation>(inst.allocation);
  const [breaker, setBreaker] = useState<CircuitBreaker>(inst.circuit_breaker);
  const cap = inst.capital;
  const bs = cap?.breaker;

  const allocDirty =
    alloc.mode !== inst.allocation.mode || alloc.value !== inst.allocation.value;
  const breakerDirty =
    breaker.mode !== inst.circuit_breaker.mode ||
    breaker.value !== inst.circuit_breaker.value ||
    breaker.enabled !== inst.circuit_breaker.enabled;

  const used = cap && cap.allocated > 0 ? cap.deployed / cap.allocated : 0;
  const dd = bs && bs.limit > 0 ? Math.min(bs.drawdown / bs.limit, 1) : 0;

  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">CAPITAL — {inst.name.toUpperCase()}</div>

      <div className="grid gap-px md:grid-cols-2">
        {/* ---------------------------------------------------- allocation */}
        <div className="flex flex-col gap-1 p-2">
          <span
            className="text-[10px] tracking-widest text-bb-muted"
            title="The capital this strategy sizes against. Intents past what is left are refused."
          >
            ALLOCATION
          </span>
          <div className="flex items-center gap-1">
            <input
              data-numeric
              type="number"
              min={0}
              step={alloc.mode === "pct" ? 1 : 500}
              value={alloc.value}
              onChange={(e) => setAlloc((a) => ({ ...a, value: Number(e.target.value) }))}
              className="w-24 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-amber outline-none focus:border-bb-amber"
              aria-label="Allocation value"
            />
            <select
              value={alloc.mode}
              onChange={(e) =>
                setAlloc((a) => ({ ...a, mode: e.target.value as "pct" | "usd" }))
              }
              className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
              aria-label="Allocation mode"
            >
              <option value="pct">% of equity</option>
              <option value="usd">USD</option>
            </select>
            <button
              className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black disabled:opacity-30"
              disabled={!allocDirty}
              onClick={() => void onAction(actions.setAllocation(inst.id, alloc))}
            >
              SET
            </button>
          </div>
          {cap && (
            <>
              <div className="mt-1 h-1.5 w-full bg-bb-border/40">
                <div
                  className="h-full bg-bb-amber"
                  style={{ width: `${Math.min(used * 100, 100)}%` }}
                />
              </div>
              <div className="flex justify-between text-[10px]" data-numeric>
                <span className="text-bb-muted">
                  deployed <span className="text-bb-amber">{fmtUsd(cap.deployed)}</span>
                </span>
                <span className="text-bb-muted">
                  available <span className="text-white">{fmtUsd(cap.available)}</span>
                </span>
                <span
                  className="text-bb-muted"
                  title="of the account's total equity"
                >
                  {fmtUsd(cap.allocated)}
                  {cap.pct_of_account != null && ` · ${cap.pct_of_account.toFixed(0)}%`}
                </span>
              </div>
            </>
          )}
        </div>

        {/* ------------------------------------------------ circuit breaker */}
        <div className="flex flex-col gap-1 border-l border-bb-border p-2">
          <span
            className="text-[10px] tracking-widest text-bb-muted"
            title="Drawdown from this strategy's high-water mark. Trips: flatten the book, pause the instance."
          >
            CIRCUIT BREAKER
          </span>
          <div className="flex items-center gap-1">
            <label className="flex items-center gap-1 text-[10px] text-bb-muted">
              <input
                type="checkbox"
                checked={breaker.enabled}
                onChange={(e) =>
                  setBreaker((b) => ({ ...b, enabled: e.target.checked }))
                }
                aria-label="Circuit breaker enabled"
              />
              on
            </label>
            <input
              data-numeric
              type="number"
              min={0}
              step={breaker.mode === "pct" ? 1 : 250}
              value={breaker.value}
              onChange={(e) =>
                setBreaker((b) => ({ ...b, value: Number(e.target.value) }))
              }
              className="w-20 border border-bb-border bg-black px-1 py-0.5 text-right text-[11px] text-bb-amber outline-none focus:border-bb-amber"
              aria-label="Breaker value"
            />
            <select
              value={breaker.mode}
              onChange={(e) =>
                setBreaker((b) => ({ ...b, mode: e.target.value as "pct" | "usd" }))
              }
              className="border border-bb-border bg-black px-1 py-0.5 text-[11px] text-bb-amber outline-none"
              aria-label="Breaker mode"
            >
              <option value="pct">% of allocation</option>
              <option value="usd">USD</option>
            </select>
            <button
              className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black disabled:opacity-30"
              disabled={!breakerDirty}
              onClick={() => void onAction(actions.setBreaker(inst.id, breaker))}
            >
              SET
            </button>
          </div>
          {bs && (
            <>
              <div className="mt-1 h-1.5 w-full bg-bb-border/40">
                <div
                  className={
                    "h-full " +
                    (bs.tripped ? "bg-bb-loss" : dd > 0.6 ? "bg-bb-orange" : "bg-bb-profit")
                  }
                  style={{ width: `${dd * 100}%` }}
                />
              </div>
              <div className="flex justify-between text-[10px]" data-numeric>
                <span className="text-bb-muted">
                  P&L{" "}
                  <span className={pnlCls(bs.pnl)}>
                    {fmtUsd(bs.pnl, true)}
                  </span>
                </span>
                <span
                  className="text-bb-muted"
                  title={`peak ${fmtUsd(bs.peak)} · worst ever ${fmtUsd(bs.max_drawdown)}`}
                >
                  drawdown <span className="text-bb-orange">{fmtUsd(bs.drawdown)}</span>
                </span>
                <span className="text-bb-muted">
                  limit <span className="text-white">{fmtUsd(bs.limit)}</span>
                </span>
              </div>
              {bs.tripped && (
                <div className="text-[10px] text-bb-loss">
                  TRIPPED — book flattened, instance paused
                </div>
              )}
              {!breaker.enabled && (
                <div
                  className="text-[10px] text-bb-orange"
                  title="An unbracketed strategy has no per-position stop, so nothing bounds its loss."
                >
                  OFF — unbounded
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * The paper twin: the instance's journalled would-be trades compounded into
 * an equity curve, under two allocators. This is the ONLY readable outcome a
 * note-mode strategy has — PERFORMANCE above is empty for them by
 * construction, because they place nothing.
 *
 * Two allocators rather than one on purpose: `vol` is what the live sizer
 * would have done, `flat` is the same trades at constant notional. A result
 * that only survives one of them is a result about the sizing.
 */
function TwinPanel({ inst }: { inst: StrategyInstance }) {
  const [twin, setTwin] = useState<StrategyTwin | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getStrategyTwin(inst.id)
        .then((t) => alive && setTwin(t))
        .catch((err) => alive && setError(apiError(err)));
    void load();
    const t = window.setInterval(load, POLL_MS * 4);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [inst.id]);

  return (
    <div className="panel flex shrink-0 flex-col">
      <div
        className="panel-title"
        title="Journalled would-be trades compounded through the session calendar. Not fills — a replay."
      >
        PAPER TWIN — {inst.name.toUpperCase()}
        {twin ? ` (${twin.signals})` : ""}
      </div>
      {error ? (
        <div className="px-2 py-2 text-[11px] text-bb-loss">{error}</div>
      ) : !twin ? (
        <div className="px-2 py-2 text-[11px] text-bb-muted">loading…</div>
      ) : twin.signals === 0 ? (
        <div className="flex h-10 items-center justify-center text-[11px] text-bb-muted">
          nothing to replay
        </div>
      ) : (
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-[10px] text-bb-muted">
            <tr className="border-b border-bb-border">
              <th className="px-2 py-1 text-left">ALLOCATOR</th>
              <th className="px-2 py-1 text-right">EQUITY</th>
              <th className="px-2 py-1 text-right">RETURN</th>
              <th className="px-2 py-1 text-right">MAX DD</th>
              <th className="px-2 py-1 text-right">TRADES</th>
              <th className="px-2 py-1 text-right">OPEN</th>
              <th className="px-2 py-1 text-right">WIN RATE</th>
            </tr>
          </thead>
          <tbody>
            {twin.policies.map((p) => (
              <tr key={p.policy} className="border-b border-bb-border/50 hover:bg-bb-hover">
                <td className="px-2 py-1 text-bb-amber">
                  {p.policy === "vol" ? "vol-weighted (live sizer)" : "flat notional"}
                </td>
                <td data-numeric className="px-2 py-1 text-right text-white">
                  {p.equity.toFixed(0)}
                </td>
                <td data-numeric className={"px-2 py-1 text-right " + pnlCls(p.return_pct)}>
                  {p.return_pct >= 0 ? "+" : ""}
                  {p.return_pct.toFixed(2)}%
                </td>
                <td data-numeric className="px-2 py-1 text-right text-bb-orange">
                  {p.max_drawdown.toFixed(2)}%
                </td>
                <td data-numeric className="px-2 py-1 text-right">{p.trades}</td>
                <td data-numeric className="px-2 py-1 text-right text-bb-muted">{p.open}</td>
                <td data-numeric className="px-2 py-1 text-right">
                  {p.win_rate == null ? "—" : `${Math.round(p.win_rate * 100)}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

    </div>
  );
}

function SourcePanel({ kind }: { kind: string }) {
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState<{ file: string; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || source) return;
    getStrategySource(kind)
      .then((s) => setSource({ file: s.file, text: s.source }))
      .catch((err) => setError(apiError(err)));
  }, [open, source, kind]);

  return (
    <div className="panel flex shrink-0 flex-col">
      <button
        className="panel-title flex w-full items-center justify-between text-left hover:bg-bb-hover"
        onClick={() => setOpen((o) => !o)}
        title="The exact module source the engine is running for this strategy kind"
      >
        <span>
          SOURCE — {kind} {source ? `(${source.file})` : ""}
        </span>
        <span className="pr-2 text-bb-muted">{open ? "▾ hide" : "▸ show"}</span>
      </button>
      {open &&
        (error ? (
          <div className="px-2 py-2 text-[11px] text-bb-loss">{error}</div>
        ) : !source ? (
          <div className="px-2 py-2 text-[11px] text-bb-muted">loading…</div>
        ) : (
          <pre className="max-h-96 overflow-auto bg-black p-2 text-[10.5px] leading-snug text-bb-neutral">
            {source.text}
          </pre>
        ))}
    </div>
  );
}

function CreatePanel({ onAction }: { onAction: (p: Promise<string | null>) => void }) {
  const [kinds, setKinds] = useState<StrategyKind[]>([]);
  const [kind, setKind] = useState("");
  const [name, setName] = useState("");
  const refresh = useStrategyRunnerStore((s) => s.refresh);

  useEffect(() => {
    getStrategyCatalog()
      .then(({ kinds: ks }) => {
        setKinds(ks);
        if (ks.length && !kind) setKind(ks[0].kind);
      })
      .catch(() => setKinds([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = kinds.find((k) => k.kind === kind);
  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">NEW INSTANCE</div>
      <div className="flex items-center gap-2 px-2 py-2 text-[11px]">
        <select
          className="border border-bb-border bg-black px-1 py-0.5 text-bb-amber outline-none focus:border-bb-amber"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {kinds.map((k) => (
            <option key={k.kind} value={k.kind}>
              {k.kind}
            </option>
          ))}
        </select>
        <input
          className="w-40 border border-bb-border bg-black px-1 py-0.5 text-bb-amber outline-none focus:border-bb-amber"
          placeholder="instance name"
          maxLength={24}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <button
          className="border border-bb-profit px-1.5 text-[10px] text-bb-profit hover:bg-bb-profit hover:text-black disabled:opacity-30"
          disabled={!kind || !name.trim()}
          onClick={() =>
            void onAction(
              (async () => {
                try {
                  await createStrategy(kind, name.trim(), {});
                  setName("");
                  await refresh();
                  return null;
                } catch (err) {
                  return apiError(err);
                }
              })(),
            )
          }
        >
          CREATE (disabled)
        </button>
        <span className="text-[10px] text-bb-muted" title={selected?.doc ?? ""}>
          {selected ? `${selected.doc.slice(0, 80)} · listens: ${selected.subscriptions.join(", ")}` : ""}
        </span>
      </div>
    </div>
  );
}

function SignalsPanel({ signals }: { signals: SignalRecord[] }) {
  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">SIGNALS ({signals.length})</div>
      {signals.length === 0 ? (
        <div className="flex h-12 items-center justify-center text-[11px] text-bb-muted">
          no signals journaled yet
        </div>
      ) : (
        <div className="max-h-56 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <tbody>
              {signals.map((s) => (
                <tr key={s.id} className="border-b border-bb-border/50 hover:bg-bb-hover">
                  <td className="whitespace-nowrap px-2 py-1 text-bb-muted" data-numeric>
                    {s.id}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 text-bb-muted" data-numeric>
                    {hhmmss(s.ts)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 text-bb-amber">{s.type}</td>
                  <td className="whitespace-nowrap px-2 py-1 text-bb-muted">{s.source}</td>
                  <td className="whitespace-nowrap px-2 py-1 text-bb-neutral">
                    {(s.symbols ?? []).join(" ")}
                  </td>
                  <td
                    className="max-w-0 truncate px-2 py-1 text-bb-muted"
                    title={JSON.stringify(s.payload)}
                  >
                    {summarizePayload(s)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function summarizePayload(s: SignalRecord): string {
  const p = s.payload ?? {};
  if (s.type === "analysis") {
    const result = p.result as Record<string, unknown> | null | undefined;
    if (result) return `${String(p.model ?? "?")}: ${JSON.stringify(result)}`;
    return `${String(p.model ?? "?")}: ${String(p.error ?? "?")}`;
  }
  if (typeof p.headline === "string") return p.headline;
  if (typeof p.text === "string") return p.text;
  return JSON.stringify(p);
}
