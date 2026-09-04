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
  /** Manual price scale; null = auto-fit to visible bars (+ overlay levels). */
  yDomain: [number, number] | null;
};

export type Layout = {
  width: number;
  height: number;
  plotW: number; // width minus price axis
  plotH: number; // height minus time axis
  /** Bottom of the PRICE pane (price->y maps into [0, volTop]). Historical
   * name: with no oscillator panes it is also where the volume strip starts. */
  volTop: number;
  /** Oscillator panes (RSI, MACD) stack between the price pane and volume. */
  oscTop: number;
  oscH: number; // height of ONE oscillator pane
  oscCount: number;
  /** Where the volume strip starts. */
  volStart: number;
  axisW: number;
  axisH: number;
};

export const AXIS_W = 64;
export const AXIS_H = 22;
export const VOL_FRAC = 0.14;
export const OSC_FRAC = 0.16;

export function computeLayout(width: number, height: number, oscCount = 0): Layout {
  const plotW = Math.max(50, width - AXIS_W);
  const plotH = Math.max(50, height - AXIS_H);
  const volStart = plotH * (1 - VOL_FRAC);
  const oscH = oscCount ? plotH * OSC_FRAC : 0;
  const volTop = volStart - oscH * oscCount;
  return {
    width,
    height,
    plotW,
    plotH,
    volTop,
    oscTop: volTop,
    oscH,
    oscCount,
    volStart,
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

export const MIN_BARS_VISIBLE = 20;
export const MAX_BARS_VISIBLE = 3000;

/** Horizontal zoom in place: scale the visible bar count by `factor`
 * (>1 zooms out) keeping the bar under plot-x `x` fixed. Shared by the
 * wheel, the phone's +/− buttons and the pinch gesture, so all three agree
 * on limits and anchoring. */
export function zoomX(view: ViewState, layout: Layout, x: number, factor: number): void {
  const anchor = xToIndex(x, view, layout);
  const next = Math.max(MIN_BARS_VISIBLE, Math.min(MAX_BARS_VISIBLE, view.barsVisible * factor));
  const frac = (view.rightIndex - anchor) / view.barsVisible;
  view.barsVisible = next;
  view.rightIndex = anchor + frac * next;
  view.follow = false;
}

/** Vertical zoom in place: stretch the price scale by `factor` (>1 widens)
 * keeping the price under plot-y `y` fixed. Makes the scale manual. */
export function zoomY(
  view: ViewState,
  domain: [number, number],
  layout: Layout,
  y: number,
  factor: number,
): void {
  const anchor = yToPrice(y, domain, layout);
  const lo = anchor - (anchor - domain[0]) * factor;
  const hi = anchor + (domain[1] - anchor) * factor;
  if (hi - lo > 1e-9) view.yDomain = [lo, hi];
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

/** Extend a price domain to include extra levels (strikes, TP/SL, breakevens),
 * but only up to `maxGrow` × the candle span on either side. Beyond that the
 * candles would flatten into a line to make room for a far-off level — the
 * level shows as an axis-edge arrow instead (TradingView's auto-scale fits the
 * bars; drawings do not stretch it). At the default the bars always keep at
 * least half of the pane. */
export function extendDomain(
  domain: [number, number],
  levels: number[],
  maxGrow = 0.5,
): [number, number] {
  let [lo, hi] = domain;
  const span = domain[1] - domain[0] || 1;
  const floor = domain[0] - span * maxGrow;
  const ceil = domain[1] + span * maxGrow;
  for (const level of levels) {
    if (!isFinite(level)) continue;
    if (level < lo) lo = Math.max(level, floor);
    if (level > hi) hi = Math.min(level, ceil);
  }
  const pad = (hi - lo) * 0.03;
  return [lo === domain[0] ? lo : Math.max(lo - pad, floor), hi === domain[1] ? hi : Math.min(hi + pad, ceil)];
}

const ET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const ET_TIME_SEC = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
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

/** HH:MM:SS — the sub-minute chart's axis, where two labels share a minute. */
export function fmtTimeSecET(ms: number): string {
  return ET_TIME_SEC.format(new Date(ms));
}

export function fmtDayET(ms: number): string {
  return ET_DAY.format(new Date(ms));
}

export function fmtPrice(p: number): string {
  return p >= 1000 ? p.toFixed(1) : p.toFixed(2);
}
