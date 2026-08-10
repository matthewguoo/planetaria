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
  positions: Plan[];
  untracked: UntrackedPosition[];
  refreshAccount: () => Promise<void>;
  refreshPositions: () => Promise<void>;
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
}));
