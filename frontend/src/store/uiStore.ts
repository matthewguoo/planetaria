/**
 * UI-level navigation state: which top-level view is showing, and which
 * position (if any) the terminal's chart is inspecting instead of the
 * designer.
 *
 * `terminal` exists only in the trading cockpit (/terminal.html); the ops
 * console renders the other six. Both shells share this store because they
 * are separate bundles with separate state — the union is a superset, not a
 * coupling. Each entry point sets its own boot view.
 */

import { create } from "zustand";
import type { Plan } from "../lib/api";

export type PnlMode = "entry" | "live";

export type View =
  | "overview"
  | "terminal"
  | "fund"
  | "portfolio"
  | "account"
  | "strategies"
  | "market"
  | "system";

type UiState = {
  view: View;
  /** Plan id the chart is viewing; null = designer mode. */
  viewingPlanId: string | null;
  /** Snapshot of a CLOSED plan being viewed (history rows aren't in the
   * positions store); live plans resolve from positions by id instead. */
  viewedHistorical: Plan | null;
  /** Position P/L basis: entry-time projection vs latest chain greeks. */
  pnlMode: PnlMode;
  /** Options chain side panel visibility. */
  chainOpen: boolean;
  setView: (view: View) => void;
  viewPosition: (planId: string) => void;
  viewHistorical: (plan: Plan) => void;
  closePositionView: () => void;
  setPnlMode: (mode: PnlMode) => void;
  toggleChain: () => void;
};

/** Any real trading action — symbol change, preset pick, strike/leg/exit
 * edit — flips the chart back to the designer. Position and history views
 * are read-only inspections, not a mode the user should have to find an
 * escape hatch for: acting like a trader IS the escape hatch. Called from
 * the trading/strategy store actions so every surface (panel, chain panel,
 * rail, symbol search, mobile sheets) inherits the behavior. */
export function exitPositionViewOnAction(): void {
  const s = useUiStore.getState();
  if (s.viewingPlanId) s.closePositionView();
}

export const useUiStore = create<UiState>((set) => ({
  // The book is the first question: whose money is where, and what is it
  // holding. The terminal bundle overrides this to "terminal" at boot.
  view: "fund",
  viewingPlanId: null,
  viewedHistorical: null,
  pnlMode: "live",
  chainOpen: false,
  setView: (view) => set({ view }),
  viewPosition: (planId) =>
    set({ viewingPlanId: planId, viewedHistorical: null, view: "terminal" }),
  viewHistorical: (plan) =>
    // Closed trades are frozen facts: always the entry-basis projection.
    set({ viewingPlanId: plan.id, viewedHistorical: plan, view: "terminal", pnlMode: "entry" }),
  closePositionView: () => set({ viewingPlanId: null, viewedHistorical: null }),
  setPnlMode: (pnlMode) => set({ pnlMode }),
  toggleChain: () => set((s) => ({ chainOpen: !s.chainOpen })),
}));
