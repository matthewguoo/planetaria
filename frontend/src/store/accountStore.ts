import { create } from "zustand";
import {
  getAccount,
  getPositions,
  type Account,
  type Plan,
  type TradingMode,
  type UntrackedPosition,
} from "../lib/api";

type AccountState = {
  account: Account | null;
  positions: Plan[];
  untracked: UntrackedPosition[];
  refreshAccount: () => Promise<void>;
  refreshPositions: () => Promise<void>;
  applyPlanUpdate: (plan: Plan) => void;
};

export const useAccountStore = create<AccountState>((set) => ({
  account: null,
  positions: [],
  untracked: [],

  refreshAccount: async () => {
    try {
      set({ account: await getAccount() });
    } catch {
      // transient; keep last known
    }
  },

  refreshPositions: async () => {
    try {
      const payload = await getPositions();
      set({ positions: payload.positions, untracked: payload.untracked ?? [] });
    } catch {
      // transient; keep last known
    }
  },

  applyPlanUpdate: (plan) =>
    set((state) => {
      const open = ["planned", "submitted", "partially_filled", "filled", "exiting"].includes(
        plan.status,
      );
      const rest = state.positions.filter((p) => p.id !== plan.id);
      return { positions: open ? [...rest, plan] : rest };
    }),
}));

/**
 * Which server this UI is connected to. FAIL-SAFE DEFAULT: "paper" until
 * the first account fetch lands — nothing live-styled or live-enabled may
 * render on an optimistic guess. `loaded` lets a control distinguish "paper
 * server" from "don't know yet" (the live submit path requires loaded).
 */
export function useTradingMode(): { mode: TradingMode; live: boolean; loaded: boolean } {
  const mode = useAccountStore((s) => s.account?.mode);
  return { mode: mode ?? "paper", live: mode === "live_manual", loaded: mode != null };
}
