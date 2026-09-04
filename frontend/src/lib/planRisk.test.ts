import { describe, expect, it } from "vitest";

import {
  planMaxLoss,
  planPremiumAtRisk,
  planProtection,
  planStopRisk,
  planUnstoppedNotional,
} from "./planRisk";

const longPut = {
  fill_premium: 2.07,
  entry_limit: 2.07,
  sl_premium: null,
  filled_qty: 1,
  qty: 1,
  asset_class: "option",
  legs: [{ side: 1 }],
};

describe("planRisk", () => {
  it("intrinsic-cap long option: whole premium at risk, no stop risk", () => {
    expect(planStopRisk(longPut)).toBe(0);
    expect(planPremiumAtRisk(longPut)).toBeCloseTo(207);
    expect(planMaxLoss(longPut)).toBeCloseTo(207);
    expect(planProtection(longPut)).toBe("premium");
  });

  it("a stop moves the risk to the stop figure", () => {
    const stopped = { ...longPut, sl_premium: 1.0 };
    expect(planStopRisk(stopped)).toBeCloseTo(107);
    expect(planPremiumAtRisk(stopped)).toBe(0);
    expect(planProtection(stopped)).toBe("stop");
  });

  it("short legs are not premium-capped", () => {
    const spread = { ...longPut, legs: [{ side: 1 }, { side: -1 }] };
    expect(planPremiumAtRisk(spread)).toBe(0);
    expect(planProtection(spread)).toBe("none");
  });

  it("equity without a stop reports notional, not premium", () => {
    const shares = { entry_limit: 24.73, fill_premium: 24.73, sl_premium: null, qty: 100, filled_qty: 100, asset_class: "equity" };
    expect(planPremiumAtRisk(shares)).toBe(0);
    expect(planUnstoppedNotional(shares)).toBeCloseTo(2473);
    expect(planProtection(shares)).toBe("none");
  });

  it("partial fills use filled_qty", () => {
    expect(planPremiumAtRisk({ ...longPut, qty: 3, filled_qty: 2 })).toBeCloseTo(414);
  });
});
