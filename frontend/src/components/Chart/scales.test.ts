import { describe, expect, it } from "vitest";
import {
  computeLayout,
  extendDomain,
  MAX_BARS_VISIBLE,
  MIN_BARS_VISIBLE,
  xToIndex,
  yToPrice,
  zoomX,
  zoomY,
  type ViewState,
} from "./scales";

const view = (): ViewState => ({ rightIndex: 100, barsVisible: 50, follow: true, yDomain: null });

describe("zoomX", () => {
  it("keeps the bar under the finger fixed and drops follow", () => {
    const layout = computeLayout(400, 300);
    const v = view();
    const x = layout.plotW * 0.4;
    const before = xToIndex(x, v, layout);
    zoomX(v, layout, x, 2);
    expect(v.barsVisible).toBe(100);
    expect(xToIndex(x, v, layout)).toBeCloseTo(before, 9);
    expect(v.follow).toBe(false);
  });

  it("clamps to the visible-bar limits", () => {
    const layout = computeLayout(400, 300);
    const v = view();
    zoomX(v, layout, 10, 0.0001);
    expect(v.barsVisible).toBe(MIN_BARS_VISIBLE);
    zoomX(v, layout, 10, 1e9);
    expect(v.barsVisible).toBe(MAX_BARS_VISIBLE);
  });
});

describe("zoomY", () => {
  it("stretches the price scale around the price under the finger", () => {
    const layout = computeLayout(400, 300);
    const v = view();
    const domain: [number, number] = [100, 110];
    const y = layout.volTop * 0.25;
    const anchor = yToPrice(y, domain, layout);
    zoomY(v, domain, layout, y, 2);
    expect(v.yDomain).not.toBeNull();
    const [lo, hi] = v.yDomain!;
    expect(hi - lo).toBeCloseTo(20, 9);
    expect(yToPrice(y, v.yDomain!, layout)).toBeCloseTo(anchor, 9);
  });
});

describe("extendDomain", () => {
  it("includes nearby levels with a small pad", () => {
    const [lo, hi] = extendDomain([100, 110], [98, 112]);
    expect(lo).toBeLessThan(98);
    expect(hi).toBeGreaterThan(112);
    expect(lo).toBeGreaterThan(94);
  });

  it("caps growth so the candles keep at least half the pane", () => {
    const [lo, hi] = extendDomain([100, 110], [50, 200]);
    expect(lo).toBe(95);
    expect(hi).toBe(115);
  });

  it("leaves the domain alone when every level is inside it", () => {
    expect(extendDomain([100, 110], [102, 108])).toEqual([100, 110]);
  });
});
