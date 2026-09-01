/**
 * Parity gate: TS math mirror vs Python fixtures (tests/gen_parity_fixtures.py).
 * BS + cdf agree to 1e-9; EV integration to 1e-6 (quadrature summation order).
 */
import { describe, expect, it } from "vitest";
import fixtures from "./__fixtures__/parity.json";
import {
  bsPrice,
  normCdf,
  payoffAtExpiry,
  positionPl,
  probItm,
  probTouch,
  terminalEv,
  type Leg,
} from "./optionsMath";

describe("python/ts parity", () => {
  it("normCdf", () => {
    for (const c of fixtures.norm_cdf) {
      expect(Math.abs(normCdf(c.x) - c.out)).toBeLessThan(1e-9);
    }
  });

  it("bsPrice", () => {
    for (const c of fixtures.bs) {
      const got = bsPrice(c.S, c.K, c.tau, c.sigma, c.right as "C" | "P");
      expect(Math.abs(got - c.out)).toBeLessThan(1e-9);
    }
  });

  it("probItm", () => {
    for (const c of fixtures.prob_itm) {
      const got = probItm(c.S, c.K, c.tau, c.sigma, c.right as "C" | "P");
      expect(Math.abs(got - c.out)).toBeLessThan(1e-9);
    }
  });

  it("probTouch", () => {
    for (const c of fixtures.prob_touch) {
      const got = probTouch(c.S, c.barrier, c.tau, c.sigma);
      expect(Math.abs(got - c.out)).toBeLessThan(1e-9);
    }
  });

  it("position PL / payoff / EV", () => {
    for (const c of fixtures.position) {
      const legs = c.legs as Leg[];
      expect(Math.abs(positionPl(legs, c.S, c.tau) - c.pl)).toBeLessThan(1e-9);
      expect(Math.abs(payoffAtExpiry(legs, c.S) - c.payoff)).toBeLessThan(1e-9);
      expect(Math.abs(terminalEv(legs, c.S, c.tau, 0.2, 4.5, 1.0) - c.ev)).toBeLessThan(1e-6);
    }
  });
});
