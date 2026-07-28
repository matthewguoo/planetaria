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
import {
  bsDelta,
  TRADING_HOURS_PER_YEAR,
  tradingHoursToExpiry,
  type Leg,
} from "../lib/optionsMath";

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

export type StrategyLeg = Leg & { symbol: string; expiry: string; halfSpread: number };

type StrategyState = {
  chain: Chain | null;
  chainError: string | null;
  expiry: string | null;
  kind: StrategyKind;
  /** chosen strike per leg (parallel arrays below) */
  strikes: number[];
  /** per-leg contract ratio */
  ratios: number[];
  /** per-leg right/side — regenerated from the preset template, then freely
   * editable via the chain panel (custom compositions). */
  rights: ("C" | "P")[];
  sides: (1 | -1)[];
  /** true once the leg set deviates from the selected preset */
  modified: boolean;
  qty: number; // desired contract sets (0 = auto from sizing)
  tpPct: number; // TP as fraction of |entry premium| gained (1.0 = +100%)
  slPct: number; // SL as fraction of |entry premium| lost (0.5 = -50%)
  timeStopEt: string; // "HH:MM"
  /** Scenario IV shock, relative (+0.2 = all vols 20% richer). */
  volShift: number;
  /** Apply skew-derived directional vol response (down-move => vol up). */
  skewBeta: boolean;

  loadChain: (underlying: string) => Promise<void>;
  setExpiry: (expiry: string) => void;
  setKind: (kind: StrategyKind) => void;
  setStrike: (index: number, strike: number) => void;
  setRatio: (index: number, ratio: number) => void;
  /** Chain-panel click: add (or stack onto) a leg. Max 4 legs (MLEG limit). */
  addLeg: (leg: { right: "C" | "P"; side: 1 | -1; strike: number }) => void;
  removeLeg: (index: number) => void;
  setTpPct: (v: number) => void;
  setSlPct: (v: number) => void;
  setTimeStopEt: (v: string) => void;
  setQty: (v: number) => void;
  setVolShift: (v: number) => void;
  setSkewBeta: (v: boolean) => void;
  /** Build a delta-targeted premium-selling structure from the live chain.
   * Returns false when the chain can't resolve it (no OTM deltas). */
  applyThetaTemplate: (id: ThetaTemplateId) => boolean;
};

function nearestStrike(strikes: number[], target: number): number {
  return strikes.reduce((best, s) => (Math.abs(s - target) < Math.abs(best - target) ? s : best), strikes[0]);
}

// ------------------------------------------------------- theta-sell system

export type ThetaTemplateId = "pcs16" | "ccs16" | "ic16" | "ic25" | "strangle16";

export type ThetaTemplate = {
  id: ThetaTemplateId;
  label: string;
  title: string;
  targetDelta: number; // |delta| of the short strike
  structure: "pcs" | "ccs" | "ic" | "strangle";
};

/** Premium-selling templates targeted by SHORT-STRIKE DELTA (probability),
 * not strike offset — the way theta sellers actually pick strikes. ~16Δ is
 * the classic one-standard-deviation short; 25Δ collects more for tighter
 * strikes. Exits default to the standard mechanics: TP at 50% of the
 * credit, stop at 100% of the credit (loss = credit received), time stop
 * 15:45 ET so nothing rides into the close. */
export const THETA_TEMPLATES: ThetaTemplate[] = [
  { id: "pcs16", label: "PUT CS 16Δ", structure: "pcs", targetDelta: 0.16,
    title: "Sell ~16Δ put + buy wing below. Bullish-neutral defined-risk income." },
  { id: "ccs16", label: "CALL CS 16Δ", structure: "ccs", targetDelta: 0.16,
    title: "Sell ~16Δ call + buy wing above. Bearish-neutral defined-risk income." },
  { id: "ic16", label: "IC 16Δ", structure: "ic", targetDelta: 0.16,
    title: "Iron condor: sell ~16Δ put AND call, wings beyond. Neutral, defined risk, max theta when price sits still." },
  { id: "ic25", label: "IC 25Δ", structure: "ic", targetDelta: 0.25,
    title: "Tighter iron condor at ~25Δ shorts — more credit, more touch risk." },
  { id: "strangle16", label: "STRGL 16Δ", structure: "strangle", targetDelta: 0.16,
    title: "Short ~16Δ strangle — UNDEFINED risk (no wings). Margin-heavy; the stop is the only protection." },
];

function contractDelta(c: Contract, spot: number, tau: number): number | null {
  if (c.delta != null) return c.delta;
  if (!c.iv || !spot || tau <= 0) return null;
  return bsDelta(spot, c.strike, tau, c.iv, c.right);
}

/** Resolve a theta template into parallel leg arrays against the chain.
 * Short strikes: OTM contract whose |delta| is closest to the target.
 * Wings: nearest listed strike ~0.8% of spot further out (≥ 1 grid step). */
export function resolveThetaLegs(
  chain: Chain,
  expiry: string,
  template: ThetaTemplate,
): { rights: ("C" | "P")[]; sides: (1 | -1)[]; strikes: number[]; ratios: number[] } | null {
  const spot = chain.spot;
  const contracts = chain.contracts.filter((c) => c.expiry === expiry);
  if (!contracts.length || !spot) return null;
  const tau = tradingHoursToExpiry(expiry, Date.now()) / TRADING_HOURS_PER_YEAR;
  const grid = [...new Set(contracts.map((c) => c.strike))].sort((a, b) => a - b);
  const atmIdx = grid.indexOf(nearestStrike(grid, spot));
  const step = grid.length > atmIdx + 1 ? grid[atmIdx + 1] - grid[atmIdx] : 1;
  const wingDist = Math.max(step, Math.round((spot * 0.008) / step) * step);

  const short = (right: "C" | "P"): number | null => {
    let best: number | null = null;
    let bestErr = Infinity;
    for (const c of contracts) {
      if (c.right !== right) continue;
      if (right === "C" ? c.strike <= spot : c.strike >= spot) continue; // OTM only
      const delta = contractDelta(c, spot, tau);
      if (delta === null) continue;
      const abs = Math.abs(delta);
      if (abs < 0.02 || abs > 0.6) continue;
      const err = Math.abs(abs - template.targetDelta);
      if (err < bestErr) {
        bestErr = err;
        best = c.strike;
      }
    }
    return best;
  };

  const wing = (shortStrike: number, dir: 1 | -1): number | null => {
    const target = shortStrike + dir * wingDist;
    let candidate = nearestStrike(grid, target);
    if (candidate === shortStrike) {
      const next = grid.filter((s) => (dir > 0 ? s > shortStrike : s < shortStrike));
      candidate = dir > 0 ? next[0] : next[next.length - 1];
    }
    return candidate ?? null;
  };

  const put = template.structure !== "ccs" ? short("P") : null;
  const call = template.structure !== "pcs" ? short("C") : null;

  switch (template.structure) {
    case "pcs": {
      if (put === null) return null;
      const w = wing(put, -1);
      if (w === null) return null;
      return { rights: ["P", "P"], sides: [1, -1], strikes: [w, put], ratios: [1, 1] };
    }
    case "ccs": {
      if (call === null) return null;
      const w = wing(call, 1);
      if (w === null) return null;
      return { rights: ["C", "C"], sides: [-1, 1], strikes: [call, w], ratios: [1, 1] };
    }
    case "ic": {
      if (put === null || call === null) return null;
      const wp = wing(put, -1);
      const wc = wing(call, 1);
      if (wp === null || wc === null) return null;
      return {
        rights: ["P", "P", "C", "C"],
        sides: [1, -1, -1, 1],
        strikes: [wp, put, call, wc],
        ratios: [1, 1, 1, 1],
      };
    }
    case "strangle": {
      if (put === null || call === null) return null;
      return { rights: ["P", "C"], sides: [-1, -1], strikes: [put, call], ratios: [1, 1] };
    }
  }
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

function templateArrays(kind: StrategyKind): {
  rights: ("C" | "P")[];
  sides: (1 | -1)[];
} {
  return {
    rights: STRATEGIES[kind].legs.map((l) => l.right),
    sides: STRATEGIES[kind].legs.map((l) => l.side),
  };
}

export const useStrategyStore = create<StrategyState>((set, get) => ({
  chain: null,
  chainError: null,
  expiry: null,
  kind: "long_call",
  strikes: [],
  ratios: defaultRatios("long_call"),
  rights: templateArrays("long_call").rights,
  sides: templateArrays("long_call").sides,
  modified: false,
  qty: 0,
  tpPct: 1.0,
  slPct: 0.5,
  timeStopEt: "15:50",
  volShift: 0,
  skewBeta: true,

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
      let { strikes, ratios, rights, sides, modified } = state;
      const chainStrikes = new Set(
        data.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike),
      );
      const valid =
        strikes.length > 0 &&
        strikes.length === ratios.length &&
        strikes.length === rights.length &&
        strikes.length === sides.length &&
        strikes.every((s) => chainStrikes.has(s));
      if (!valid) {
        strikes = expiry ? defaultStrikes(data, expiry, state.kind) : [];
        ratios = defaultRatios(state.kind);
        ({ rights, sides } = templateArrays(state.kind));
        modified = false;
      }
      set({ chain: data, chainError: null, expiry, strikes, ratios, rights, sides, modified });
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
      ...templateArrays(kind),
      modified: false,
    });
  },
  setKind: (kind) => {
    const { chain, expiry } = get();
    set({
      kind,
      strikes: chain && expiry ? defaultStrikes(chain, expiry, kind) : [],
      ratios: defaultRatios(kind),
      ...templateArrays(kind),
      modified: false,
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
  addLeg: ({ right, side, strike }) =>
    set((s) => {
      // Stack onto an identical leg first (ratio up to 9).
      const existing = s.strikes.findIndex(
        (k, i) => k === strike && s.rights[i] === right && s.sides[i] === side,
      );
      if (existing >= 0) {
        const ratios = [...s.ratios];
        ratios[existing] = Math.min((ratios[existing] ?? 1) + 1, 9);
        return { ratios, modified: true };
      }
      if (s.strikes.length >= 4) return {}; // MLEG order limit
      return {
        strikes: [...s.strikes, strike],
        ratios: [...s.ratios, 1],
        rights: [...s.rights, right],
        sides: [...s.sides, side],
        modified: true,
      };
    }),
  removeLeg: (index) =>
    set((s) => {
      if (s.strikes.length <= 1) return {}; // never drop below one leg
      const drop = <T,>(arr: T[]) => arr.filter((_, i) => i !== index);
      return {
        strikes: drop(s.strikes),
        ratios: drop(s.ratios),
        rights: drop(s.rights),
        sides: drop(s.sides),
        modified: true,
      };
    }),
  setTpPct: (v) => set({ tpPct: Math.max(0.05, Math.min(v, 10)) }),
  // SL beyond 100% of premium is meaningless for debit structures but is
  // THE standard stop for credit ones (stop at 1-2x the credit received).
  setSlPct: (v) => set({ slPct: Math.max(0.05, Math.min(v, 3.0)) }),
  setTimeStopEt: (v) => set({ timeStopEt: v }),
  setQty: (v) => set({ qty: Math.max(0, Math.min(v, 100)) }),
  setVolShift: (v) => set({ volShift: Math.max(-0.5, Math.min(v, 0.5)) }),
  setSkewBeta: (v) => set({ skewBeta: v }),
  applyThetaTemplate: (id) => {
    const { chain, expiry } = get();
    const template = THETA_TEMPLATES.find((t) => t.id === id);
    if (!chain || !template) return false;
    // Try the active expiry first, then roll FORWARD: a 0DTE chain after
    // the close (or at tau≈0) has step-function deltas — nothing sellable —
    // so the template moves to the next expiry that actually resolves.
    const candidates = [
      ...(expiry ? [expiry] : []),
      ...chain.expirations.filter((e) => e !== expiry),
    ];
    for (const candidate of candidates) {
      const resolved = resolveThetaLegs(chain, candidate, template);
      if (!resolved) continue;
      set({
        ...resolved,
        expiry: candidate,
        modified: true,
        // Standard premium-selling mechanics: take profit at 50% of the
        // credit, stop when the loss equals the credit received, and never
        // carry into the close.
        tpPct: 0.5,
        slPct: 1.0,
        timeStopEt: "15:45",
      });
      return true;
    }
    return false;
  },
}));

/** Trading hours from now until today's HH:MM ET (clamped to [0, 6.5]). */
export function timeStopHoursFromEt(timeStopEt: string): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  const nowMin = Number(parts.hour === "24" ? 0 : parts.hour) * 60 + Number(parts.minute);
  const [h, m] = timeStopEt.split(":").map(Number);
  return Math.max(0, Math.min((h * 60 + m - nowMin) / 60, 6.5));
}

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

/** Build priced legs for the current selection; null if incomplete.
 * rights/sides override the preset template (custom chain-built positions);
 * without them the preset template applies. */
export function buildLegs(state: {
  chain: Chain | null;
  expiry: string | null;
  kind: StrategyKind;
  strikes: number[];
  ratios?: number[];
  rights?: ("C" | "P")[];
  sides?: (1 | -1)[];
}): StrategyLeg[] | null {
  const { chain, expiry, kind, strikes } = state;
  if (!chain || !expiry || !strikes.length) return null;
  const template = STRATEGIES[kind].legs;
  const custom =
    state.rights &&
    state.sides &&
    state.rights.length === strikes.length &&
    state.sides.length === strikes.length;
  if (!custom && strikes.length !== template.length) return null;
  const rights = custom ? state.rights! : template.map((l) => l.right);
  const sides = custom ? state.sides! : template.map((l) => l.side);
  const ratios =
    state.ratios && state.ratios.length === strikes.length
      ? state.ratios
      : custom
        ? strikes.map(() => 1)
        : template.map((l) => l.ratio);
  const legs: StrategyLeg[] = [];
  for (let i = 0; i < strikes.length; i++) {
    const contract = findContract(chain, expiry, rights[i], strikes[i]);
    if (!contract || contract.mid <= 0 || contract.iv <= 0) return null;
    legs.push({
      right: rights[i],
      strike: strikes[i],
      qty: ratios[i],
      side: sides[i],
      entry: contract.mid,
      iv: contract.iv,
      symbol: contract.symbol,
      expiry,
      halfSpread:
        contract.ask > contract.bid && contract.bid > 0
          ? (contract.ask - contract.bid) / 2
          : Math.max(contract.mid * 0.05, 0.01), // no two-sided quote: assume 10% width
    });
  }
  return legs;
}

export function hoursToExpiry(expiry: string): number {
  return tradingHoursToExpiry(expiry, Date.now());
}

/** Per-right IV smile [strike, iv] for the active expiry, from live quotes. */
export function smileFromChain(
  chain: Chain | null,
  expiry: string | null,
): { C: [number, number][]; P: [number, number][] } | null {
  if (!chain || !expiry) return null;
  const build = (right: "C" | "P"): [number, number][] =>
    chain.contracts
      .filter((c) => c.expiry === expiry && c.right === right && c.iv > 0.005)
      .map((c) => [c.strike, c.iv] as [number, number])
      .sort((a, b) => a[0] - b[0]);
  const smiles = { C: build("C"), P: build("P") };
  if (smiles.C.length < 2 && smiles.P.length < 2) return null;
  return smiles;
}

/** Strikes available for the active expiry, sorted. */
export function availableStrikes(chain: Chain | null, expiry: string | null): number[] {
  if (!chain || !expiry) return [];
  return [...new Set(chain.contracts.filter((c) => c.expiry === expiry).map((c) => c.strike))].sort(
    (a, b) => a - b,
  );
}
