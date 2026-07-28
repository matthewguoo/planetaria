/**
 * Strategy designer state: chain data, selected structure, exits, sizing.
 * All advisory math runs client-side (mirror); the server re-validates at
 * order time and is authoritative.
 */

import { create } from "zustand";
import { api } from "../lib/api";
import { tradingHoursToExpiry, type Leg } from "../lib/optionsMath";

export type Contract = {
  symbol: string;
  right: "C" | "P";
  strike: number;
  expiry: string;
  bid: number;
  ask: number;
  mid: number;
  iv: number;
  iv_source?: string;
  delta: number | null;
};

export type Chain = {
  underlying: string;
  spot: number;
  asof: number;
  expirations: string[];
  contracts: Contract[];
  demo: boolean;
};

export type StrategyKind = "long_call" | "long_put" | "call_spread" | "put_spread";

export const STRATEGY_LABELS: Record<StrategyKind, string> = {
  long_call: "LONG CALL",
  long_put: "LONG PUT",
  call_spread: "CALL DEBIT SPREAD",
  put_spread: "PUT DEBIT SPREAD",
};

export type StrategyLeg = Leg & { symbol: string; expiry: string };

type StrategyState = {
  chain: Chain | null;
  chainError: string | null;
  expiry: string | null;
  kind: StrategyKind;
  /** chosen strikes per leg role (index 0 = primary/long, 1 = short wing) */
  strikes: number[];
  qty: number; // desired contract sets (0 = auto from sizing)
  tpPct: number; // TP as multiple of entry premium (1.0 = +100%)
  slPct: number; // SL as fraction lost (0.5 = -50%)
  timeStopEt: string; // "HH:MM"

  loadChain: (underlying: string) => Promise<void>;
  setExpiry: (expiry: string) => void;
  setKind: (kind: StrategyKind) => void;
  setStrike: (index: number, strike: number) => void;
  setTpPct: (v: number) => void;
  setSlPct: (v: number) => void;
  setTimeStopEt: (v: string) => void;
  setQty: (v: number) => void;
};

function nearestStrike(strikes: number[], target: number): number {
  return strikes.reduce((best, s) => (Math.abs(s - target) < Math.abs(best - target) ? s : best), strikes[0]);
}

function defaultStrikes(chain: Chain, expiry: string, kind: StrategyKind): number[] {
  const strikes = [...new Set(chain.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike))].sort(
    (a, b) => a - b,
  );
  if (!strikes.length) return [];
  const atm = nearestStrike(strikes, chain.spot);
  const idx = strikes.indexOf(atm);
  const step = (offset: number) => strikes[Math.min(Math.max(idx + offset, 0), strikes.length - 1)];
  switch (kind) {
    case "long_call":
      return [step(1)];
    case "long_put":
      return [step(-1)];
    case "call_spread":
      return [step(0), step(3)];
    case "put_spread":
      return [step(0), step(-3)];
  }
}

export const useStrategyStore = create<StrategyState>((set, get) => ({
  chain: null,
  chainError: null,
  expiry: null,
  kind: "long_call",
  strikes: [],
  qty: 0,
  tpPct: 1.0,
  slPct: 0.5,
  timeStopEt: "15:50",

  loadChain: async (underlying: string) => {
    try {
      const { data } = await api.get<Chain>(`/api/options/chain/${underlying}`, {
        params: { dte_max: 3 },
      });
      const state = get();
      const expiry =
        state.expiry && data.expirations.includes(state.expiry)
          ? state.expiry
          : data.expirations[0] ?? null;
      let strikes = state.strikes;
      const chainStrikes = new Set(
        data.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike),
      );
      if (!strikes.length || !strikes.every((s) => chainStrikes.has(s))) {
        strikes = expiry ? defaultStrikes(data, expiry, state.kind) : [];
      }
      set({ chain: data, chainError: null, expiry, strikes });
    } catch (err) {
      set({ chainError: String((err as Error).message ?? err) });
    }
  },

  setExpiry: (expiry) => {
    const { chain, kind } = get();
    set({ expiry, strikes: chain ? defaultStrikes(chain, expiry, kind) : [] });
  },
  setKind: (kind) => {
    const { chain, expiry } = get();
    set({ kind, strikes: chain && expiry ? defaultStrikes(chain, expiry, kind) : [] });
  },
  setStrike: (index, strike) =>
    set((s) => {
      const strikes = [...s.strikes];
      strikes[index] = strike;
      return { strikes };
    }),
  setTpPct: (v) => set({ tpPct: Math.max(0.05, Math.min(v, 10)) }),
  setSlPct: (v) => set({ slPct: Math.max(0.05, Math.min(v, 0.95)) }),
  setTimeStopEt: (v) => set({ timeStopEt: v }),
  setQty: (v) => set({ qty: Math.max(0, Math.min(v, 100)) }),
}));

// ------------------------------------------------------------ derivations

export function findContract(
  chain: Chain,
  expiry: string,
  right: "C" | "P",
  strike: number,
): Contract | null {
  return (
    chain.contracts.find(
      (c) => c.expiry === expiry && c.right === right && c.strike === strike,
    ) ?? null
  );
}

/** Build priced legs for the current selection; null if incomplete. */
export function buildLegs(state: {
  chain: Chain | null;
  expiry: string | null;
  kind: StrategyKind;
  strikes: number[];
}): StrategyLeg[] | null {
  const { chain, expiry, kind, strikes } = state;
  if (!chain || !expiry || !strikes.length) return null;
  const mk = (right: "C" | "P", strike: number, side: 1 | -1): StrategyLeg | null => {
    const contract = findContract(chain, expiry, right, strike);
    if (!contract || contract.mid <= 0 || contract.iv <= 0) return null;
    return {
      right,
      strike,
      qty: 1,
      side,
      entry: contract.mid,
      iv: contract.iv,
      symbol: contract.symbol,
      expiry,
    };
  };
  let legs: (StrategyLeg | null)[];
  switch (kind) {
    case "long_call":
      legs = [mk("C", strikes[0], 1)];
      break;
    case "long_put":
      legs = [mk("P", strikes[0], 1)];
      break;
    case "call_spread":
      legs = [mk("C", strikes[0], 1), mk("C", strikes[1], -1)];
      break;
    case "put_spread":
      legs = [mk("P", strikes[0], 1), mk("P", strikes[1], -1)];
      break;
  }
  if (legs.some((l) => l === null)) return null;
  return legs as StrategyLeg[];
}

export function hoursToExpiry(expiry: string): number {
  return tradingHoursToExpiry(expiry, Date.now());
}

/** Strikes available for the active expiry, sorted. */
export function availableStrikes(chain: Chain | null, expiry: string | null): number[] {
  if (!chain || !expiry) return [];
  return [...new Set(chain.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike))].sort(
    (a, b) => a - b,
  );
}
