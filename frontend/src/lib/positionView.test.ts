import { describe, expect, it } from "vitest";
import type { Plan, UntrackedPosition } from "./api";
import {
  buildPositionView,
  buildUntrackedView,
  equityPositionOfPlan,
  equityPositionOfUntracked,
} from "./positionView";

const NOW = Date.parse("2026-09-03T14:00:00Z"); // Thu 10:00 ET

const put: UntrackedPosition = {
  symbol: "NVDA260904P00230000", qty: 2, side: 1, asset_class: "option", avg_entry_price: 2.07,
  current_price: 2.51, market_value: 502, unrealized_pl: 88,
  occ: { underlying: "NVDA", expiry: "2026-09-04", right: "P", strike: 230 },
};

const shares: UntrackedPosition = {
  symbol: "PLTZ", qty: -100, side: -1, asset_class: "stock", avg_entry_price: 9.03,
  current_price: 8.56, market_value: -856, unrealized_pl: 47, occ: null,
};

const eqPlan = {
  id: "eq1", created_at: "2026-09-01T13:35:00Z", entered_at: "2026-09-01T14:32:00Z", underlying: "AVGG",
  asset_class: "equity", status: "filled", legs: [{ symbol: "AVGG", side: 1, ratio: 1, entry: 20.6, right: null, strike: null, expiry: null }],
  qty: 40, filled_qty: 40, entry_limit: 20.7, fill_premium: 20.6, tp_premium: 24, sl_premium: 18.54,
  time_stop_utc: "2026-09-26T19:55:00Z", exit_fills: null,
} as unknown as Plan;

describe("buildUntrackedView", () => {
  it("marks a long option at its live IV against the broker basis, anchored at the fill", () => {
    const v = buildUntrackedView(put, {
      iv: 0.61, enteredAt: "2026-09-01T14:32:00Z", nowMs: NOW,
      exits: { sl: null, tp: 4.0, timeStopUtc: "2026-09-04T19:50:00Z" },
    })!;
    expect(v.legs).toEqual([{ symbol: put.symbol, right: "P", strike: 230, qty: 1, side: 1, entry: 2.07, iv: 0.61 }]);
    expect(v.entryBasis).toBe(2.07);
    expect(v.qty).toBe(2);
    expect(v.anchorMs).toBe(Date.parse("2026-09-01T14:32:00Z"));
    // Tue 10:32 -> Fri 16:00: 5.47 + 6.5 + 6.5 + 6.5 = 24.97h
    expect(v.hoursTotal).toBeCloseTo(24.97, 2);
    // the time stop sits 10 minutes before the close
    expect(v.hoursTotal - v.timeStopHours).toBeCloseTo(10 / 60, 3);
    expect(v.tpPremium).toBe(4);
    expect(v.slPremium).toBeNull();
    expect(v.label).toBe("NVDA 09/04 230P ×2");
  });

  it("a short leg carries a negative basis; unknown entry time and IV degrade to now and 20%", () => {
    const v = buildUntrackedView({ ...put, qty: -1, side: -1 }, {
      iv: null, enteredAt: null, nowMs: NOW, exits: { sl: null, tp: null, timeStopUtc: null },
    })!;
    expect(v.entryBasis).toBe(-2.07);
    expect(v.legs[0].side).toBe(-1);
    expect(v.legs[0].iv).toBe(0.2);
    expect(v.anchorMs).toBe(NOW);
    expect(v.timeStopHours).toBe(v.hoursTotal);
  });

  it("shares and a zero row have no options view", () => {
    expect(buildUntrackedView(shares, { iv: null, enteredAt: null, exits: { sl: null, tp: null, timeStopUtc: null } })).toBeNull();
    expect(buildUntrackedView({ ...put, qty: 0 }, { iv: null, enteredAt: null, exits: { sl: null, tp: null, timeStopUtc: null } })).toBeNull();
  });
});

describe("equity positions", () => {
  it("a managed share plan draws its own exits, or the draft's", () => {
    const p = equityPositionOfPlan(eqPlan, null, NOW)!;
    expect(p).toMatchObject({ key: "eq1", side: 1, shares: 40, entryPx: 20.6, sl: 18.54, tp: 24, editable: true });
    expect(p.anchorMs).toBe(Date.parse("2026-09-01T14:32:00Z"));
    expect(p.timeStopMs).toBe(Date.parse("2026-09-26T19:55:00Z"));
    // 2026-09-01 10:32 -> 09-26 is a Saturday: the stop's date column counts weekdays to the 25th's close, 15:55 on the 26th adds nothing
    expect(p.timeStopHours).toBeGreaterThan(100);
    const edited = equityPositionOfPlan(eqPlan, { sl: 19.5, tp: null, timeStopUtc: null }, NOW)!;
    expect(edited.sl).toBe(19.5);
    expect(edited.tp).toBeNull();
    expect(edited.timeStopMs).toBeNull();
    expect(equityPositionOfPlan({ ...eqPlan, status: "closed" }, null, NOW)!.editable).toBe(false);
  });

  it("an untracked short stock row is a position too; option rows are not", () => {
    const p = equityPositionOfUntracked(shares, {
      enteredAt: null, nowMs: NOW, exits: { sl: -9.93, tp: -8.1, timeStopUtc: "2026-10-15T19:55:00Z" },
    })!;
    expect(p).toMatchObject({ key: "PLTZ", side: -1, shares: 100, entryPx: 9.03, sl: 9.93, tp: 8.1, editable: true });
    expect(p.anchorMs).toBe(NOW);
    expect(equityPositionOfUntracked(put, { enteredAt: null, exits: { sl: null, tp: null, timeStopUtc: null } })).toBeNull();
  });

  it("buildPositionView takes the draft's exits over the plan's", () => {
    const plan = {
      ...eqPlan, id: "op1", asset_class: "option", underlying: "NVDA", fill_premium: 2.07, entry_limit: 2.07,
      legs: [{ symbol: put.symbol, right: "P", strike: 230, expiry: "2026-09-04", side: 1, ratio: 1, entry: 2.07, iv: 0.5 }],
      tp_premium: null, sl_premium: null, time_stop_utc: "2026-09-04T19:50:00Z",
    } as unknown as Plan;
    expect(buildPositionView(plan, null, "entry")!.slPremium).toBeNull();
    const v = buildPositionView(plan, null, "entry", { sl: 1.05, tp: 3.1, timeStopUtc: "2026-09-03T18:00:00Z" })!;
    expect(v.slPremium).toBe(1.05);
    expect(v.tpPremium).toBe(3.1);
    expect(v.timeStopHours).toBeLessThan(v.hoursTotal - 6);
  });
});
