import { describe, expect, it } from "vitest";
import { addTradingHours, shareExitDayIso, tradingHoursBetween } from "./tradingTime";

// 2026-09-01 is a Tuesday. EDT: 14:32 UTC = 10:32 ET.
const T = Date.parse("2026-09-01T14:32:00Z");

describe("tradingTime", () => {
  it("measures trading hours across sessions and weekends", () => {
    expect(tradingHoursBetween(T, Date.parse("2026-09-01T15:32:00Z"))).toBeCloseTo(1, 6);
    // 10:32 -> 16:00 today (5.47h) + Wed full (6.5) + Thu until 10:32 (1.03h) = 13h
    expect(tradingHoursBetween(T, Date.parse("2026-09-03T14:32:00Z"))).toBeCloseTo(13, 6);
    // Friday 15:00 ET -> Monday 10:00 ET = 1h + 0.5h
    expect(tradingHoursBetween(Date.parse("2026-09-04T19:00:00Z"), Date.parse("2026-09-07T14:00:00Z"))).toBeCloseTo(1.5, 6);
    expect(tradingHoursBetween(T, T - 1000)).toBe(0);
  });

  it("adds trading hours, rolling over closes and weekends, snapped to 5 min", () => {
    expect(addTradingHours(T, 1)).toBe("2026-09-01T15:30:00.000Z"); // 11:32 -> 11:30 ET
    const toClose = (16 * 60 - (10 * 60 + 32)) / 60;
    expect(addTradingHours(T, toClose)).toBe("2026-09-01T20:00:00.000Z"); // the close
    expect(addTradingHours(T, toClose + 0.5)).toBe("2026-09-02T14:00:00.000Z"); // 0.5h into Wednesday -> 10:00 ET
    // Friday 15:30 ET + 2h = Monday 11:00 ET
    expect(addTradingHours(Date.parse("2026-09-04T19:30:00Z"), 2)).toBe("2026-09-07T15:00:00.000Z");
    // pre-market start snaps to the open; a weekend start to Monday's open
    expect(addTradingHours(Date.parse("2026-09-01T12:00:00Z"), 0)).toBe("2026-09-01T13:30:00.000Z");
    expect(addTradingHours(Date.parse("2026-09-05T15:00:00Z"), 0)).toBe("2026-09-07T13:30:00.000Z");
  });

  it("round-trips: hours between an instant and its offset instant", () => {
    const later = Date.parse(addTradingHours(T, 9, 1));
    expect(tradingHoursBetween(T, later)).toBeCloseTo(9, 1);
  });

  it("share exit day is 15:55 ET of the date, weekends rolled to Monday", () => {
    expect(shareExitDayIso(Date.parse("2026-09-01T14:32:00Z"))).toBe("2026-09-01T19:55:00.000Z");
    expect(shareExitDayIso(Date.parse("2026-09-05T14:32:00Z"))).toBe("2026-09-07T19:55:00.000Z");
  });
});
