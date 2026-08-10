/**
 * Which top-level page the console is showing. That is the whole of the
 * console's UI state.
 *
 * This store used to also carry chart concerns — which plan the chart was
 * inspecting, the P/L basis, whether the options chain was open. Those went
 * with the options terminal.
 */

import { create } from "zustand";

export type View = "fund" | "account" | "strategies" | "market" | "system";

type UiState = {
  view: View;
  setView: (view: View) => void;
};

export const useUiStore = create<UiState>((set) => ({
  // The book is the first question: whose money is where, and what is it
  // holding. ACCOUNT keeps the broker's raw view one tab away.
  view: "fund",
  setView: (view) => set({ view }),
}));
