import { describe, expect, it } from "vitest";
import { allocationSegments, fmtUsd, UNALLOCATED_COLOR } from "./fund";

const inst = (id: string, allocated: number) => ({ id, name: id, allocated });

describe("allocationSegments", () => {
  it("splits equity into strategy segments plus the unallocated remainder", () => {
    const segs = allocationSegments(
      [inst("a", 10_000), inst("b", 10_000)],
      100_000,
    );
    expect(segs).toHaveLength(3);
    expect(segs[0].pct).toBeCloseTo(10);
    expect(segs[2].id).toBe("__unallocated__");
    expect(segs[2].value).toBeCloseTo(80_000);
    expect(segs[2].color).toBe(UNALLOCATED_COLOR);
    expect(segs.reduce((s, x) => s + x.pct, 0)).toBeCloseTo(100);
  });

  it("squeezes proportionally when allocations exceed equity, no overflow", () => {
    const segs = allocationSegments(
      [inst("a", 80_000), inst("b", 40_000)],
      100_000,
    );
    expect(segs).toHaveLength(2); // no unallocated segment when over-allocated
    expect(segs.reduce((s, x) => s + x.pct, 0)).toBeCloseTo(100);
    expect(segs[0].pct).toBeCloseTo((80_000 / 120_000) * 100);
  });

  it("drops zero allocations and survives zero equity", () => {
    expect(allocationSegments([inst("a", 0)], 0)).toHaveLength(0);
    const segs = allocationSegments([inst("a", 0), inst("b", 5_000)], 10_000);
    expect(segs.map((s) => s.label)).toEqual(["b", "unallocated"]);
  });
});

describe("fmtUsd", () => {
  it("formats signed dollars and tolerates null", () => {
    expect(fmtUsd(12_345.6)).toBe("$12,346");
    expect(fmtUsd(-2_000)).toBe("-$2,000");
    expect(fmtUsd(null)).toBe("—");
  });
});
