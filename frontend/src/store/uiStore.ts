/**
 * UI-level navigation state: which top-level view is showing, and which
 * position (if any) the chart is inspecting instead of the designer.
 */

import { create } from "zustand";

export type PnlMode = "entry" | "live";

type UiState = {
  view: "terminal" | "account";
  /** Plan id the chart is viewing; null = designer mode. */
  viewingPlanId: string | null;
  /** Position P/L basis: entry-time projection vs latest chain greeks. */
  pnlMode: PnlMode;
  /** Options chain side panel visibility. */
  chainOpen: boolean;
  setView: (view: "terminal" | "account") => void;
  viewPosition: (planId: string) => void;
  closePositionView: () => void;
  setPnlMode: (mode: PnlMode) => void;
  toggleChain: () => void;
};

export const useUiStore = create<UiState>((set) => ({
  view: "terminal",
  viewingPlanId: null,
  pnlMode: "live",
  chainOpen: false,
  setView: (view) => set({ view }),
  viewPosition: (planId) => set({ viewingPlanId: planId, view: "terminal" }),
  closePositionView: () => set({ viewingPlanId: null }),
  setPnlMode: (pnlMode) => set({ pnlMode }),
  toggleChain: () => set((s) => ({ chainOpen: !s.chainOpen })),
}));
