/**
 * UI-level navigation state: which top-level view is showing, and which
 * position (if any) the chart is inspecting instead of the designer.
 */

import { create } from "zustand";
import type { Plan } from "../lib/api";

export type PnlMode = "entry" | "live";

type UiState = {
  view: "terminal" | "account";
  /** Plan id the chart is viewing; null = designer mode. */
  viewingPlanId: string | null;
  /** Snapshot of a CLOSED plan being viewed (history rows aren't in the
   * positions store); live plans resolve from positions by id instead. */
  viewedHistorical: Plan | null;
  /** Position P/L basis: entry-time projection vs latest chain greeks. */
  pnlMode: PnlMode;
  /** Options chain side panel visibility. */
  chainOpen: boolean;
  setView: (view: "terminal" | "account") => void;
  viewPosition: (planId: string) => void;
  viewHistorical: (plan: Plan) => void;
  closePositionView: () => void;
  setPnlMode: (mode: PnlMode) => void;
  toggleChain: () => void;
};

export const useUiStore = create<UiState>((set) => ({
  view: "terminal",
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
