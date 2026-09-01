/**
 * Monte Carlo exit simulator: deterministic (seeded) sanity checks that the
 * simulated exit mechanics match the enforcer's semantics, plus scenario
 * model (IV shock / skew beta) behavior.
 */

import { describe, expect, it } from "vitest";
import { simulateExits, type McParams } from "./mcSim";
import {
  atmSkewSlope,
  makeScenarioModel,
  positionValueModel,
  scenarioIvModel,
  type Leg,
  type Smiles,
} from "./optionsMath";

const CALL: Leg = { right: "C", strike: 452, qty: 1, side: 1, entry: 2.0, iv: 0.2 };

function base(overrides: Partial<McParams> = {}): McParams {
  return {
    legs: [CALL],
    entry: 2.0,
    tpPremium: 4.0,
    slPremium: 1.0,
    hoursToExpiry: 13,
    timeStopHours: 13,
    spot: 452,
    smiles: null,
    volShift: 0,
    skewBeta: false,
    frictionPerSet: 0,
    paths: 800,
    stepMinutes: 5,
    seed: 42,
    ...overrides,
  };
}

describe("simulateExits", () => {
  it("is deterministic for a fixed seed and exit fractions sum to 1", () => {
    const a = simulateExits(base());
    const b = simulateExits(base());
    expect(a.evPerSet).toBe(b.evPerSet);
    expect(a.pTp + a.pSl + a.pTime + a.pExpiry).toBeCloseTo(1, 9);
    expect(a.p5).toBeLessThanOrEqual(a.p50);
    expect(a.p50).toBeLessThanOrEqual(a.p95);
  });

  it("TP fills at the threshold; SL can gap through (worse than stop)", () => {
    const r = simulateExits(base({ frictionPerSet: 0 }));
    // TP exit P/L per set is exactly (tp - entry)*100 = $200 -> p95 bounded by it.
    expect(r.p95).toBeLessThanOrEqual(200 + 1e-9);
    // SL exits realize <= (sl - entry)*100 = -$100 (gap-through allowed).
    expect(r.p5).toBeLessThanOrEqual(-100 + 1e-9);
  });

  it("tight TP is hit far more often than a distant SL", () => {
    const r = simulateExits(base({ tpPremium: 2.1, slPremium: 0.2 }));
    expect(r.pTp).toBeGreaterThan(0.6);
    expect(r.pTp).toBeGreaterThan(r.pSl);
  });

  it("with unreachable brackets everything exits at the time stop", () => {
    const r = simulateExits(
      base({ tpPremium: 50, slPremium: -50, timeStopHours: 2, hoursToExpiry: 13 }),
    );
    expect(r.pTime).toBeCloseTo(1, 9);
    expect(r.avgMinutesInTrade).toBeCloseTo(120, 0);
  });

  it("friction shifts the whole distribution down dollar-for-dollar", () => {
    const clean = simulateExits(base());
    const costly = simulateExits(base({ frictionPerSet: 25 }));
    expect(costly.evPerSet).toBeCloseTo(clean.evPerSet - 25, 6);
    expect(costly.p50).toBeCloseTo(clean.p50 - 25, 6);
  });

  it("higher scenario vols raise straddle EV before exits", () => {
    const straddle: Leg[] = [
      { right: "C", strike: 452, qty: 1, side: 1, entry: 2.0, iv: 0.2 },
      { right: "P", strike: 452, qty: 1, side: 1, entry: 2.0, iv: 0.2 },
    ];
    const params = base({
      legs: straddle,
      entry: 4.0,
      tpPremium: 50,
      slPremium: -50,
      timeStopHours: 6,
    });
    const flat = simulateExits(params);
    const shocked = simulateExits({ ...params, volShift: 0.3 });
    // Vol up: long-vol position marks higher at the time stop.
    expect(shocked.evPerSet).toBeGreaterThan(flat.evPerSet);
  });
});

describe("scenario model (IV shock + skew beta)", () => {
  const smiles: Smiles = {
    C: [[430, 0.3], [440, 0.25], [450, 0.2], [460, 0.17], [470, 0.16]],
    P: [[430, 0.32], [440, 0.26], [450, 0.21], [460, 0.18], [470, 0.17]],
  };

  it("atmSkewSlope recovers a negative equity-style skew", () => {
    const slope = atmSkewSlope(smiles.C, 450);
    expect(slope).not.toBeNull();
    expect(slope!).toBeLessThan(0);
  });

  it("volShift scales scenario vols", () => {
    const leg: Leg = { right: "C", strike: 450, qty: 1, side: 1, entry: 2, iv: 0.2 };
    const flat = makeScenarioModel(smiles, 450, 0, false);
    const shocked = makeScenarioModel(smiles, 450, 0.2, false);
    expect(scenarioIvModel(leg, 450, shocked)).toBeCloseTo(
      scenarioIvModel(leg, 450, flat) * 1.2,
      9,
    );
  });

  it("skew beta raises vols on down-moves and cuts them on rallies", () => {
    const leg: Leg = { right: "P", strike: 450, qty: 1, side: 1, entry: 2, iv: 0.21 };
    const noBeta = makeScenarioModel(smiles, 450, 0, false);
    const withBeta = makeScenarioModel(smiles, 450, 0, true);
    expect(scenarioIvModel(leg, 440, withBeta)).toBeGreaterThan(scenarioIvModel(leg, 440, noBeta));
    expect(scenarioIvModel(leg, 460, withBeta)).toBeLessThan(scenarioIvModel(leg, 460, noBeta));
    // At spot0 the beta term vanishes.
    expect(scenarioIvModel(leg, 450, withBeta)).toBeCloseTo(scenarioIvModel(leg, 450, noBeta), 9);
  });

  it("a long put under selloff marks higher with skew beta on", () => {
    const put: Leg[] = [{ right: "P", strike: 450, qty: 1, side: 1, entry: 2, iv: 0.21 }];
    const tau = 6.5 / (252 * 6.5);
    const off = positionValueModel(put, 438, tau, makeScenarioModel(smiles, 450, 0, false));
    const on = positionValueModel(put, 438, tau, makeScenarioModel(smiles, 450, 0, true));
    expect(on).toBeGreaterThan(off);
  });
});
