/** The options order body: the spread-optimizer choice rides along as
 * `work_spread` (null = follow the server), and the scalp HOLD chips
 * produce an ET wall-clock time N minutes out. */
import { describe, expect, it } from "vitest";
import { etTimePlusMinutes, optionsOrderPayload } from "./orderPayload";
import type { Designer } from "./useDesigner";

const designer = {
  ready: true,
  qty: 2,
  entry: 1.2345,
  tpPremium: 1.6,
  slPremium: 0.98,
  legs: [
    { symbol: "SPY260904C00650000", right: "C", strike: 650, expiry: "2026-09-04", side: 1, qty: 1, entry: 1.2345, iv: 0.2, halfSpread: 0.02 },
  ],
} as unknown as Designer;

describe("optionsOrderPayload", () => {
  it("defaults work_spread to null (follow the server setting)", () => {
    const body = optionsOrderPayload({ designer, symbol: "SPY", kind: "long_call", modified: false, timeStopEt: "15:50" }) as Record<string, unknown>;
    expect(body.work_spread).toBeNull();
    expect(body.entry_limit).toBe(1.23);
    expect(body.strategy).toBe("long_call");
    expect((body.legs as unknown[]).length).toBe(1);
  });

  it("pins the choice per order when the ticket says so", () => {
    const on = optionsOrderPayload({ designer, symbol: "SPY", kind: "long_call", modified: true, timeStopEt: "15:50", workSpread: true }) as Record<string, unknown>;
    const off = optionsOrderPayload({ designer, symbol: "SPY", kind: "long_call", modified: true, timeStopEt: "15:50", workSpread: false }) as Record<string, unknown>;
    expect(on.work_spread).toBe(true);
    expect(off.work_spread).toBe(false);
    expect(on.strategy).toBe("custom");
  });
});

describe("etTimePlusMinutes", () => {
  // 2026-09-04 14:00:00 ET (EDT, UTC-4) = 18:00:00Z
  const base = Date.UTC(2026, 8, 4, 18, 0, 0);
  it("adds minutes on the ET wall clock", () => {
    expect(etTimePlusMinutes(0, base)).toBe("14:00");
    expect(etTimePlusMinutes(20, base)).toBe("14:20");
    expect(etTimePlusMinutes(75, base)).toBe("15:15");
  });
  it("never emits the 24:xx form", () => {
    const beforeMidnight = Date.UTC(2026, 8, 5, 3, 50, 0); // 23:50 ET
    expect(etTimePlusMinutes(15, beforeMidnight)).toBe("00:05");
  });
});
