/**
 * Strategy RUNTIME control plane: backend StrategyRunner instances, their
 * decision journals, and the signals feed. Distinct from strategyStore.ts,
 * which is the options DESIGNER's leg-template catalog.
 *
 * House pattern: poll REST on an interval (the page owns the timer), keep
 * last-known state on transient errors, surface action errors to the caller.
 */

import { create } from "zustand";
import {
  apiError,
  enableStrategy,
  flattenStrategy,
  getSignals,
  getStrategies,
  getStrategyDecisions,
  killAllStrategies,
  pauseStrategy,
  patchStrategyParams,
  triggerStrategy,
  type SignalRecord,
  type StrategyDecision,
  type StrategyInstance,
} from "../lib/api";

// ------------------------------------------------------------ pure helpers
// Exported for vitest: presentation logic stays out of the component tree.

/** Terminal palette class for an instance's lifecycle state + task health.
 * A row is only "green" when it is enabled AND its task is actually alive —
 * an enabled instance with a dead task is the loudest possible red. */
export function stateColor(state: StrategyInstance["state"], task: string): string {
  if (state === "enabled" && task.startsWith("DEAD")) return "text-bb-loss";
  if (state === "enabled" && task === "running") return "text-bb-profit";
  if (state === "enabled") return "text-bb-orange"; // enabled but not running yet
  if (state === "paused") return "text-bb-orange";
  return "text-bb-muted";
}

/** One-line runtime summary for the table: queue depth, error count, and how
 * long since the strategy last saw an event. */
export function runtimeSummary(rt: StrategyInstance["runtime"]): string {
  if (!rt || rt.task === "not running") return "-";
  const parts = [rt.task];
  if (rt.queue_size) parts.push(`q${rt.queue_size}`);
  if (rt.errors) parts.push(`${rt.errors} err`);
  if (rt.last_event_age_s != null) parts.push(`${formatAge(rt.last_event_age_s)} ago`);
  return parts.join(" · ");
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** Decision action -> terminal color. Placed/note are facts, skips are
 * expected discipline, rejections and errors demand eyes. */
export function decisionColor(action: string): string {
  switch (action) {
    case "placed":
      return "text-bb-profit";
    case "intent":
      return "text-bb-amber";
    case "rejected":
    case "error":
      return "text-bb-loss";
    case "skip":
      return "text-bb-orange";
    default:
      return "text-bb-muted";
  }
}

/** Compact one-liner for a decision row's detail blob. */
export function decisionSummary(d: StrategyDecision): string {
  const detail = d.detail ?? {};
  if (typeof detail.skip === "string") return detail.skip;
  if (typeof detail.why === "string") return detail.why;
  if (typeof detail.error === "string") return detail.error;
  if (detail.verdict && typeof detail.verdict === "object") {
    const v = detail.verdict as Record<string, unknown>;
    return `verdict: ${String(v.direction ?? "?")} (${String(v.confidence ?? "?")})`;
  }
  if (typeof detail.reason === "string" && detail.reason) return detail.reason;
  if (detail.flatten && typeof detail.flatten === "object") {
    const f = detail.flatten as Record<string, unknown>;
    return `flatten: closed ${String(f.closed ?? 0)}`;
  }
  if (d.plan_id) return `plan ${d.plan_id.slice(0, 8)}`;
  const keys = Object.keys(detail);
  return keys.length ? keys.join(", ") : "-";
}

// ------------------------------------------------------------------- store

type RunnerState = {
  instances: StrategyInstance[];
  decisions: StrategyDecision[];
  signals: SignalRecord[];
  selectedId: string | null;
  loaded: boolean;
  refresh: () => Promise<void>;
  select: (id: string | null) => void;
  /** Control actions return an error string (for inline display) or null. */
  act: (fn: () => Promise<unknown>) => Promise<string | null>;
};

export const useStrategyRunnerStore = create<RunnerState>((set, get) => ({
  instances: [],
  decisions: [],
  signals: [],
  selectedId: null,
  loaded: false,

  refresh: async () => {
    const { selectedId } = get();
    const [instances, signals, decisions] = await Promise.allSettled([
      getStrategies(),
      getSignals({ limit: 40 }),
      selectedId ? getStrategyDecisions(selectedId, 60) : Promise.resolve([]),
    ]);
    // Keep last-known on transient failures; a blank page helps nobody.
    set((s) => ({
      loaded: true,
      instances: instances.status === "fulfilled" ? instances.value : s.instances,
      signals: signals.status === "fulfilled" ? signals.value : s.signals,
      decisions: decisions.status === "fulfilled" ? decisions.value : s.decisions,
    }));
  },

  select: (id) => {
    set({ selectedId: id, decisions: [] });
    void get().refresh();
  },

  act: async (fn) => {
    try {
      await fn();
      await get().refresh();
      return null;
    } catch (err) {
      return apiError(err);
    }
  },
}));

// Thin bound wrappers so the page reads declaratively.
export const actions = {
  enable: (id: string) => useStrategyRunnerStore.getState().act(() => enableStrategy(id)),
  pause: (id: string) => useStrategyRunnerStore.getState().act(() => pauseStrategy(id)),
  flatten: (id: string) => useStrategyRunnerStore.getState().act(() => flattenStrategy(id)),
  trigger: (id: string, payload: Record<string, unknown>) =>
    useStrategyRunnerStore.getState().act(() => triggerStrategy(id, payload)),
  killAll: (flatten: boolean) =>
    useStrategyRunnerStore.getState().act(() => killAllStrategies(flatten)),
  saveParams: (id: string, params: Record<string, unknown>) =>
    useStrategyRunnerStore.getState().act(() => patchStrategyParams(id, params)),
};
