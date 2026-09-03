import { describe, expect, it } from "vitest";
import { dayChange, prevCloseIndex } from "./dayChange";
import type { Bars } from "../components/Chart/scales";

// September 2026 is EDT (UTC-4): 09:30 ET = 13:30Z, 16:00 ET = 20:00Z.
const et = (day: number, hh: number, mm: number) => Date.UTC(2026, 8, day, hh + 4, mm);

function bars(ts: number[], closes: number[]): Bars {
  return {
    t: Float64Array.from(ts),
    o: Float64Array.from(closes),
    h: Float64Array.from(closes),
    l: Float64Array.from(closes),
    c: Float64Array.from(closes),
    v: new Float64Array(ts.length),
    n: ts.length,
  };
}

describe("dayChange", () => {
  it("measures against the previous session's last RTH bar, skipping AH prints", () => {
    const b = bars(
      [et(2, 15, 58), et(2, 15, 59), et(2, 16, 30), et(2, 19, 59), et(3, 9, 30), et(3, 9, 31)],
      [100, 101, 102, 103, 104, 105],
    );
    const now = et(3, 9, 32);
    expect(prevCloseIndex(b, now)).toBe(1); // 15:59 on the 2nd, not the 19:59 AH print
    const dc = dayChange(b, 106, now)!;
    expect(dc.prevClose).toBe(101);
    expect(dc.change).toBe(5);
    expect(dc.pct).toBeCloseTo(5 / 101, 8);
  });

  it("premarket on an RTH-only tape: the latest bar is the reference close", () => {
    const b = bars([et(2, 15, 58), et(2, 15, 59)], [100, 101]);
    const dc = dayChange(b, 99, et(3, 8, 0))!;
    expect(dc.prevClose).toBe(101);
    expect(dc.change).toBe(-2);
  });

  it("weekend tape (last bar Friday) references Friday's close", () => {
    const b = bars([et(4, 15, 59)], [200]); // Fri Sep 4 2026
    expect(prevCloseIndex(b, et(6, 12, 0))).toBe(0);
  });

  it("returns null when the tape holds only today's bars or no price", () => {
    const b = bars([et(3, 9, 30), et(3, 9, 31)], [104, 105]);
    expect(dayChange(b, 106, et(3, 9, 32))).toBeNull();
    expect(dayChange(bars([], []), 106)).toBeNull();
    expect(dayChange(b, 0, et(3, 9, 32))).toBeNull();
  });
});
