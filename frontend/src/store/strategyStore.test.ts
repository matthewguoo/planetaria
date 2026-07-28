/**
 * Preset integrity: every strategy must produce a complete, priced leg set
 * from a realistic chain, with sane structure (leg counts, credit/debit
 * orientation, editable ratios), and the credit-aware sizing mirror must
 * agree with the backend's semantics.
 */

import { describe, expect, it } from "vitest";
import { computeSizingClient } from "../lib/analytics";
import { positionEntryCost, structuralMaxLoss, type Leg } from "../lib/optionsMath";
import {
  buildLegs,
  defaultRatios,
  defaultStrikes,
  STRATEGIES,
  useStrategyStore,
  type Chain,
  type StrategyKind,
} from "./strategyStore";

function syntheticChain(spot = 450): Chain {
  const expiry = "2026-07-31";
  const contracts = [];
  for (let k = spot - 30; k <= spot + 30; k += 2.5) {
    for (const right of ["C", "P"] as const) {
      const intrinsic = Math.max(right === "C" ? spot - k : k - spot, 0);
      // Time value decays away from ATM so spreads carry realistic debits/credits.
      const timeValue = 3.0 * Math.exp(-Math.abs(k - spot) / 8);
      const mid = Math.max(intrinsic + timeValue, 0.05);
      contracts.push({
        symbol: `SPY260731${right}${String(Math.round(k * 1000)).padStart(8, "0")}`,
        right,
        strike: k,
        expiry,
        bid: mid - 0.05,
        ask: mid + 0.05,
        mid,
        iv: 0.2,
        delta: null,
      });
    }
  }
  return {
    underlying: "SPY",
    spot,
    asof: Date.now(),
    expirations: [expiry],
    contracts,
    demo: true,
  };
}

const KINDS = Object.keys(STRATEGIES) as StrategyKind[];

describe("strategy presets", () => {
  const chain = syntheticChain();
  const expiry = chain.expirations[0];

  it("covers the common strategy families", () => {
    expect(KINDS.length).toBeGreaterThanOrEqual(15);
    for (const required of [
      "long_call", "long_put", "call_debit_spread", "put_debit_spread",
      "call_credit_spread", "put_credit_spread", "long_straddle", "long_strangle",
      "short_straddle", "short_strangle", "iron_condor", "iron_butterfly",
      "call_butterfly", "put_butterfly", "short_put",
    ]) {
      expect(KINDS).toContain(required);
    }
  });

  it.each(KINDS)("%s builds a complete priced leg set", (kind) => {
    const strikes = defaultStrikes(chain, expiry, kind);
    const ratios = defaultRatios(kind);
    expect(strikes.length).toBe(STRATEGIES[kind].legs.length);
    expect(strikes.length).toBeLessThanOrEqual(4); // MLEG order limit
    const legs = buildLegs({ chain, expiry, kind, strikes, ratios });
    expect(legs).not.toBeNull();
    expect(legs!.length).toBe(STRATEGIES[kind].legs.length);
    for (const leg of legs!) {
      expect(leg.entry).toBeGreaterThan(0);
      expect(leg.iv).toBeGreaterThan(0);
      expect(leg.qty).toBeGreaterThanOrEqual(1);
    }
  });

  it("credit presets produce net credits, debit presets net debits", () => {
    const entryOf = (kind: StrategyKind) => {
      const legs = buildLegs({
        chain, expiry, kind,
        strikes: defaultStrikes(chain, expiry, kind),
        ratios: defaultRatios(kind),
      })!;
      return positionEntryCost(legs);
    };
    for (const kind of ["long_call", "long_put", "call_debit_spread", "put_debit_spread",
                        "long_straddle", "long_strangle"] as StrategyKind[]) {
      expect(entryOf(kind)).toBeGreaterThan(0);
    }
    for (const kind of ["call_credit_spread", "put_credit_spread", "iron_condor",
                        "iron_butterfly", "short_straddle", "short_strangle",
                        "short_put"] as StrategyKind[]) {
      expect(entryOf(kind)).toBeLessThan(0);
    }
  });

  it("butterfly middle legs carry ratio 2", () => {
    expect(defaultRatios("call_butterfly")).toEqual([1, 2, 1]);
    expect(defaultRatios("put_butterfly")).toEqual([1, 2, 1]);
  });
});

describe("structuralMaxLoss (mirror of backend)", () => {
  it("long call: max loss = debit", () => {
    const legs: Leg[] = [{ right: "C", strike: 455, qty: 1, side: 1, entry: 2, iv: 0.2 }];
    expect(structuralMaxLoss(legs)).toBeCloseTo(2.0, 9);
  });
  it("call credit spread: width - credit", () => {
    const legs: Leg[] = [
      { right: "C", strike: 450, qty: 1, side: -1, entry: 2.0, iv: 0.2 },
      { right: "C", strike: 455, qty: 1, side: 1, entry: 0.8, iv: 0.2 },
    ];
    expect(structuralMaxLoss(legs)).toBeCloseTo(5 - 1.2, 9);
  });
  it("naked short call: unbounded", () => {
    const legs: Leg[] = [{ right: "C", strike: 450, qty: 1, side: -1, entry: 2, iv: 0.2 }];
    expect(structuralMaxLoss(legs)).toBeNull();
  });
  it("short put: bounded by strike", () => {
    const legs: Leg[] = [{ right: "P", strike: 450, qty: 1, side: -1, entry: 3, iv: 0.2 }];
    expect(structuralMaxLoss(legs)).toBeCloseTo(447, 9);
  });
});

describe("credit-aware sizing", () => {
  it("sizes a defined-risk credit spread by margin", () => {
    const legs: Leg[] = [
      { right: "C", strike: 450, qty: 1, side: -1, entry: 2.0, iv: 0.2 },
      { right: "C", strike: 455, qty: 1, side: 1, entry: 0.8, iv: 0.2 },
    ];
    // entry = -1.2 credit; SL at -2.4 -> $120 risk/set
    const s = computeSizingClient(legs, 25_000, 0.02, -2.4, 0.25);
    expect(s.contracts).toBeGreaterThanOrEqual(1);
    expect(s.maxLossAtStop).toBeCloseTo(s.contracts * 120, 6);
    expect(s.maxLossStructural).toBeCloseTo(s.contracts * 380, 6);
  });
  it("flags undefined-risk shorts", () => {
    const legs: Leg[] = [{ right: "C", strike: 450, qty: 1, side: -1, entry: 2, iv: 0.2 }];
    const s = computeSizingClient(legs, 25_000, 0.02, -3.0, 0.25);
    expect(s.contracts).toBe(5); // $100 risk/set vs $500 budget
    expect(s.reasons.some((r) => r.includes("undefined risk"))).toBe(true);
  });
  it("refuses inverted stops", () => {
    const legs: Leg[] = [{ right: "C", strike: 455, qty: 1, side: 1, entry: 2, iv: 0.2 }];
    expect(computeSizingClient(legs, 25_000, 0.02, 2.5, 0.25).contracts).toBe(0);
  });
});

describe("smile-aware scenario pricing (sticky moneyness)", () => {
  const smiles = {
    C: [[430, 0.30], [440, 0.25], [450, 0.20], [460, 0.24], [470, 0.29]] as [number, number][],
    P: [[430, 0.32], [440, 0.26], [450, 0.21], [460, 0.25], [470, 0.30]] as [number, number][],
  };
  const leg: Leg = { right: "C", strike: 460, qty: 1, side: 1, entry: 2, iv: 0.24 };

  it("interpolates the smile linearly and clamps at the wings", async () => {
    const { smileIv } = await import("../lib/optionsMath");
    expect(smileIv(smiles.C, 450)).toBeCloseTo(0.2, 9);
    expect(smileIv(smiles.C, 455)).toBeCloseTo(0.22, 9);
    expect(smileIv(smiles.C, 400)).toBeCloseTo(0.3, 9); // below range -> clamp
    expect(smileIv(smiles.C, 500)).toBeCloseTo(0.29, 9); // above range -> clamp
    expect(smileIv([[450, 0.2]], 450)).toBeNull(); // <2 points unusable
  });

  it("at spot0 the scenario IV matches today's smile at the leg strike", async () => {
    const { scenarioIv } = await import("../lib/optionsMath");
    expect(scenarioIv(leg, 450, 450, smiles)).toBeCloseTo(0.24, 9);
  });

  it("when spot rallies, the strike rolls DOWN the moneyness smile", async () => {
    const { scenarioIv } = await import("../lib/optionsMath");
    // Spot 450 -> 460: leg struck 460 now sits ATM-equivalent (460*450/460=450).
    expect(scenarioIv(leg, 460, 450, smiles)).toBeCloseTo(0.2, 2);
    // No smile -> frozen leg IV (plain BSM).
    expect(scenarioIv(leg, 460, 450, null)).toBeCloseTo(0.24, 9);
  });

  it("smile-aware value differs from frozen-IV value off-spot but agrees at expiry", async () => {
    const { positionValueSmile, positionValue, payoffAtExpiry, positionEntryCost } =
      await import("../lib/optionsMath");
    const legs = [leg];
    const tau = 6.5 / (252 * 6.5);
    const frozen = positionValue(legs, 462, tau);
    const smiled = positionValueSmile(legs, 462, tau, 450, smiles);
    expect(smiled).not.toBeCloseTo(frozen, 4); // the correction is real
    // At tau=0 both collapse to intrinsic — the payoff anchor is model-free.
    expect(positionValueSmile(legs, 462, 0, 450, smiles)).toBeCloseTo(
      payoffAtExpiry(legs, 462) + positionEntryCost(legs), 9,
    );
  });
});

describe("custom leg composition (chain-panel clicks)", () => {
  const chain = syntheticChain();
  const expiry = chain.expirations[0];

  function freshStore() {
    const store = useStrategyStore.getState();
    useStrategyStore.setState({
      chain,
      expiry,
      kind: "long_call",
      strikes: [452.5],
      ratios: [1],
      rights: ["C"],
      sides: [1],
      modified: false,
    });
    return store;
  }

  it("addLeg appends a new leg and marks the composition custom", () => {
    freshStore();
    useStrategyStore.getState().addLeg({ right: "P", side: -1, strike: 447.5 });
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([452.5, 447.5]);
    expect(s.rights).toEqual(["C", "P"]);
    expect(s.sides).toEqual([1, -1]);
    expect(s.modified).toBe(true);
    const legs = buildLegs({
      chain, expiry, kind: s.kind,
      strikes: s.strikes, ratios: s.ratios, rights: s.rights, sides: s.sides,
    });
    expect(legs).not.toBeNull();
    expect(legs![1].side).toBe(-1);
    expect(legs![1].right).toBe("P");
  });

  it("re-adding an identical leg stacks ratio instead of duplicating", () => {
    freshStore();
    useStrategyStore.getState().addLeg({ right: "C", side: 1, strike: 452.5 });
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([452.5]);
    expect(s.ratios).toEqual([2]);
  });

  it("caps at 4 legs (MLEG limit)", () => {
    freshStore();
    for (const k of [447.5, 450, 455, 457.5]) {
      useStrategyStore.getState().addLeg({ right: "P", side: -1, strike: k });
    }
    expect(useStrategyStore.getState().strikes).toHaveLength(4);
  });

  it("removeLeg splices all parallel arrays and never drops below one leg", () => {
    freshStore();
    useStrategyStore.getState().addLeg({ right: "P", side: -1, strike: 447.5 });
    useStrategyStore.getState().removeLeg(0);
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([447.5]);
    expect(s.rights).toEqual(["P"]);
    expect(s.sides).toEqual([-1]);
    useStrategyStore.getState().removeLeg(0);
    expect(useStrategyStore.getState().strikes).toHaveLength(1);
  });

  it("selecting a preset resets the custom composition", () => {
    freshStore();
    useStrategyStore.getState().addLeg({ right: "P", side: -1, strike: 447.5 });
    useStrategyStore.getState().setKind("iron_condor");
    const s = useStrategyStore.getState();
    expect(s.modified).toBe(false);
    expect(s.strikes).toHaveLength(4);
    expect(s.sides).toEqual([1, -1, -1, 1]);
  });
});
