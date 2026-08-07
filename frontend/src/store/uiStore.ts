/**
 * Which top-level page the console is showing. That is the whole of the
 * console's UI state.
 *
 * This store used to also carry chart concerns — which plan the chart was
 * inspecting, the P/L basis, whether the options chain was open. Those went
 * with the options terminal.
 */

import { create } from "zustand";

export type View = "account" | "strategies" | "market" | "system";

type UiState = {
  view: View;
  setView: (view: View) => void;
};

export const useUiStore = create<UiState>((set) => ({
  // The money is the first question.
  view: "account",
  setView: (view) => set({ view }),
}));
