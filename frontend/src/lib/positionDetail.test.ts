import { describe, expect, it } from "vitest";

import type { OpenOrder } from "./api";
import {
  breakeven,
  breakevenPct,
  changeTodayUsd,
  closeSentence,
  contractLabel,
  countdown,
  expiryCountdown,
  groupOrdersByPlan,
  protection,
} from "./positionDetail";

describe("positionDetail", () => {
  it("labels", () => {
    expect(contractLabel([{ symbol: "NVDA260904P00230000", right: "P", strike: 230, expiry: "2026-09-04", side: 1, ratio: 1, entry: 2.07, iv: null }], "NVDA")).toBe("NVDA 09/04 230P");
    expect(contractLabel([{ symbol: "AVGG", right: null, strike: null, expiry: null, side: 1, ratio: 1, entry: 24.7, iv: null }], "AVGG")).toBe("AVGG");
    expect(contractLabel([
      { symbol: "a", right: "C", strike: 650, expiry: "2026-09-18", side: 1, ratio: 1, entry: 1, iv: null },
      { symbol: "b", right: "C", strike: 655, expiry: "2026-09-18", side: -1, ratio: 1, entry: 0.5, iv: null },
    ])).toBe("+1C650 −1C655");
  });

  it("breakeven", () => {
    expect(breakeven("P", 230, 2.07)).toBeCloseTo(227.93);
    expect(breakeven("C", 650, 1.2)).toBeCloseTo(651.2);
    expect(breakevenPct(227.93, 229.7)).toBeCloseTo(-0.77, 1);
    expect(breakevenPct(227.93, 0)).toBeNull();
  });

  it("countdowns", () => {
    const now = Date.parse("2026-09-04T14:00:00Z");
    expect(expiryCountdown("2026-09-04", now)).toEqual({ dte: 0, label: "0DTE · 6h00m" });
    expect(expiryCountdown("2026-09-18", now).label).toBe("14d");
    expect(expiryCountdown("2026-09-03", now).label).toBe("expired");
    expect(countdown("2026-09-04T14:45:00Z", now)).toBe("45m");
    expect(countdown("2026-09-04T17:30:00Z", now)).toBe("3.5h");
    expect(countdown("2026-09-20T14:00:00Z", now)).toBe("16d");
    expect(countdown("2026-09-04T13:00:00Z", now)).toBe("due");
  });

  it("today's move and protection", () => {
    expect(changeTodayUsd({ current_price: 2.51, lastday_price: 6.25, qty: 1, asset_class: "option" })).toBeCloseTo(-374);
    expect(changeTodayUsd({ current_price: 22.97, lastday_price: 23.5, qty: 100, asset_class: "stock" })).toBeCloseTo(-53);
    expect(changeTodayUsd({ current_price: null, lastday_price: 1, qty: 1, asset_class: "stock" })).toBeNull();
    expect(protection({ sl_premium: 1, asset_class: "option", legs: [{ side: 1 }] })).toBe("stop");
    expect(protection({ sl_premium: null, asset_class: "option", legs: [{ side: 1 }] })).toBe("premium");
    expect(protection({ sl_premium: null, asset_class: "equity", legs: [{ side: 1 }] })).toBe("none");
    expect(protection(null)).toBe("none");
  });

  it("orders group by plan and close sentences read as actions", () => {
    const o = (id: string, plan_id: string | null): OpenOrder =>
      ({ id, plan_id, role: plan_id ? "entry" : null, symbol: "X", side: "buy", qty: 1, filled_qty: 0, type: "limit", limit_price: 1, status: "new", submitted_at: null, legs: [] });
    const g = groupOrdersByPlan([o("a", "p1"), o("b", null), o("c", "p1")]);
    expect(g.byPlan.get("p1")?.map((x) => x.id)).toEqual(["a", "c"]);
    expect(g.loose.map((x) => x.id)).toEqual(["b"]);
    expect(closeSentence(1, 5, "market", null, false)).toBe("SELL 5 @ MKT");
    expect(closeSentence(1, 2, "limit", 1.85, false)).toBe("SELL 2 @ 1.85 LMT");
    expect(closeSentence(-1, 3, "market", null, true)).toBe("BUY ALL @ MKT");
  });
});
