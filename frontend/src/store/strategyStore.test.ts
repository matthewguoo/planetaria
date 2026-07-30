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

  it("cash-secured naked-put BP binds honestly, with the risk-budget gap surfaced", () => {
    // Risk reversal with a short 750P. Broker-verified: Alpaca cash-secures
    // uncovered short puts (~strike*100/set), so a 25% BP cap on $100k allows
    // ZERO sets — and the reason string must say the stop budget alone would
    // allow more, plus what to do about it.
    const legs: Leg[] = [
      { right: "P", strike: 750, qty: 1, side: -1, entry: 9.3, iv: 0.2 },
      { right: "C", strike: 744, qty: 1, side: 1, entry: 1.6, iv: 0.2 },
    ];
    const entry = positionEntryCost(legs); // -7.7 credit
    const sl = entry - 3.95; // stop $395/set below entry
    const capped = computeSizingClient(legs, 100_000, 0.02, sl, 0.25, 743.57);
    expect(capped.contracts).toBe(0);
    expect(capped.riskBudgetContracts).toBe(5);
    expect(capped.reasons.some((r) => /cash-secured/.test(r) && /allows 5/.test(r))).toBe(true);
    // Raising the BP cap to 100% admits what the broker actually permits: 1.
    const full = computeSizingClient(legs, 100_000, 0.02, sl, 1.0, 743.57);
    expect(full.contracts).toBe(1);
    expect(full.entryCost).toBeCloseTo(75_000, 0);
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

  it("at spot0 the scenario IV is the leg's own IV — zero correction by construction", async () => {
    const { scenarioIv } = await import("../lib/optionsMath");
    expect(scenarioIv(leg, 450, 450, smiles)).toBeCloseTo(0.24, 9);
  });

  it("when spot rallies, the strike rides the smile SHAPE down (anchored)", async () => {
    const { scenarioIv } = await import("../lib/optionsMath");
    // Spot 450 -> 460: moneyness-equivalent strike 450; smile(450)=0.20,
    // smile(460)=0.24 -> correction -0.04 on the leg's own 0.24.
    expect(scenarioIv(leg, 460, 450, smiles)).toBeCloseTo(0.2, 2);
    // No smile -> frozen leg IV (plain BSM).
    expect(scenarioIv(leg, 460, 450, null)).toBeCloseTo(0.24, 9);
  });

  it("smoothSmile recovers a quadratic from sawtooth noise and passes small sets through", async () => {
    const { smoothSmile } = await import("../lib/optionsMath");
    const spot = 450;
    // True smile: 0.2 + 0.1x + 3x^2 in log-moneyness, plus alternating noise.
    const strikes = [430, 435, 440, 445, 450, 455, 460, 465, 470];
    const noisy = strikes.map((k, i) => {
      const x = Math.log(k / spot);
      return [k, 0.2 + 0.1 * x + 3 * x * x + (i % 2 === 0 ? 0.04 : -0.04)] as [number, number];
    });
    const smooth = smoothSmile(noisy, spot);
    for (const [k, iv] of smooth) {
      const x = Math.log(k / spot);
      // ±0.04 alternating noise over 9 points: the fit should at least halve
      // it everywhere (sawtooth itself would leave full ±0.04 excursions).
      expect(Math.abs(iv - (0.2 + 0.1 * x + 3 * x * x))).toBeLessThan(0.025);
    }
    // Too few points: raw passthrough.
    const few: [number, number][] = [[445, 0.2], [450, 0.21], [455, 0.25]];
    expect(smoothSmile(few, spot)).toEqual(few);
  });

  it("a polluted smile cannot inject phantom P/L at (now, spot) — regression", async () => {
    const { scenarioIv, positionPlSmile, bsPrice } = await import("../lib/optionsMath");
    // Off-hours failure mode: fit says 30% vol while the leg's own mid
    // implies 10%. Anchoring must keep t=0 value AT the leg's market price.
    const polluted = {
      C: [[430, 0.30], [450, 0.30], [470, 0.30]] as [number, number][],
      P: [[430, 0.30], [450, 0.30], [470, 0.30]] as [number, number][],
    };
    const tau = 13 / (252 * 6.5);
    const entry = bsPrice(450, 455, tau, 0.10, "C");
    const cheapLeg = { right: "C" as const, strike: 455, qty: 1, side: 1 as const, entry, iv: 0.10 };
    expect(scenarioIv(cheapLeg, 450, 450, polluted)).toBeCloseTo(0.10, 9);
    expect(positionPlSmile([cheapLeg], 450, tau, 450, polluted)).toBeCloseTo(0, 9);
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

describe("every strategy preset resolves on a live chain", () => {
  const chain = syntheticChain();
  const expiry = chain.expirations[0];

  for (const kind of Object.keys(STRATEGIES) as (keyof typeof STRATEGIES)[]) {
    it(`${kind} builds priced legs matching its template`, () => {
      useStrategyStore.setState({ chain, expiry, modified: false });
      useStrategyStore.getState().setKind(kind);
      const s = useStrategyStore.getState();
      const legs = buildLegs(s);
      const template = STRATEGIES[kind].legs;
      expect(legs).not.toBeNull();
      expect(legs!).toHaveLength(template.length);
      expect(template.length).toBeLessThanOrEqual(4); // Alpaca MLEG limit
      template.forEach((t, i) => {
        expect(legs![i].right).toBe(t.right);
        expect(legs![i].side).toBe(t.side);
        expect(legs![i].qty).toBe(t.ratio);
        expect(legs![i].entry).toBeGreaterThan(0);
        expect(legs![i].iv).toBeGreaterThan(0);
      });
      // Strikes are monotone with template offsets (no crossed structures).
      const offsets = template.map((t) => t.offset);
      const strikesInOffsetOrder = [...s.strikes];
      offsets.forEach((off, i) => {
        offsets.forEach((off2, j) => {
          if (off < off2) expect(strikesInOffsetOrder[i]).toBeLessThan(strikesInOffsetOrder[j]);
        });
      });
    });
  }
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

  it("decRatio decrements above 1 and removes the leg at 1", () => {
    freshStore();
    useStrategyStore.getState().addLeg({ right: "P", side: -1, strike: 447.5 });
    useStrategyStore.getState().setRatio(1, 2);
    useStrategyStore.getState().decRatio(1); // 2 -> 1, leg stays
    expect(useStrategyStore.getState().strikes).toHaveLength(2);
    expect(useStrategyStore.getState().ratios[1]).toBe(1);
    useStrategyStore.getState().decRatio(1); // at 1 -> leg removed
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([452.5]);
    expect(s.rights).toEqual(["C"]);
    // Last remaining leg survives the minus (>= 1 leg invariant).
    useStrategyStore.getState().decRatio(0);
    expect(useStrategyStore.getState().strikes).toHaveLength(1);
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

describe("edited flag / preset re-selection (sticky-selector fix)", () => {
  const chain = syntheticChain();
  const expiry = chain.expirations[0];

  function freshStore() {
    useStrategyStore.setState({
      chain, expiry, modified: false, edited: false,
    });
    useStrategyStore.getState().setKind("call_debit_spread");
  }

  it("dragging a strike marks edited; re-selecting the SAME preset re-conjures defaults", () => {
    freshStore();
    const defaults = [...useStrategyStore.getState().strikes];
    useStrategyStore.getState().setStrike(0, 440);
    useStrategyStore.getState().setStrike(1, 470);
    let s = useStrategyStore.getState();
    expect(s.edited).toBe(true);
    expect(s.modified).toBe(false); // strikes moved, leg SET unchanged
    expect(s.strikes).not.toEqual(defaults);
    // The UI can now re-fire setKind with the same kind (sentinel value).
    useStrategyStore.getState().setKind("call_debit_spread");
    s = useStrategyStore.getState();
    expect(s.edited).toBe(false);
    expect(s.strikes).toEqual(defaults);
  });

  it("ratio tweaks mark edited; setExpiry clears it", () => {
    freshStore();
    useStrategyStore.getState().setRatio(0, 2);
    expect(useStrategyStore.getState().edited).toBe(true);
    useStrategyStore.getState().setExpiry(expiry);
    expect(useStrategyStore.getState().edited).toBe(false);
  });

  it("setStrike/setRatio ignore stale out-of-range indices (dead drag targets)", () => {
    freshStore();
    useStrategyStore.getState().setKind("long_call"); // 1 leg
    // A strike drag armed on leg 3 of the previous 2-leg spread must not
    // punch a sparse hole into the new single-leg arrays.
    useStrategyStore.getState().setStrike(3, 455);
    useStrategyStore.getState().setRatio(3, 2);
    const s = useStrategyStore.getState();
    expect(s.strikes).toHaveLength(1);
    expect(s.ratios).toHaveLength(1);
    expect(s.edited).toBe(false); // stale writes don't dirty the selection
    expect(buildLegs(s)).not.toBeNull();
  });
});

describe("theta-sell templates", () => {
  const spot = 450;
  const expiry = "2099-01-15";
  const callDeltas: Record<number, number> = {
    455: 0.4, 460: 0.3, 465: 0.25, 470: 0.16, 475: 0.1, 480: 0.06, 485: 0.04, 490: 0.03,
  };
  const putDeltas: Record<number, number> = {
    445: -0.4, 440: -0.3, 435: -0.25, 430: -0.16, 425: -0.1, 420: -0.06, 415: -0.04, 410: -0.03,
  };
  const contracts = [];
  for (let k = 405; k <= 495; k += 5) {
    for (const right of ["C", "P"] as const) {
      const delta = right === "C" ? (callDeltas[k] ?? (k <= spot ? 0.7 : 0.02)) : (putDeltas[k] ?? (k >= spot ? -0.7 : -0.02));
      contracts.push({
        symbol: `X${right}${k}`, right, strike: k, expiry,
        bid: 1, ask: 1.1, mid: 1.05, iv: 0.2, delta,
      });
    }
  }
  const chain = {
    underlying: "SPY", spot, asof: Date.now(), expirations: [expiry], contracts, demo: true,
  } as never;

  function primed() {
    useStrategyStore.setState({ chain, expiry, tpPct: 1.0, slPct: 0.5, timeStopEt: "15:50" });
  }

  it("IC 16Δ sells the 16-delta strikes with wings one width out", () => {
    primed();
    expect(useStrategyStore.getState().applyThetaTemplate("ic16")).toBe(true);
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([425, 430, 470, 475]);
    expect(s.rights).toEqual(["P", "P", "C", "C"]);
    expect(s.sides).toEqual([1, -1, -1, 1]);
    expect(s.modified).toBe(true);
    expect(s.tpPct).toBe(0.5);
    expect(s.slPct).toBe(1.0);
    expect(s.timeStopEt).toBe("15:45");
  });

  it("IC 25Δ picks tighter shorts", () => {
    primed();
    useStrategyStore.getState().applyThetaTemplate("ic25");
    expect(useStrategyStore.getState().strikes).toEqual([430, 435, 465, 470]);
  });

  it("put credit spread: long wing below the short put", () => {
    primed();
    useStrategyStore.getState().applyThetaTemplate("pcs16");
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([425, 430]);
    expect(s.sides).toEqual([1, -1]);
    expect(s.rights).toEqual(["P", "P"]);
  });

  it("call credit spread: long wing above the short call", () => {
    primed();
    useStrategyStore.getState().applyThetaTemplate("ccs16");
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([470, 475]);
    expect(s.sides).toEqual([-1, 1]);
  });

  it("short strangle has no wings and both legs short", () => {
    primed();
    useStrategyStore.getState().applyThetaTemplate("strangle16");
    const s = useStrategyStore.getState();
    expect(s.strikes).toEqual([430, 470]);
    expect(s.sides).toEqual([-1, -1]);
    expect(s.rights).toEqual(["P", "C"]);
  });

  it("fails gracefully with no chain", () => {
    useStrategyStore.setState({ chain: null });
    expect(useStrategyStore.getState().applyThetaTemplate("ic16")).toBe(false);
  });

  it("slPct clamp allows credit-style stops up to 300%", () => {
    useStrategyStore.getState().setSlPct(2.0);
    expect(useStrategyStore.getState().slPct).toBe(2.0);
    useStrategyStore.getState().setSlPct(9);
    expect(useStrategyStore.getState().slPct).toBe(3.0);
  });
});

describe("theta template expiry roll-forward", () => {
  it("skips a dead 0DTE (step-function deltas) and resolves the next expiry", () => {
    const spot = 450;
    const dead = "2099-01-14";
    const live = "2099-01-15";
    const contracts: object[] = [];
    for (let k = 405; k <= 495; k += 5) {
      for (const right of ["C", "P"] as const) {
        // Dead expiry: binary deltas (tau=0). Live expiry: usable ones.
        const itm = right === "C" ? k < spot : k > spot;
        contracts.push({ symbol: `D${right}${k}`, right, strike: k, expiry: dead,
          bid: 1, ask: 1.1, mid: 1.05, iv: 0.2,
          delta: right === "C" ? (itm ? 1 : 0) : (itm ? -1 : 0) });
        const dist = Math.abs(k - spot);
        const mag = Math.max(0.02, 0.5 - dist * 0.017);
        contracts.push({ symbol: `L${right}${k}`, right, strike: k, expiry: live,
          bid: 1, ask: 1.1, mid: 1.05, iv: 0.2,
          delta: right === "C" ? (k <= spot ? 0.7 : mag) : (k >= spot ? -0.7 : -mag) });
      }
    }
    useStrategyStore.setState({
      chain: { underlying: "SPY", spot, asof: Date.now(), expirations: [dead, live], contracts, demo: true } as never,
      expiry: dead,
    });
    expect(useStrategyStore.getState().applyThetaTemplate("ic16")).toBe(true);
    const s = useStrategyStore.getState();
    expect(s.expiry).toBe(live);
    expect(s.strikes).toHaveLength(4);
    expect(s.sides).toEqual([1, -1, -1, 1]);
  });
});
