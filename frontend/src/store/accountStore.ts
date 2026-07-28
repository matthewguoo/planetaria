import { create } from "zustand";
import {
  getAccount,
  getPositions,
  type Account,
  type Plan,
  type UntrackedPosition,
} from "../lib/api";

type AccountState = {
  account: Account | null;
  accountError: string | null;
  positions: Plan[];
  untracked: UntrackedPosition[];
  refreshAccount: () => Promise<void>;
  refreshPositions: () => Promise<void>;
  applyPlanUpdate: (plan: Plan) => void;
};

export const useAccountStore = create<AccountState>((set) => ({
  account: null,
  accountError: null,
  positions: [],
  untracked: [],

  refreshAccount: async () => {
    try {
      set({ account: await getAccount(), accountError: null });
    } catch (err) {
      set({ accountError: String((err as Error).message ?? err) });
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
      const open = ["planned", "submitted", "filled", "exiting"].includes(plan.status);
      const rest = state.positions.filter((p) => p.id !== plan.id);
      return { positions: open ? [...rest, plan] : rest };
    }),
}));
