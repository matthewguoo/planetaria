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
  type SignalRecord,
  type StrategyDecision,
  type StrategyInstance,
  type StrategyKind,
  type StrategyPerformance,
} from "../../lib/api";
import {
  actions,
  decisionColor,
  decisionSummary,
  formatAge,
  runtimeSummary,
  stateColor,
  useStrategyRunnerStore,
} from "../../store/strategyRunnerStore";

const POLL_MS = 5_000;

export default function StrategiesPage() {
  const instances = useStrategyRunnerStore((s) => s.instances);
  const decisions = useStrategyRunnerStore((s) => s.decisions);
  const signals = useStrategyRunnerStore((s) => s.signals);
  const selectedId = useStrategyRunnerStore((s) => s.selectedId);
  const loaded = useStrategyRunnerStore((s) => s.loaded);
  const refresh = useStrategyRunnerStore((s) => s.refresh);
  const select = useStrategyRunnerStore((s) => s.select);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(t);
  }, [refresh]);

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
                  selected={inst.id === selectedId}
                  onSelect={() => select(inst.id === selectedId ? null : inst.id)}
                  onAction={run}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selected && <ParamsPanel key={selected.id} inst={selected} onAction={run} />}
      {selected && <PerformancePanel key={`perf-${selected.id}`} inst={selected} />}
      {selected && <DecisionsPanel inst={selected} decisions={decisions} />}
      {selected && <SourcePanel key={`src-${selected.kind}`} kind={selected.kind} />}

      <CreatePanel onAction={run} />
      <SignalsPanel signals={signals} />
    </div>
  );
}

function InstanceRow({
  inst,
  selected,
  onSelect,
  onAction,
}: {
  inst: StrategyInstance;
  selected: boolean;
  onSelect: () => void;
  onAction: (p: Promise<string | null>) => void;
}) {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <tr
      className={
        "cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover " +
        (selected ? "bg-bb-orange/5" : "")
      }
      onClick={onSelect}
      title="Click to inspect params and the decision journal"
    >
      <td className="px-2 py-1 text-bb-amber">{inst.name}</td>
      <td className="px-2 py-1 text-bb-muted">{inst.kind}</td>
      <td className={"px-2 py-1 " + stateColor(inst.state, inst.runtime?.task ?? "")}>
        {inst.state}
        {inst.runtime?.task?.startsWith("DEAD") ? " (task dead)" : ""}
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
            title="Journal a manual signal scoped to this instance"
            className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black disabled:opacity-30"
            disabled={inst.state !== "enabled"}
            onClick={() => void onAction(actions.trigger(inst.id, {}))}
          >
            TRIGGER
          </button>
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
        PARAMS — {inst.name.toUpperCase()} (saving restarts the instance)
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
                    {d.ts?.slice(11, 19) ?? "-"}
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

  const pnlColor = (v: number | null | undefined) =>
    v == null ? "text-bb-muted" : v >= 0 ? "text-bb-profit" : "text-bb-loss";

  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        PERFORMANCE — {inst.name.toUpperCase()} (plans carrying this strategy label)
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
              <span className={pnlColor(perf.realized_pnl)}>
                {perf.realized_pnl.toFixed(2)}
              </span>
            </span>
            <span className="text-bb-muted">
              avg win{" "}
              <span className={pnlColor(perf.avg_win)}>
                {perf.avg_win == null ? "—" : perf.avg_win.toFixed(2)}
              </span>
            </span>
            <span className="text-bb-muted">
              avg loss{" "}
              <span className={pnlColor(perf.avg_loss)}>
                {perf.avg_loss == null ? "—" : perf.avg_loss.toFixed(2)}
              </span>
            </span>
          </div>
          {perf.plans.length === 0 ? (
            <div className="flex h-10 items-center justify-center text-[11px] text-bb-muted">
              no plans yet — note-mode strategies journal would-be trades in the
              decision journal instead
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
                        {p.created_at?.slice(5, 16).replace("T", " ") ?? "-"}
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
                      <td className={"px-2 py-1 text-right " + pnlColor(p.realized_pnl)} data-numeric>
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
      .then((ks) => {
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
                    {s.ts?.slice(11, 19) ?? "-"}
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
