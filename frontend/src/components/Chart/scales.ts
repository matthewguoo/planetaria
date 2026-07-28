/**
 * Chart coordinate system.
 *
 * X is *bar-index space*, not wall-clock time: intraday sessions have
 * overnight gaps that would waste 70% of a linear time axis. Index space
 * also extends smoothly past the last bar (fractional indices >= n) — that
 * future region is trading-time by construction, which is exactly the time
 * basis the options math uses (252 x 6.5h years). The P/L heatmap and the
 * payoff overlays plug straight into these scales.
 */

export type Bars = {
  t: Float64Array;
  o: Float64Array;
  h: Float64Array;
  l: Float64Array;
  c: Float64Array;
  v: Float64Array;
  n: number;
};

export type ViewState = {
  /** Fractional index of the bar at the right edge of the plot area. */
  rightIndex: number;
  /** Number of bar-widths visible across the plot area. */
  barsVisible: number;
  /** Auto-scroll to keep the newest bar in view as data arrives. */
  follow: boolean;
};

export type Layout = {
  width: number;
  height: number;
  plotW: number; // width minus price axis
  plotH: number; // height minus time axis
  volTop: number; // y where volume subpane starts
  axisW: number;
  axisH: number;
};

export const AXIS_W = 64;
export const AXIS_H = 22;
export const VOL_FRAC = 0.14;

export function computeLayout(width: number, height: number): Layout {
  const plotW = Math.max(50, width - AXIS_W);
  const plotH = Math.max(50, height - AXIS_H);
  return {
    width,
    height,
    plotW,
    plotH,
    volTop: plotH * (1 - VOL_FRAC),
    axisW: AXIS_W,
    axisH: AXIS_H,
  };
}

export function indexToX(index: number, view: ViewState, layout: Layout): number {
  const barW = layout.plotW / view.barsVisible;
  return layout.plotW - (view.rightIndex - index) * barW;
}

export function xToIndex(x: number, view: ViewState, layout: Layout): number {
  const barW = layout.plotW / view.barsVisible;
  return view.rightIndex - (layout.plotW - x) / barW;
}

export function priceToY(price: number, domain: [number, number], layout: Layout): number {
  const [lo, hi] = domain;
  return ((hi - price) / (hi - lo || 1)) * layout.volTop;
}

export function yToPrice(y: number, domain: [number, number], layout: Layout): number {
  const [lo, hi] = domain;
  return hi - (y / layout.volTop) * (hi - lo);
}

/** Visible [firstIndex, lastIndex] clamped to data, given the view. */
export function visibleRange(bars: Bars, view: ViewState): [number, number] {
  const last = Math.min(bars.n - 1, Math.ceil(view.rightIndex));
  const first = Math.max(0, Math.floor(view.rightIndex - view.barsVisible));
  return [first, last];
}

/** Price domain (with pad) across the visible candles. */
export function priceDomain(bars: Bars, view: ViewState): [number, number] {
  const [first, last] = visibleRange(bars, view);
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = first; i <= last; i++) {
    if (bars.l[i] < lo) lo = bars.l[i];
    if (bars.h[i] > hi) hi = bars.h[i];
  }
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  const pad = (hi - lo || hi * 0.01 || 1) * 0.07;
  return [lo - pad, hi + pad];
}

/** Extend a price domain to include extra levels (strikes, breakevens). */
export function extendDomain(domain: [number, number], levels: number[]): [number, number] {
  let [lo, hi] = domain;
  for (const level of levels) {
    if (!isFinite(level)) continue;
    if (level < lo) lo = level;
    if (level > hi) hi = level;
  }
  const pad = (hi - lo) * 0.03;
  return [lo === domain[0] ? lo : lo - pad, hi === domain[1] ? hi : hi + pad];
}

const ET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const ET_DAY = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "2-digit",
});

export function fmtTimeET(ms: number): string {
  return ET_TIME.format(new Date(ms));
}

export function fmtDayET(ms: number): string {
  return ET_DAY.format(new Date(ms));
}

export function fmtPrice(p: number): string {
  return p >= 1000 ? p.toFixed(1) : p.toFixed(2);
}
