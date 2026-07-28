/**
 * Strategy designer state: chain data, selected structure, exits, sizing.
 * All advisory math runs client-side (mirror); the server re-validates at
 * order time and is authoritative.
 *
 * Strategies are declarative leg templates: offsets are in strike-steps from
 * ATM, so every preset works across underlyings and strike grids. Strikes and
 * per-leg ratios are then freely editable (chart drag / payoff rails / panel).
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

export type LegTemplate = {
  right: "C" | "P";
  side: 1 | -1;
  ratio: number;
  /** default placement, in strike-grid steps from ATM (+ = higher strike) */
  offset: number;
};

export type StrategyDef = {
  label: string;
  group: "DIRECTIONAL" | "SPREADS" | "VOLATILITY" | "INCOME / NEUTRAL";
  legs: LegTemplate[];
};

export const STRATEGIES = {
  long_call: {
    label: "LONG CALL",
    group: "DIRECTIONAL",
    legs: [{ right: "C", side: 1, ratio: 1, offset: 1 }],
  },
  long_put: {
    label: "LONG PUT",
    group: "DIRECTIONAL",
    legs: [{ right: "P", side: 1, ratio: 1, offset: -1 }],
  },
  call_debit_spread: {
    label: "CALL DEBIT SPREAD",
    group: "SPREADS",
    legs: [
      { right: "C", side: 1, ratio: 1, offset: 0 },
      { right: "C", side: -1, ratio: 1, offset: 3 },
    ],
  },
  put_debit_spread: {
    label: "PUT DEBIT SPREAD",
    group: "SPREADS",
    legs: [
      { right: "P", side: 1, ratio: 1, offset: 0 },
      { right: "P", side: -1, ratio: 1, offset: -3 },
    ],
  },
  call_credit_spread: {
    label: "CALL CREDIT SPREAD",
    group: "SPREADS",
    legs: [
      { right: "C", side: -1, ratio: 1, offset: 2 },
      { right: "C", side: 1, ratio: 1, offset: 5 },
    ],
  },
  put_credit_spread: {
    label: "PUT CREDIT SPREAD",
    group: "SPREADS",
    legs: [
      { right: "P", side: -1, ratio: 1, offset: -2 },
      { right: "P", side: 1, ratio: 1, offset: -5 },
    ],
  },
  long_straddle: {
    label: "LONG STRADDLE",
    group: "VOLATILITY",
    legs: [
      { right: "C", side: 1, ratio: 1, offset: 0 },
      { right: "P", side: 1, ratio: 1, offset: 0 },
    ],
  },
  long_strangle: {
    label: "LONG STRANGLE",
    group: "VOLATILITY",
    legs: [
      { right: "C", side: 1, ratio: 1, offset: 2 },
      { right: "P", side: 1, ratio: 1, offset: -2 },
    ],
  },
  short_straddle: {
    label: "SHORT STRADDLE",
    group: "VOLATILITY",
    legs: [
      { right: "C", side: -1, ratio: 1, offset: 0 },
      { right: "P", side: -1, ratio: 1, offset: 0 },
    ],
  },
  short_strangle: {
    label: "SHORT STRANGLE",
    group: "VOLATILITY",
    legs: [
      { right: "C", side: -1, ratio: 1, offset: 3 },
      { right: "P", side: -1, ratio: 1, offset: -3 },
    ],
  },
  iron_condor: {
    label: "IRON CONDOR",
    group: "INCOME / NEUTRAL",
    legs: [
      { right: "P", side: 1, ratio: 1, offset: -6 },
      { right: "P", side: -1, ratio: 1, offset: -3 },
      { right: "C", side: -1, ratio: 1, offset: 3 },
      { right: "C", side: 1, ratio: 1, offset: 6 },
    ],
  },
  iron_butterfly: {
    label: "IRON BUTTERFLY",
    group: "INCOME / NEUTRAL",
    legs: [
      { right: "P", side: 1, ratio: 1, offset: -4 },
      { right: "P", side: -1, ratio: 1, offset: 0 },
      { right: "C", side: -1, ratio: 1, offset: 0 },
      { right: "C", side: 1, ratio: 1, offset: 4 },
    ],
  },
  call_butterfly: {
    label: "CALL BUTTERFLY",
    group: "INCOME / NEUTRAL",
    legs: [
      { right: "C", side: 1, ratio: 1, offset: -3 },
      { right: "C", side: -1, ratio: 2, offset: 0 },
      { right: "C", side: 1, ratio: 1, offset: 3 },
    ],
  },
  put_butterfly: {
    label: "PUT BUTTERFLY",
    group: "INCOME / NEUTRAL",
    legs: [
      { right: "P", side: 1, ratio: 1, offset: 3 },
      { right: "P", side: -1, ratio: 2, offset: 0 },
      { right: "P", side: 1, ratio: 1, offset: -3 },
    ],
  },
  short_put: {
    label: "SHORT PUT (CSP)",
    group: "INCOME / NEUTRAL",
    legs: [{ right: "P", side: -1, ratio: 1, offset: -2 }],
  },
} as const satisfies Record<string, StrategyDef>;

export type StrategyKind = keyof typeof STRATEGIES;

export const STRATEGY_LABELS: Record<StrategyKind, string> = Object.fromEntries(
  Object.entries(STRATEGIES).map(([k, def]) => [k, def.label]),
) as Record<StrategyKind, string>;

export function strategyDef(kind: StrategyKind): StrategyDef {
  return STRATEGIES[kind];
}

export type StrategyLeg = Leg & { symbol: string; expiry: string };

type StrategyState = {
  chain: Chain | null;
  chainError: string | null;
  expiry: string | null;
  kind: StrategyKind;
  /** chosen strike per leg (parallel to STRATEGIES[kind].legs) */
  strikes: number[];
  /** per-leg contract ratio (parallel to legs; template default until edited) */
  ratios: number[];
  qty: number; // desired contract sets (0 = auto from sizing)
  tpPct: number; // TP as fraction of |entry premium| gained (1.0 = +100%)
  slPct: number; // SL as fraction of |entry premium| lost (0.5 = -50%)
  timeStopEt: string; // "HH:MM"

  loadChain: (underlying: string) => Promise<void>;
  setExpiry: (expiry: string) => void;
  setKind: (kind: StrategyKind) => void;
  setStrike: (index: number, strike: number) => void;
  setRatio: (index: number, ratio: number) => void;
  setTpPct: (v: number) => void;
  setSlPct: (v: number) => void;
  setTimeStopEt: (v: string) => void;
  setQty: (v: number) => void;
};

function nearestStrike(strikes: number[], target: number): number {
  return strikes.reduce((best, s) => (Math.abs(s - target) < Math.abs(best - target) ? s : best), strikes[0]);
}

export function defaultStrikes(chain: Chain, expiry: string, kind: StrategyKind): number[] {
  const strikes = [...new Set(chain.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike))].sort(
    (a, b) => a - b,
  );
  if (!strikes.length) return [];
  const atm = nearestStrike(strikes, chain.spot);
  const idx = strikes.indexOf(atm);
  const step = (offset: number) => strikes[Math.min(Math.max(idx + offset, 0), strikes.length - 1)];
  return STRATEGIES[kind].legs.map((leg) => step(leg.offset));
}

export function defaultRatios(kind: StrategyKind): number[] {
  return STRATEGIES[kind].legs.map((leg) => leg.ratio);
}

export const useStrategyStore = create<StrategyState>((set, get) => ({
  chain: null,
  chainError: null,
  expiry: null,
  kind: "long_call",
  strikes: [],
  ratios: defaultRatios("long_call"),
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
      let ratios = state.ratios;
      const template = STRATEGIES[state.kind].legs;
      const chainStrikes = new Set(
        data.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike),
      );
      if (
        strikes.length !== template.length ||
        !strikes.every((s) => chainStrikes.has(s))
      ) {
        strikes = expiry ? defaultStrikes(data, expiry, state.kind) : [];
        ratios = defaultRatios(state.kind);
      }
      set({ chain: data, chainError: null, expiry, strikes, ratios });
    } catch (err) {
      set({ chainError: String((err as Error).message ?? err) });
    }
  },

  setExpiry: (expiry) => {
    const { chain, kind } = get();
    set({
      expiry,
      strikes: chain ? defaultStrikes(chain, expiry, kind) : [],
      ratios: defaultRatios(kind),
    });
  },
  setKind: (kind) => {
    const { chain, expiry } = get();
    set({
      kind,
      strikes: chain && expiry ? defaultStrikes(chain, expiry, kind) : [],
      ratios: defaultRatios(kind),
    });
  },
  setStrike: (index, strike) =>
    set((s) => {
      const strikes = [...s.strikes];
      strikes[index] = strike;
      return { strikes };
    }),
  setRatio: (index, ratio) =>
    set((s) => {
      const ratios = [...s.ratios];
      ratios[index] = Math.max(1, Math.min(Math.round(ratio), 9));
      return { ratios };
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
  ratios?: number[];
}): StrategyLeg[] | null {
  const { chain, expiry, kind, strikes } = state;
  const template = STRATEGIES[kind].legs;
  if (!chain || !expiry || strikes.length !== template.length) return null;
  const ratios = state.ratios && state.ratios.length === template.length
    ? state.ratios
    : template.map((l) => l.ratio);
  const legs: StrategyLeg[] = [];
  for (let i = 0; i < template.length; i++) {
    const t = template[i];
    const contract = findContract(chain, expiry, t.right, strikes[i]);
    if (!contract || contract.mid <= 0 || contract.iv <= 0) return null;
    legs.push({
      right: t.right,
      strike: strikes[i],
      qty: ratios[i],
      side: t.side,
      entry: contract.mid,
      iv: contract.iv,
      symbol: contract.symbol,
      expiry,
    });
  }
  return legs;
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
