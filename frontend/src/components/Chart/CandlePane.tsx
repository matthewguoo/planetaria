import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { barsTable } from "../../lib/perspective";
import { focusFeed, onSnapshot } from "../../lib/barFeed";
import type { HeatmapResult } from "../../lib/heatmap.worker";
import {
  breakevensForBasis,
  makeScenarioModel,
  normPdf,
  positionEntryCost,
  positionIv,
  positionPlModel,
  positionValueModel,
  TRADING_HOURS_PER_YEAR,
  type Leg,
  type ScenarioModel,
  type Smiles,
} from "../../lib/optionsMath";
import {
  computeAtr,
  computeBollinger,
  computeEma,
  computeVwap,
  realizedVolAnnualized,
} from "../../lib/indicators";
import { buildPositionView } from "../../lib/positionView";
import type { Designer } from "../../lib/useDesigner";
import { ChartHud } from "./ChartHud";
import { publishScale, sharedBars } from "../../lib/chartShared";
import { useHeatmap } from "../../lib/useHeatmap";
import { useAccountStore } from "../../store/accountStore";
import { TF_MS, freshSpot, quoteIsStale, useTradingStore, type IndicatorToggles } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";
import {
  availableStrikes,
  buildLegs,
  hoursToExpiry as calcHoursToExpiry,
  smileFromChain,
  useStrategyStore,
} from "../../store/strategyStore";
import {
  computeLayout,
  extendDomain,
  fmtDayET,
  fmtPrice,
  fmtTimeET,
  indexToX,
  priceDomain,
  priceToY,
  visibleRange,
  xToIndex,
  yToPrice,
  type Bars,
  type Layout,
  type ViewState,
} from "./scales";

const COLORS = {
  bg: "#000000",
  grid: "#1a1a1a",
  axisText: "#666666",
  up: "#00C853",
  down: "#FF1744",
  last: "#FFB000",
  crosshair: "#444444",
  volUp: "rgba(0,200,83,0.35)",
  volDown: "rgba(255,23,68,0.35)",
  strike: "#FFB000",
  strikeShort: "#FFA028",
  breakeven: "#FFFFFF",
  tp: "#00C853",
  sl: "#FF1744",
  expiry: "#2196F3",
  timeStop: "#FF6D00",
};

const EMPTY: Bars = {
  t: new Float64Array(0),
  o: new Float64Array(0),
  h: new Float64Array(0),
  l: new Float64Array(0),
  c: new Float64Array(0),
  v: new Float64Array(0),
  n: 0,
};

const RIGHT_PAD_FRAC = 0.08;
const STRIKE_HIT_PX = 6;
/** Inset of the strike-handle rail from the plot's right edge. */
const RAIL_INSET = 16;
const RAIL_HIT_PX = 9;
// Strike chips start right of the LegRail strip (44px, desktop) so the two
// leg affordances never overlap; on mobile the inset is just breathing room.
const CHIP_X = 48;
const CHIP_H = 18;
const CHIP_ZONE = 14; // width of the − / + click zones on a strike chip

type StrategyOverlay = {
  legs: (Leg & { symbol: string })[] | null;
  strikes: number[];
  strikeSides: number[]; // +1 long leg, -1 short leg (for coloring)
  strikeRights: ("C" | "P")[];
  ratios: number[];
  snapStrikes: number[];
  hoursToExpiry: number;
  timeStopHours: number;
  tpPremium: number | null;
  slPremium: number | null;
  tpPct: number;
  slPct: number;
  entry: number;
  sigma: number;
  spot: number;
  smiles: Smiles | null;
  volShift: number;
  skewBeta: boolean;
  /** Precomputed scenario model — the ONE pricing function shared by the
   * surface, contours, tooltip, and exit-drag inverse mapping. */
  model: ScenarioModel;
  /** Position view: overlay is a live plan, not the designer — no editing. */
  readOnly: boolean;
  /** Position view: surface time anchor (entry timestamp, ms); null = now. */
  anchorMs: number | null;
  /** Position view: P/L basis (actual fill premium); null = leg net entry. */
  entryBasis: number | null;
  /** Model-free expiry breakevens against the active premium basis. */
  breakevens: number[];
  /** Closed-trade view: exit event (trading hours after entry), the actual
   * exit premium/reason and realized P/L — drives the EXIT marker. */
  exitHours: number | null;
  exitPremium: number | null;
  exitReason: string | null;
  realizedPnl: number | null;
  /** Chunked closing waves — one marker per wave when the close landed in
   * pieces (broker auto-liquidation). Empty = single exit marker only. */
  exitEvents: { hours: number; premium: number; qty: number }[];
};

/** Bar index the surface's t=0 column maps to: entry bar for a position
 * view (clamped into the loaded range), else the latest bar. */
function anchorIndexFor(bars: Bars, overlay: StrategyOverlay | null): number {
  const nowIdx = Math.max(bars.n - 1, 0);
  if (!overlay || overlay.anchorMs === null || !bars.n) return nowIdx;
  const target = overlay.anchorMs;
  if (target <= bars.t[0]) return 0;
  if (target >= bars.t[nowIdx]) return nowIdx;
  let lo = 0;
  let hi = nowIdx;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars.t[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

type DragTarget =
  | { kind: "strike"; i: number }
  | { kind: "tp" }
  | { kind: "sl" }
  | { kind: "timestop" };

function currentEtMinutes(): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  return Number(parts.hour === "24" ? 0 : parts.hour) * 60 + Number(parts.minute);
}

type ChipRect = { i: number; x: number; y: number; w: number; label: string };

function chipLabel(overlay: StrategyOverlay, i: number): string {
  const side = overlay.strikeSides[i] > 0 ? "+" : "−";
  const ratio = overlay.ratios[i] ?? 1;
  return `${side}${ratio} ${overlay.strikeRights[i] ?? ""}${fmtPrice(overlay.strikes[i])}`;
}

/** Chip rectangles for every visible strike, staggered so same-price legs
 * (straddles) stay individually clickable. Shared by render + hit tests. */
function computeChipRects(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
): ChipRect[] {
  const rects: ChipRect[] = [];
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const label = chipLabel(overlay, i);
    const w = ctx.measureText(label).width + CHIP_ZONE * 2 + 8;
    let x = CHIP_X;
    for (const r of rects) {
      if (Math.abs(r.y - y) < CHIP_H && x < r.x + r.w + 4) x = r.x + r.w + 4;
    }
    rects.push({ i, x, y, w, label });
  });
  return rects;
}

export function CandlePane({
  designer,
  hudVariant,
}: {
  designer: Designer;
  /** "none": the host renders ChartHud itself (desktop left sidebar). */
  hudVariant?: "full" | "chips" | "none";
}) {
  const symbol = useTradingStore((s) => s.symbol);
  const tf = useTradingStore((s) => s.tf);
  const quote = useTradingStore((s) => s.quote);

  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const kind = useStrategyStore((s) => s.kind);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const legRights = useStrategyStore((s) => s.rights);
  const legSides = useStrategyStore((s) => s.sides);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const volShift = useStrategyStore((s) => s.volShift);
  const skewBeta = useStrategyStore((s) => s.skewBeta);
  const indicatorToggles = useTradingStore((s) => s.indicators);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);
  const decRatio = useStrategyStore((s) => s.decRatio);
  const setTpPct = useStrategyStore((s) => s.setTpPct);
  const setSlPct = useStrategyStore((s) => s.setSlPct);
  const setTimeStopEt = useStrategyStore((s) => s.setTimeStopEt);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const barsRef = useRef<Bars>(EMPTY);
  const viewRef = useRef<ViewState>({
    rightIndex: 0,
    barsVisible: 120,
    follow: true,
    yDomain: null,
  });
  const mouseRef = useRef<{ x: number; y: number } | null>(null);
  const dragRef = useRef<{
    startX: number;
    startY: number;
    startRight: number;
    startDomain: [number, number];
    vActive: boolean;
  } | null>(null);
  const axisDragRef = useRef<{ startY: number; domain: [number, number] } | null>(null);
  const dragTargetRef = useRef<DragTarget | null>(null);
  const prevSymbolRef = useRef(symbol);
  const framedPlanRef = useRef<string | null>(null);
  const surfaceRef = useRef<HeatmapResult | null>(null);
  const overlayRef = useRef<StrategyOverlay | null>(null);
  // Visible y-domain, quantized to 25% steps of its own span: pans/zooms
  // only trigger a surface recompute when the view moves meaningfully.
  const [viewWindow, setViewWindow] = useState<[number, number] | null>(null);
  const viewWindowRef = useRef<[number, number] | null>(null);
  const indicatorsRef = useRef<IndicatorToggles>(indicatorToggles);
  indicatorsRef.current = indicatorToggles;
  // Tick-fresh display: the newest FRESH quote extends the forming bar so the
  // chart moves with every tick instead of once per bar close. Display-only —
  // stored bar data stays exactly what the feed delivered. In RTH-only mode
  // an extended-hours tick must NOT extend the last RTH candle (it would
  // falsify the session close), so the extension is gated to RTH minutes.
  const showEth = useTradingStore((s) => s.showEth);
  const showEthRef = useRef(showEth);
  showEthRef.current = showEth;
  const liveTickRef = useRef<{ mid: number; ts: number } | null>(null);
  liveTickRef.current = (() => {
    if (!quote || quote.mid <= 0 || quoteIsStale(quote)) return null;
    if (!showEth) {
      const minuteOfDay =
        ((quote.ts / 60_000 + etOffsetMinutes(quote.ts)) % 1440 + 1440) % 1440;
      if (minuteOfDay < RTH_START_MIN || minuteOfDay >= RTH_END_MIN) return null;
    }
    return { mid: quote.mid, ts: quote.ts };
  })();
  const rafRef = useRef(0);

  // ------------------------------------------------- strategy derivations

  const viewingPlanId = useUiStore((s) => s.viewingPlanId);
  const viewedHistorical = useUiStore((s) => s.viewedHistorical);
  const pnlMode = useUiStore((s) => s.pnlMode);
  const positions = useAccountStore((s) => s.positions);
  const viewingPlan = viewingPlanId
    ? positions.find((p) => p.id === viewingPlanId) ??
      (viewedHistorical?.id === viewingPlanId ? viewedHistorical : null)
    : null;

  const overlay: StrategyOverlay | null = useMemo(() => {
    // POSITION VIEW: chart inspects a live plan (legs + rules), read-only,
    // anchored at entry, P/L measured against the actual fill premium.
    if (viewingPlan) {
      const positionView = buildPositionView(viewingPlan, chain, pnlMode);
      if (positionView) {
        const spot = freshSpot(quote, chain?.spot ?? 0);
        const live = pnlMode === "live";
        const entry = positionView.entryBasis;
        const shift = live ? volShift : 0;
        const beta = live ? skewBeta : false;
        return {
          legs: positionView.legs,
          strikes: positionView.strikes,
          strikeSides: positionView.strikeSides,
          strikeRights: positionView.strikeRights,
          ratios: positionView.ratios,
          snapStrikes: [],
          hoursToExpiry: positionView.hoursTotal,
          timeStopHours: positionView.timeStopHours,
          tpPremium: positionView.tpPremium,
          slPremium: positionView.slPremium,
          tpPct:
            Math.abs(entry) >= 0.01 ? (positionView.tpPremium - entry) / Math.abs(entry) : 0,
          slPct:
            Math.abs(entry) >= 0.01 ? (entry - positionView.slPremium) / Math.abs(entry) : 0,
          entry,
          sigma: positionIv(positionView.legs),
          spot,
          smiles: positionView.smiles,
          volShift: shift,
          skewBeta: beta,
          model: makeScenarioModel(positionView.smiles, spot, shift, beta),
          readOnly: true,
          anchorMs: positionView.anchorMs,
          entryBasis: entry,
          breakevens:
            spot > 0
              ? breakevensForBasis(positionView.legs, entry, spot * 0.7, spot * 1.3)
              : [],
          exitHours: positionView.exitHours,
          exitPremium: positionView.exitPremium,
          exitReason: positionView.exitReason,
          realizedPnl: positionView.realizedPnl,
          exitEvents: positionView.exitEvents,
        };
      }
    }
    const legs = buildLegs({
      chain, expiry, kind, strikes, ratios, rights: legRights, sides: legSides,
    });
    if (!chain || !expiry) return null;
    const spot = freshSpot(quote, chain.spot);
    const hte = calcHoursToExpiry(expiry);
    const entry = legs ? positionEntryCost(legs) : 0;
    // Time stop: today at HH:MM ET, in trading hours from now.
    const nowEt = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(new Date());
    const parts = Object.fromEntries(nowEt.map((p) => [p.type, p.value]));
    const nowMin = Number(parts.hour === "24" ? 0 : parts.hour) * 60 + Number(parts.minute);
    const [tsH, tsM] = timeStopEt.split(":").map(Number);
    const timeStopHours = Math.max(0, Math.min((tsH * 60 + tsM - nowMin) / 60, 6.5));
    const priced = legs !== null && Math.abs(entry) >= 0.01;
    return {
      legs,
      strikes,
      strikeSides: legSides,
      strikeRights: legRights,
      ratios: strikes.map((_, i) => ratios[i] ?? 1),
      snapStrikes: availableStrikes(chain, expiry),
      hoursToExpiry: hte,
      timeStopHours,
      // Signed premium axis: TP above entry, SL below — credit-safe.
      tpPremium: priced ? entry + Math.abs(entry) * tpPct : null,
      slPremium: priced ? entry - Math.abs(entry) * slPct : null,
      tpPct,
      slPct,
      entry,
      sigma: legs ? positionIv(legs) : 0,
      spot,
      smiles: smileFromChain(chain, expiry),
      volShift,
      skewBeta,
      model: makeScenarioModel(smileFromChain(chain, expiry), spot, volShift, skewBeta),
      readOnly: false,
      anchorMs: null,
      entryBasis: null,
      breakevens:
        legs && spot > 0 ? breakevensForBasis(legs, entry, spot * 0.7, spot * 1.3) : [],
      exitHours: null,
      exitPremium: null,
      exitReason: null,
      realizedPnl: null,
      exitEvents: [],
    };
  }, [chain, expiry, kind, strikes, ratios, legRights, legSides, tpPct, slPct, timeStopEt, quote, volShift, skewBeta, viewingPlan, pnlMode]);

  overlayRef.current = overlay;

  const draw = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (!canvas || !wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = wrap.clientWidth;
      const cssH = wrap.clientHeight;
      if (!cssW || !cssH) return;
      if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;
        canvas.style.width = `${cssW}px`;
        canvas.style.height = `${cssH}px`;
      }
      const ctx = canvas.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const layout = computeLayout(cssW, cssH);
      render(
        ctx,
        layout,
        barsRef.current,
        viewRef.current,
        mouseRef.current,
        overlayRef.current,
        surfaceRef.current,
        TF_MS[tf] / 60000,
        dragTargetRef.current,
        indicatorsRef.current,
        liveTickRef.current,
        showEthRef.current,
      );
      // Feed HTML layers outside the canvas (sidebar HUD, leg rail).
      sharedBars.current = barsRef.current;
      const [lo, hi] = currentDomain(
        barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current,
      );
      publishScale({ lo, hi, volTopPx: layout.volTop });
      // Quantized view window for the heatmap grid (see useHeatmap).
      const step = Math.max((hi - lo) * 0.25, 1e-6);
      const qLo = Math.floor(lo / step) * step;
      const qHi = Math.ceil(hi / step) * step;
      const prev = viewWindowRef.current;
      if (!prev || Math.abs(prev[0] - qLo) > step * 0.5 || Math.abs(prev[1] - qHi) > step * 0.5) {
        viewWindowRef.current = [qLo, qHi];
        setViewWindow([qLo, qHi]);
      }
    });
  }, [tf]);

  // Surface recompute (worker). The grid follows the QUANTIZED visible
  // window (dense vertical sampling at any zoom — see useHeatmap); pans
  // inside the same quantized window still remap without recompute.
  const surfaceInputs = useMemo(() => {
    if (!overlay || !overlay.legs) return null;
    const risk =
      overlay.slPremium !== null ? (overlay.entry - overlay.slPremium) * 100 : 100;
    return {
      legs: overlay.legs,
      hoursToExpiry: overlay.hoursToExpiry,
      spot: overlay.spot,
      tpPremium: overlay.tpPremium,
      slPremium: overlay.slPremium,
      riskDollars: Math.max(risk, 1),
      smiles: overlay.smiles,
      volShift: overlay.volShift,
      skewBeta: overlay.skewBeta,
      entryOverride: overlay.entryBasis,
      viewLo: viewWindow ? viewWindow[0] : null,
      viewHi: viewWindow ? viewWindow[1] : null,
    };
  }, [overlay, viewWindow]);

  useHeatmap(surfaceInputs, (result) => {
    surfaceRef.current = result;
    draw();
  });

  useEffect(() => {
    if (!overlay?.legs) surfaceRef.current = null;
    draw();
  }, [overlay, draw, indicatorToggles]);

  // Every fresh quote repaints the forming bar (tick-level chart freshness).
  useEffect(() => {
    draw();
  }, [quote, draw]);

  // ------------------------------------------------------- data plumbing

  useEffect(() => {
    let disposed = false;
    let view: {
      delete(): Promise<void>;
      on_update(cb: () => void): void;
      to_columns(): Promise<Record<string, unknown[]>>;
    } | null = null;

    focusFeed(symbol, tf);

    // Ticker switch: the old symbol's manual y-scale and pan position are
    // meaningless on a different price level — snap back to auto-fit/follow
    // so the new symbol lands in view instead of off-screen.
    if (prevSymbolRef.current !== symbol) {
      prevSymbolRef.current = symbol;
      viewRef.current.yDomain = null;
      viewRef.current.follow = true;
    }

    async function pull() {
      if (!view || disposed) return;
      const cols = await view.to_columns();
      if (disposed) return;
      const t = (cols.t as number[] | undefined) ?? [];
      let idx = t.map((_, i) => i);
      // RTH-only default: extended-hours bars are dropped entirely, so days
      // sit contiguous on the index axis and future-time mapping is exactly
      // trading-time. The ETH toggle shows the full tape.
      if (!showEth && t.length) {
        const etOff = etOffsetMinutes(t[t.length - 1]);
        idx = idx.filter((i) => {
          const minuteOfDay = (((t[i] / 60_000 + etOff) % 1440) + 1440) % 1440;
          return minuteOfDay >= RTH_START_MIN && minuteOfDay < RTH_END_MIN;
        });
      }
      const n = idx.length;
      const pick = (col: unknown[]) => Float64Array.from(idx, (i) => col[i] as number);
      barsRef.current = n
        ? {
            t: pick(cols.t as number[]),
            o: pick(cols.o as number[]),
            h: pick(cols.h as number[]),
            l: pick(cols.l as number[]),
            c: pick(cols.c as number[]),
            v: pick(cols.v as number[]),
            n,
          }
        : EMPTY;
      const v = viewRef.current;
      // Historical replay: frame the holding window (entry -> exit, padded)
      // once per viewed plan as soon as bars cover it — a 1-minute scalp from
      // this morning must open ON SCREEN, not off the left edge.
      const hist = useUiStore.getState().viewedHistorical;
      if (hist && n && framedPlanRef.current !== hist.id) {
        const bars = barsRef.current;
        const anchorMs = Date.parse(hist.entered_at ?? hist.created_at);
        const exitMs = hist.exited_at ? Date.parse(hist.exited_at) : anchorMs;
        if (Number.isFinite(anchorMs) && anchorMs <= bars.t[n - 1]) {
          let aIdx = 0;
          let xIdx = n - 1;
          for (let i = 0; i < n; i++) {
            if (bars.t[i] <= anchorMs) aIdx = i;
            if (bars.t[i] <= exitMs) xIdx = i;
          }
          const span = Math.max(xIdx - aIdx, 1);
          v.barsVisible = Math.min(Math.max(span * 4, 60), 3000);
          v.rightIndex = xIdx + v.barsVisible * 0.3;
          v.follow = false;
          v.yDomain = null;
          framedPlanRef.current = hist.id;
        }
      } else if (!hist) {
        framedPlanRef.current = null;
      }
      if (v.follow) {
        v.rightIndex = Math.max(0, n - 1) + v.barsVisible * RIGHT_PAD_FRAC;
      }
      draw();
    }

    (async () => {
      const table = await barsTable(symbol, tf);
      if (disposed) return;
      view = (await table.view({
        columns: ["t", "o", "h", "l", "c", "v"],
        sort: [["t", "asc"]],
      })) as unknown as typeof view;
      view!.on_update(() => void pull());
      await pull();
    })();

    const offSnapshot = onSnapshot(() => void pull());
    return () => {
      disposed = true;
      offSnapshot();
      void view?.delete();
      barsRef.current = EMPTY;
    };
  }, [symbol, tf, draw, showEth]);

  useEffect(() => {
    const observer = new ResizeObserver(draw);
    if (wrapRef.current) observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [draw]);

  // ---------------------------------------------------------- interactions

  const hitTestStrike = useCallback((y: number): number | null => {
    const overlayNow = overlayRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current);
    let best: number | null = null;
    let bestDist = STRIKE_HIT_PX + 1;
    overlayNow.strikes.forEach((strike, i) => {
      const dist = Math.abs(priceToY(strike, domain, layout) - y);
      if (dist < bestDist) {
        best = i;
        bestDist = dist;
      }
    });
    return best;
  }, []);

  /** Drag handles where each strike line crosses the vertical rail. */
  const hitTestRail = useCallback((x: number, y: number): number | null => {
    const overlayNow = overlayRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    if (Math.abs(x - (layout.plotW - RAIL_INSET)) > RAIL_HIT_PX) return null;
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current);
    let best: number | null = null;
    let bestDist = RAIL_HIT_PX + 1;
    overlayNow.strikes.forEach((strike, i) => {
      const dist = Math.abs(priceToY(strike, domain, layout) - y);
      if (dist < bestDist) {
        best = i;
        bestDist = dist;
      }
    });
    return best;
  }, []);

  /** Exit boundaries: TP/SL premium contours and the time-stop vertical. */
  const hitTestExit = useCallback((x: number, y: number): "tp" | "sl" | "timestop" | null => {
    const overlayNow = overlayRef.current;
    const surface = surfaceRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow?.legs || overlayNow.readOnly || !surface || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current);
    const view = viewRef.current;
    const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
    const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
    if (y > layout.volTop) return null;
    const xTs = indexToX(futureIndex(overlayNow.timeStopHours, anchorIdx, tfMinutes), view, layout);
    if (Math.abs(x - xTs) <= 6) return "timestop";
    const x0 = indexToX(anchorIdx, view, layout);
    const xExp = indexToX(futureIndex(surface.hoursToExpiry, anchorIdx, tfMinutes), view, layout);
    if (x < x0 || x > xExp || xExp <= x0) return null;
    const ti = Math.max(
      0,
      Math.min(Math.round(((x - x0) / (xExp - x0)) * (surface.timeSteps - 1)), surface.timeSteps - 1),
    );
    const near = (line: Float64Array): boolean => {
      const s = line[ti];
      return isFinite(s) && Math.abs(priceToY(s, domain, layout) - y) <= 6;
    };
    if (near(surface.tpLine)) return "tp";
    if (near(surface.slLine)) return "sl";
    return null;
  }, []);

  /** Strike chip zones: − / + edit the leg's contract ratio, middle drags. */
  const hitTestChip = useCallback(
    (x: number, y: number): { i: number; zone: "minus" | "plus" | "drag" } | null => {
      const overlayNow = overlayRef.current;
      const wrap = wrapRef.current;
      const canvas = canvasRef.current;
      if (!overlayNow || overlayNow.readOnly || !overlayNow.strikes.length || !wrap || !canvas) return null;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current);
      const ctx = canvas.getContext("2d")!;
      ctx.font = "11px 'SF Mono', Consolas, monospace";
      for (const rect of computeChipRects(ctx, layout, domain, overlayNow)) {
        if (x < rect.x || x > rect.x + rect.w || Math.abs(y - rect.y) > CHIP_H / 2) continue;
        if (x <= rect.x + CHIP_ZONE) return { i: rect.i, zone: "minus" };
        if (x >= rect.x + rect.w - CHIP_ZONE) return { i: rect.i, zone: "plus" };
        return { i: rect.i, zone: "drag" };
      }
      return null;
    },
    [],
  );

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      const view = viewRef.current;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      if (x > layout.plotW && y <= layout.plotH) {
        // Wheel over the price axis: vertical scale around the cursor price.
        const domain = currentDomain(barsRef.current, view, overlayRef.current, surfaceRef.current);
        const anchor = yToPrice(y, domain, layout);
        view.yDomain = [
          anchor - (anchor - domain[0]) * factor,
          anchor + (domain[1] - anchor) * factor,
        ];
        draw();
        return;
      }
      const anchor = xToIndex(x, view, layout);
      const next = Math.max(20, Math.min(3000, view.barsVisible * factor));
      const frac = (view.rightIndex - anchor) / view.barsVisible;
      view.barsVisible = next;
      view.rightIndex = anchor + frac * next;
      view.follow = false;
      draw();
    },
    [draw],
  );

  const onMouseDown = useCallback(
    (e: React.PointerEvent) => {
      // Capture so touch drags keep streaming to the canvas even when the
      // finger wanders off it (also helps fast mouse drags).
      try {
        (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      } catch {
        /* unsupported pointer id (e.g. synthetic events) */
      }
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      if (x > layout.plotW && y <= layout.plotH) {
        // Grab the price axis: vertical scale drag.
        axisDragRef.current = {
          startY: y,
          domain: currentDomain(barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current),
        };
        return;
      }
      const chip = hitTestChip(x, y);
      if (chip) {
        const ratios = overlayRef.current?.ratios ?? [];
        if (chip.zone === "minus") {
          decRatio(chip.i);
        } else if (chip.zone === "plus") {
          setRatio(chip.i, (ratios[chip.i] ?? 1) + 1);
        } else {
          dragTargetRef.current = { kind: "strike", i: chip.i };
        }
        return;
      }
      const rail = hitTestRail(x, y);
      if (rail !== null) {
        dragTargetRef.current = { kind: "strike", i: rail };
        return;
      }
      const exit = hitTestExit(x, y);
      if (exit !== null) {
        dragTargetRef.current = { kind: exit };
        return;
      }
      const strikeIdx = hitTestStrike(y);
      if (strikeIdx !== null) {
        dragTargetRef.current = { kind: "strike", i: strikeIdx };
      } else {
        dragRef.current = {
          startX: e.clientX,
          startY: e.clientY,
          startRight: viewRef.current.rightIndex,
          startDomain: currentDomain(barsRef.current, viewRef.current, overlayRef.current, surfaceRef.current),
          vActive: viewRef.current.yDomain !== null,
        };
      }
    },
    [hitTestChip, hitTestRail, hitTestExit, hitTestStrike, setRatio, decRatio],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      mouseRef.current = { x, y };
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);

      if (dragTargetRef.current !== null) {
        const target = dragTargetRef.current;
        const overlayNow = overlayRef.current;
        if (overlayNow) {
          const domain = currentDomain(barsRef.current, viewRef.current, overlayNow, surfaceRef.current);
          if (target.kind === "strike") {
            const price = yToPrice(y, domain, layout);
            const snaps = overlayNow.snapStrikes;
            if (snaps.length) {
              const snapped = snaps.reduce(
                (best, s) => (Math.abs(s - price) < Math.abs(best - price) ? s : best),
                snaps[0],
              );
              if (snapped !== overlayNow.strikes[target.i]) {
                setStrike(target.i, snapped);
              }
            }
          } else if (target.kind === "timestop") {
            // Drag the force-exit time horizontally; snapped to 5 minutes,
            // clamped inside [now+5m, 15:55 ET] and before expiry.
            const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = ((idx - anchorIdx) * tfMinutes) / 60;
            const nowMin = currentEtMinutes();
            const maxMin = Math.min(15 * 60 + 55, nowMin + overlayNow.hoursToExpiry * 60);
            const targetMin = Math.max(
              nowMin + 5,
              Math.min(Math.round((nowMin + hours * 60) / 5) * 5, maxMin),
            );
            const hh = String(Math.floor(targetMin / 60)).padStart(2, "0");
            const mm = String(targetMin % 60).padStart(2, "0");
            setTimeStopEt(`${hh}:${mm}`);
          } else if (overlayNow.legs && Math.abs(overlayNow.entry) >= 0.01) {
            // TP/SL drag: invert price@time back to a premium level with the
            // SAME smile-aware pricing the contours use, then store as %.
            const surface = surfaceRef.current;
            const anchorIdx = anchorIndexFor(barsRef.current, overlayNow);
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const hte = surface?.hoursToExpiry ?? overlayNow.hoursToExpiry;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = Math.max(0, Math.min(((idx - anchorIdx) * tfMinutes) / 60, hte));
            const tau = Math.max(hte - hours, 0) / TRADING_HOURS_PER_YEAR;
            const price = yToPrice(y, domain, layout);
            if (price > 0) {
              const premium = positionValueModel(overlayNow.legs, price, tau, overlayNow.model);
              const entry = overlayNow.entry;
              if (target.kind === "tp") {
                setTpPct((premium - entry) / Math.abs(entry));
              } else {
                setSlPct((entry - premium) / Math.abs(entry));
              }
            }
          }
        }
      } else if (axisDragRef.current) {
        // Price-axis drag: stretch the vertical scale around the domain center.
        const { startY, domain } = axisDragRef.current;
        const factor = Math.exp((y - startY) / 200);
        const center = (domain[0] + domain[1]) / 2;
        const half = ((domain[1] - domain[0]) / 2) * factor;
        if (half > 1e-9) viewRef.current.yDomain = [center - half, center + half];
      } else if (dragRef.current) {
        const drag = dragRef.current;
        const barW = layout.plotW / viewRef.current.barsVisible;
        viewRef.current.rightIndex = drag.startRight - (e.clientX - drag.startX) / barW;
        viewRef.current.follow = false;
        // Vertical pan engages past a small threshold so ordinary horizontal
        // pans keep auto-fit; once engaged the price scale goes manual.
        const dy = e.clientY - drag.startY;
        if (!drag.vActive && Math.abs(dy) > 8) drag.vActive = true;
        if (drag.vActive) {
          const [lo, hi] = drag.startDomain;
          const shift = (dy * (hi - lo)) / layout.volTop;
          viewRef.current.yDomain = [lo + shift, hi + shift];
        }
      } else {
        const overAxis = x > layout.plotW && y <= layout.plotH;
        const chip = overAxis ? null : hitTestChip(x, y);
        const exit = chip || overAxis ? null : hitTestExit(x, y);
        const cursor = overAxis
          ? "ns-resize"
          : chip
            ? chip.zone === "drag"
              ? "ns-resize"
              : "pointer"
            : hitTestRail(x, y) !== null
              ? "ns-resize"
              : exit === "timestop"
                ? "ew-resize"
                : exit !== null
                  ? "ns-resize"
                  : hitTestStrike(y) !== null
                    ? "ns-resize"
                    : "crosshair";
        if (canvasRef.current!.style.cursor !== cursor) {
          canvasRef.current!.style.cursor = cursor;
        }
      }
      draw();
    },
    [draw, hitTestChip, hitTestRail, hitTestExit, hitTestStrike, setStrike, setTpPct, setSlPct, setTimeStopEt],
  );

  const endDrag = useCallback(() => {
    dragRef.current = null;
    axisDragRef.current = null;
    dragTargetRef.current = null;
  }, []);

  const onMouseLeave = useCallback(() => {
    mouseRef.current = null;
    endDrag();
    draw();
  }, [draw, endDrag]);

  const onDoubleClick = useCallback(() => {
    const view = viewRef.current;
    view.follow = true;
    view.yDomain = null; // back to auto-fit
    view.rightIndex = Math.max(0, barsRef.current.n - 1) + view.barsVisible * RIGHT_PAD_FRAC;
    draw();
  }, [draw]);

  return (
    <div ref={wrapRef} className="relative h-full w-full cursor-crosshair overflow-hidden">
      {hudVariant !== "none" && (
        <ChartHud designer={designer} barsRef={barsRef} variant={hudVariant} />
      )}
      {/* Pointer events (not mouse) so touch drives the same pan/drag
          interactions on phones; touch-none stops the page from scrolling
          while dragging the chart. */}
      <canvas
        ref={canvasRef}
        className="touch-none"
        onWheel={onWheel}
        onPointerDown={onMouseDown}
        onPointerMove={onMouseMove}
        onPointerUp={endDrag}
        // A cancelled pointer (touch gesture takeover, capture loss) never
        // sends pointerup; without these a strike drag stays armed and every
        // later hover keeps rewriting that leg — presets appeared "stuck".
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
        onPointerLeave={onMouseLeave}
        onDoubleClick={onDoubleClick}
      />
    </div>
  );
}

// ------------------------------------------------------------- rendering

/** Earliest-in-time finite value of a contour line (its "active now" level). */
function firstFinite(line: Float64Array): number | null {
  for (let i = 0; i < line.length; i++) if (isFinite(line[i])) return line[i];
  return null;
}

function currentDomain(
  bars: Bars,
  view: ViewState,
  overlay: StrategyOverlay | null,
  surface: HeatmapResult | null = null,
): [number, number] {
  // Manual vertical scale (axis wheel/drag or chart vertical pan) wins;
  // double-click restores auto-fit.
  if (view.yDomain) return view.yDomain;
  const base = priceDomain(bars, view);
  if (!overlay) return base;
  const levels = [...overlay.strikes];
  if (overlay.spot) levels.push(overlay.spot);
  // Auto-fit also keeps the TP/SL execution boundaries on screen, so editing
  // the % fields visibly moves the lines into view.
  if (surface) {
    const tp = firstFinite(surface.tpLine);
    const sl = firstFinite(surface.slLine);
    if (tp !== null) levels.push(tp);
    if (sl !== null) levels.push(sl);
  }
  return extendDomain(base, levels);
}

/** Bar-index offset from the surface anchor (entry bar in position view,
 * latest bar in designer view) in trading time. */
function futureIndex(hoursFromAnchor: number, anchorIdx: number, tfMinutes: number): number {
  return anchorIdx + (hoursFromAnchor * 60) / tfMinutes;
}

function render(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  mouse: { x: number; y: number } | null,
  overlay: StrategyOverlay | null,
  surface: HeatmapResult | null,
  tfMinutes: number,
  dragTarget: DragTarget | null,
  indicators: IndicatorToggles,
  liveTick: { mid: number; ts: number } | null = null,
  showEth = false,
) {
  const draggingStrike = dragTarget?.kind === "strike" ? dragTarget.i : null;
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, layout.width, layout.height);
  ctx.font = "11px 'SF Mono', Consolas, monospace";

  if (!bars.n) {
    ctx.fillStyle = COLORS.axisText;
    ctx.textAlign = "center";
    ctx.fillText("NO DATA — waiting for feed", layout.plotW / 2, layout.plotH / 2);
    return;
  }

  const domain = currentDomain(bars, view, overlay, surface);
  const [first, last] = visibleRange(bars, view);
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));

  // Live-bar extension: a fresh quote newer than the last bar's start moves
  // the forming candle between feed bar-closes. Display-only.
  const lastIdx = bars.n - 1;
  const live =
    liveTick && liveTick.ts >= bars.t[lastIdx]
      ? {
          c: liveTick.mid,
          h: Math.max(bars.h[lastIdx], liveTick.mid),
          l: Math.min(bars.l[lastIdx], liveTick.mid),
        }
      : null;

  const anchorIdx = anchorIndexFor(bars, overlay);
  const etOff = etOffsetMinutes(bars.t[bars.n - 1]);
  drawPriceGrid(ctx, layout, domain);
  if (showEth) drawSessionZones(ctx, layout, bars, view, first, last, etOff);
  drawSessionBoundaries(ctx, layout, bars, view, first, last, etOff);
  drawTimeAxis(ctx, layout, bars, view, first, last, overlay, tfMinutes, anchorIdx);

  // Heatmap first: background layer in the future region (HEAT toggle).
  if (overlay?.legs && surface && indicators.heat) {
    drawHeatmap(ctx, layout, bars, view, domain, overlay, surface, tfMinutes, dragTarget, anchorIdx);
  }

  drawVolume(ctx, layout, bars, view, first, last);

  for (let i = first; i <= last; i++) {
    const x = indexToX(i, view, layout);
    if (x < -barW || x > layout.plotW + barW) continue;
    const isLive = live !== null && i === lastIdx;
    const c = isLive ? live.c : bars.c[i];
    const h = isLive ? live.h : bars.h[i];
    const l = isLive ? live.l : bars.l[i];
    const up = c >= bars.o[i];
    const color = up ? COLORS.up : COLORS.down;
    const yH = priceToY(h, domain, layout);
    const yL = priceToY(l, domain, layout);
    const yO = priceToY(bars.o[i], domain, layout);
    const yC = priceToY(c, domain, layout);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, yH);
    ctx.lineTo(x, yL);
    ctx.stroke();
    const top = Math.min(yO, yC);
    ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, Math.abs(yC - yO)));
  }

  drawIndicators(ctx, layout, bars, view, domain, first, last, indicators);
  if (indicators.theta && overlay && overlay.sigma > 0 && overlay.spot > 0) {
    drawExpectedMove(ctx, layout, view, domain, bars, overlay, tfMinutes);
  }
  if (overlay) {
    drawStrikes(ctx, layout, domain, overlay, draggingStrike);
    drawRail(ctx, layout, domain, overlay, draggingStrike);
  }
  if (overlay?.legs && surface) drawExitLevels(ctx, layout, domain, surface, overlay);
  drawLastPrice(ctx, layout, bars, domain, live?.c);
  if (mouse && mouse.x <= layout.plotW && mouse.y <= layout.plotH) {
    drawCrosshair(ctx, layout, bars, view, domain, mouse);
    if (overlay?.legs && surface) {
      drawPlTooltip(ctx, layout, view, domain, overlay, surface, mouse, tfMinutes, anchorIdx);
    }
  }
}

/** Hover readout over the P/L surface: price, trading-time offset, and the
 * model P/L at that exact point (computed live, not sampled from the grid). */
function drawPlTooltip(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  view: ViewState,
  domain: [number, number],
  overlay: StrategyOverlay,
  surface: HeatmapResult,
  mouse: { x: number; y: number },
  tfMinutes: number,
  anchorIdx: number,
) {
  if (mouse.y > layout.volTop) return;
  const idx = xToIndex(mouse.x, view, layout);
  const hours = ((idx - anchorIdx) * tfMinutes) / 60;
  if (hours < 0 || hours > surface.hoursToExpiry) return;
  const price = yToPrice(mouse.y, domain, layout);
  if (price <= 0) return;
  const tau = Math.max(surface.hoursToExpiry - hours, 0) / TRADING_HOURS_PER_YEAR;
  const pl = positionPlModel(overlay.legs!, price, tau, overlay.model) * 100;
  const hLabel = hours >= 6.5 ? `+${(hours / 6.5).toFixed(1)}d` : `+${hours.toFixed(1)}h`;
  const pastStop = hours > overlay.timeStopHours + 1e-9;
  const txt =
    `S ${fmtPrice(price)}  ${hLabel}  P/L ${pl >= 0 ? "+" : "-"}$${Math.abs(pl).toFixed(0)}/set` +
    (pastStop ? "  · past time stop" : "");
  const w = ctx.measureText(txt).width + 12;
  let bx = mouse.x + 14;
  if (bx + w > layout.plotW) bx = mouse.x - w - 14;
  let by = mouse.y - 26;
  if (by < 0) by = mouse.y + 14;
  const color = pl >= 0 ? COLORS.up : COLORS.down;
  ctx.fillStyle = "rgba(0,0,0,0.92)";
  ctx.fillRect(bx, by, w, 18);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.strokeRect(bx, by, w, 18);
  ctx.fillStyle = color;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(txt, bx + 6, by + 9);
}

/** R-normalized diverging color, capped ±2R. Returns [r,g,b,a255]. */
function plColorRGBA(pl: number, risk: number): [number, number, number, number] {
  const r = Math.max(Math.min(pl / risk, 2), -2);
  if (r >= 0) {
    const t = r / 2;
    return [0, Math.round(120 + 80 * t), Math.round(50 + 30 * t), Math.round(255 * (0.28 + 0.3 * t))];
  }
  const t = -r / 2;
  return [
    Math.round(150 + 105 * t),
    Math.round(23 * (1 - t) + 10),
    Math.round(50 * (1 - t) + 18),
    Math.round(255 * (0.28 + 0.3 * t)),
  ];
}

/**
 * Rasterize the P/L grid once per (surface, risk) into an offscreen canvas —
 * one pixel per cell — then let drawImage's bilinear filtering stretch it into
 * a CONTINUOUS gradient. Re-renders on pan/zoom are a single blit.
 */
let surfaceImage: { surface: HeatmapResult; risk: number; canvas: HTMLCanvasElement } | null = null;

function surfaceToImage(surface: HeatmapResult, risk: number): HTMLCanvasElement {
  if (surfaceImage && surfaceImage.surface === surface && surfaceImage.risk === risk) {
    return surfaceImage.canvas;
  }
  const { priceSteps, timeSteps, grid } = surface;
  const canvas = document.createElement("canvas");
  canvas.width = timeSteps;
  canvas.height = priceSteps;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(timeSteps, priceSteps);
  for (let ti = 0; ti < timeSteps; ti++) {
    for (let pi = 0; pi < priceSteps; pi++) {
      // Image row 0 = top = highest price = last price index.
      const row = priceSteps - 1 - pi;
      const [r, g, b, a] = plColorRGBA(grid[ti * priceSteps + pi], risk);
      const off = (row * timeSteps + ti) * 4;
      img.data[off] = r;
      img.data[off + 1] = g;
      img.data[off + 2] = b;
      img.data[off + 3] = a;
    }
  }
  ctx.putImageData(img, 0, 0);
  surfaceImage = { surface, risk, canvas };
  return canvas;
}

function drawHeatmap(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  overlay: StrategyOverlay,
  surface: HeatmapResult,
  tfMinutes: number,
  dragTarget: DragTarget | null,
  anchorIdx: number,
) {
  const risk = Math.max(
    overlay.slPremium !== null ? (overlay.entry - overlay.slPremium) * 100 : 100,
    1,
  );
  const x0 = indexToX(anchorIdx, view, layout);
  const xExpiry = indexToX(futureIndex(surface.hoursToExpiry, anchorIdx, tfMinutes), view, layout);
  if (xExpiry <= x0) return;

  const { priceLo, priceHi } = surface;
  const image = surfaceToImage(surface, risk);
  // Price axis is linear, so the grid maps affinely onto the plot. Half-cell
  // insets align pixel CENTERS with the grid's sample points.
  const halfPrice = (priceHi - priceLo) / (surface.priceSteps - 1) / 2;
  const yTop = priceToY(priceHi + halfPrice, domain, layout);
  const yBottom = priceToY(priceLo - halfPrice, domain, layout);
  ctx.save();
  ctx.beginPath();
  ctx.rect(Math.max(x0, 0), 0, layout.plotW - Math.max(x0, 0), layout.volTop);
  ctx.clip();
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(image, x0, yTop, xExpiry - x0, yBottom - yTop);
  ctx.restore();

  const { timeSteps } = surface;
  const colW = (xExpiry - x0) / timeSteps;
  const xTs = indexToX(
    futureIndex(Math.min(overlay.timeStopHours, surface.hoursToExpiry), anchorIdx, tfMinutes),
    view, layout,
  );

  // EXIT BOUNDARY: everything right of the time stop is a dead zone — the
  // enforcer force-closes there, so that P/L can never be realized.
  if (xTs < xExpiry) {
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(Math.max(xTs, 0), 0, Math.min(xExpiry, layout.plotW) - Math.max(xTs, 0), layout.volTop);
  }

  // Contours (drag the TP/SL lines to move the exits; % inputs stay synced).
  const lineAt = (line: Float64Array, color: string, dash: number[], width = 1.4) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    let last: [number, number] | null = null;
    for (let ti = 0; ti < timeSteps; ti++) {
      const s = line[ti];
      if (!isFinite(s)) {
        started = false;
        continue;
      }
      const x = x0 + (ti + 0.5) * colW;
      const y = priceToY(s, domain, layout);
      if (x < 0 || x > layout.plotW || y < 0 || y > layout.volTop) {
        started = false;
        continue;
      }
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
      last = [x, y];
    }
    ctx.stroke();
    ctx.setLineDash([]);
    return last;
  };
  const label = (at: [number, number] | null, text: string, color: string) => {
    if (!at) return;
    ctx.font = "10px 'SF Mono', Consolas, monospace";
    const w = ctx.measureText(text).width + 8;
    const bx = Math.min(Math.max(at[0] - w - 4, 0), layout.plotW - w);
    const by = Math.min(Math.max(at[1] - 15, 0), layout.volTop - 13);
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(bx, by, w, 13);
    ctx.strokeStyle = color;
    ctx.lineWidth = 0.8;
    ctx.strokeRect(bx, by, w, 13);
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, bx + w / 2, by + 6.5);
    ctx.font = "11px 'SF Mono', Consolas, monospace";
  };
  lineAt(surface.breakevenLine, COLORS.breakeven, [2, 3]);
  const tpEnd = lineAt(
    surface.tpLine, COLORS.tp, [6, 3], dragTarget?.kind === "tp" ? 2.6 : 1.6,
  );
  const slEnd = lineAt(
    surface.slLine, COLORS.sl, [6, 3], dragTarget?.kind === "sl" ? 2.6 : 1.6,
  );
  if (overlay.tpPremium !== null) {
    label(tpEnd, `TP ${overlay.tpPremium.toFixed(2)} (+${Math.round(overlay.tpPct * 100)}%) ⇕`, COLORS.tp);
  }
  if (overlay.slPremium !== null) {
    label(slEnd, `SL ${overlay.slPremium.toFixed(2)} (−${Math.round(overlay.slPct * 100)}%) ⇕`, COLORS.sl);
  }

  // Expiry + time-stop verticals.
  const vline = (x: number, color: string, text: string, width = 1) => {
    if (x < 0 || x > layout.plotW) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(text, x, 4);
  };
  vline(xExpiry, COLORS.expiry, "EXPIRY");
  vline(
    xTs,
    COLORS.timeStop,
    overlay.readOnly ? "TIME STOP" : "⇔ TIME STOP",
    dragTarget?.kind === "timestop" ? 2.4 : 1.2,
  );
  if (overlay.readOnly) vline(x0, "#9c8cff", "ENTRY", 1.2);

  // Closed-trade EXIT markers. A close that landed in WAVES (broker
  // auto-liquidation chunks) draws one line + chip per wave at its own
  // time and premium; the summary chip (reason · net premium · realized
  // P/L) anchors at the LAST wave. Single-fill exits keep one marker.
  if (overlay.exitHours !== null) {
    const waves =
      overlay.exitEvents.length > 1
        ? overlay.exitEvents
        : [{
            hours: overlay.exitHours,
            premium: overlay.exitPremium ?? NaN,
            qty: NaN,
          }];
    const win = (overlay.realizedPnl ?? 0) >= 0;
    const color = win ? COLORS.tp : COLORS.sl;
    ctx.font = "11px 'SF Mono', Consolas, monospace";
    let chipY = 20;
    const chip = (text: string, xAt: number, emphasize: boolean) => {
      const w = ctx.measureText(text).width + 10;
      const cx = Math.min(Math.max(xAt - w / 2, 2), layout.plotW - w - 2);
      ctx.fillStyle = "rgba(0,0,0,0.88)";
      ctx.fillRect(cx, chipY, w, CHIP_H);
      ctx.globalAlpha = emphasize ? 1 : 0.75;
      ctx.strokeStyle = color;
      ctx.strokeRect(cx, chipY, w, CHIP_H);
      ctx.fillStyle = color;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(text, cx + 5, chipY + CHIP_H / 2);
      ctx.globalAlpha = 1;
      chipY += CHIP_H + 4; // stack chips of near-coincident waves
    };
    for (let i = 0; i < waves.length; i++) {
      const wave = waves[i];
      const last = i === waves.length - 1;
      const xExit = indexToX(futureIndex(wave.hours, anchorIdx, tfMinutes), view, layout);
      if (xExit < 0 || xExit > layout.plotW) continue;
      ctx.strokeStyle = color;
      ctx.globalAlpha = last ? 1 : 0.65;
      ctx.lineWidth = last ? 1.6 : 1.2;
      ctx.beginPath();
      ctx.moveTo(xExit, 0);
      ctx.lineTo(xExit, layout.plotH);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1;
      if (waves.length > 1) {
        chip(`−${wave.qty}× @${wave.premium.toFixed(2)}`, xExit, false);
      }
      if (last) {
        const parts = [`EXIT ${(overlay.exitReason ?? "closed").toUpperCase()}`];
        if (overlay.exitPremium !== null) parts.push(`@${overlay.exitPremium.toFixed(2)}`);
        if (overlay.realizedPnl !== null) {
          parts.push(`${overlay.realizedPnl >= 0 ? "+" : "−"}$${Math.abs(overlay.realizedPnl).toFixed(0)}`);
        }
        chip(parts.join(" "), xExit, true);
      }
    }
  }

  // Terminal density strip along the expiry line (model-implied lognormal).
  if (overlay.sigma > 0 && overlay.spot > 0 && xExpiry > 0 && xExpiry <= layout.plotW) {
    const tau = surface.hoursToExpiry / TRADING_HOURS_PER_YEAR;
    const sq = overlay.sigma * Math.sqrt(tau);
    if (sq > 0) {
      const mu = Math.log(overlay.spot) + (0.05 - 0.5 * overlay.sigma ** 2) * tau;
      const maxW = 46;
      ctx.fillStyle = "rgba(255,176,0,0.20)";
      ctx.beginPath();
      ctx.moveTo(xExpiry, priceToY(domain[1], domain, layout));
      const samples = 80;
      let peak = 0;
      const densities: number[] = [];
      for (let i = 0; i <= samples; i++) {
        const p = domain[0] + ((domain[1] - domain[0]) * i) / samples;
        const d = p > 0 ? normPdf((Math.log(p) - mu) / sq) / (p * sq) : 0;
        densities.push(d);
        if (d > peak) peak = d;
      }
      for (let i = 0; i <= samples; i++) {
        const p = domain[0] + ((domain[1] - domain[0]) * i) / samples;
        const w = peak > 0 ? (densities[i] / peak) * maxW : 0;
        ctx.lineTo(xExpiry - w, priceToY(p, domain, layout));
      }
      ctx.lineTo(xExpiry, priceToY(domain[0], domain, layout));
      ctx.closePath();
      ctx.fill();
    }
  }
}

function drawStrikes(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
  draggingStrike: number | null,
) {
  // Lines first (chips render on top, staggered for same-strike legs).
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const isShort = overlay.strikeSides[i] < 0;
    ctx.strokeStyle = isShort ? COLORS.strikeShort : COLORS.strike;
    ctx.lineWidth = draggingStrike === i ? 2.5 : 1.5;
    ctx.setLineDash(isShort ? [8, 4] : []);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
  });
  // Chips: [−] ±ratio right strike [+] — click zones edit contracts, the
  // middle (or the line/rail handle) drags the strike. Read-only (position
  // view): plain labels, no edit affordances.
  for (const rect of computeChipRects(ctx, layout, domain, overlay)) {
    const isShort = overlay.strikeSides[rect.i] < 0;
    const color = isShort ? COLORS.strikeShort : COLORS.strike;
    ctx.fillStyle = "rgba(0,0,0,0.88)";
    ctx.fillRect(rect.x, rect.y - CHIP_H / 2, rect.w, CHIP_H);
    ctx.strokeStyle = color;
    ctx.lineWidth = draggingStrike === rect.i ? 1.8 : 1;
    ctx.strokeRect(rect.x, rect.y - CHIP_H / 2, rect.w, CHIP_H);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (!overlay.readOnly) {
      ctx.fillStyle = COLORS.axisText;
      ctx.fillText("−", rect.x + CHIP_ZONE / 2 + 1, rect.y);
      ctx.fillText("+", rect.x + rect.w - CHIP_ZONE / 2 - 1, rect.y);
    }
    ctx.fillStyle = color;
    ctx.fillText(rect.label, rect.x + rect.w / 2, rect.y);
  }
}

/** Vertical rail: the drag track for strike handles, just inside the price
 * axis so handles read directly against the scale. */
function drawRail(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  overlay: StrategyOverlay,
  draggingStrike: number | null,
) {
  if (overlay.readOnly) return; // position view: strikes aren't editable
  const railX = layout.plotW - RAIL_INSET;
  ctx.strokeStyle = "#2a2a2a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(railX, 0);
  ctx.lineTo(railX, layout.volTop);
  ctx.stroke();
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const isShort = overlay.strikeSides[i] < 0;
    const color = isShort ? COLORS.strikeShort : COLORS.strike;
    const r = draggingStrike === i ? 7 : 5;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(railX, y - r);
    ctx.lineTo(railX + r, y);
    ctx.lineTo(railX, y + r);
    ctx.lineTo(railX - r, y);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = COLORS.bg;
    ctx.stroke();
  });
}

const IND_COLORS = {
  vwap: "#26C6DA",
  ema9: "#FFD54F",
  ema21: "#BA68C8",
  bb: "rgba(96,125,139,0.55)",
  bbFill: "rgba(96,125,139,0.08)",
};

/** Overlay indicator lines across the visible bar range. */
/** Theta-sell context: ±1σ expected-move cone from NOW to expiry. Short
 * strikes that sit inside the cone are the ones the market expects to
 * touch — the core visual for premium selling. */
function drawExpectedMove(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  view: ViewState,
  domain: [number, number],
  bars: Bars,
  overlay: StrategyOverlay,
  tfMinutes: number,
) {
  const nowIdx = bars.n - 1;
  const spot = overlay.spot;
  const sigma = overlay.sigma;
  const hours = overlay.hoursToExpiry;
  if (hours <= 0) return;
  const STEPS = 40;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, layout.plotW, layout.plotH);
  ctx.clip();

  const upper: [number, number][] = [];
  const lower: [number, number][] = [];
  for (let i = 0; i <= STEPS; i++) {
    const h = (hours * i) / STEPS;
    const tau = h / TRADING_HOURS_PER_YEAR;
    const move = Math.exp(sigma * Math.sqrt(tau));
    const x = indexToX(futureIndex(h, nowIdx, tfMinutes), view, layout);
    upper.push([x, priceToY(spot * move, domain, layout)]);
    lower.push([x, priceToY(spot / move, domain, layout)]);
  }

  ctx.fillStyle = "rgba(255,166,0,0.05)";
  ctx.beginPath();
  upper.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
  for (let i = lower.length - 1; i >= 0; i--) ctx.lineTo(lower[i][0], lower[i][1]);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = "rgba(255,166,0,0.55)";
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 4]);
  for (const line of [upper, lower]) {
    ctx.beginPath();
    line.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    ctx.stroke();
  }
  ctx.setLineDash([]);
  const endUp = upper[upper.length - 1];
  ctx.fillStyle = "rgba(255,166,0,0.8)";
  ctx.textAlign = "right";
  ctx.fillText("±1σ EM", Math.min(endUp[0], layout.plotW) - 4, endUp[1] - 4);
  ctx.restore();
}

function drawIndicators(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  first: number,
  last: number,
  indicators: IndicatorToggles,
) {
  if (!bars.n || last <= first) return;
  const line = (values: Float64Array, color: string, width = 1.2, dash: number[] = []) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    for (let i = first; i <= last; i++) {
      const x = indexToX(i, view, layout);
      const y = priceToY(values[i], domain, layout);
      if (x < -20 || x > layout.plotW + 20 || !isFinite(y)) {
        started = false;
        continue;
      }
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  };

  if (indicators.bb) {
    const bb = computeBollinger(bars);
    // Band fill between upper and lower.
    ctx.fillStyle = IND_COLORS.bbFill;
    ctx.beginPath();
    let started = false;
    for (let i = first; i <= last; i++) {
      const x = indexToX(i, view, layout);
      const y = priceToY(bb.upper[i], domain, layout);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    for (let i = last; i >= first; i--) {
      ctx.lineTo(indexToX(i, view, layout), priceToY(bb.lower[i], domain, layout));
    }
    ctx.closePath();
    ctx.fill();
    line(bb.upper, IND_COLORS.bb, 1, [3, 3]);
    line(bb.lower, IND_COLORS.bb, 1, [3, 3]);
    line(bb.mid, IND_COLORS.bb, 1);
  }
  if (indicators.ema) {
    line(computeEma(bars.c, bars.n, 9), IND_COLORS.ema9, 1.2);
    line(computeEma(bars.c, bars.n, 21), IND_COLORS.ema21, 1.2);
  }
  if (indicators.vwap) {
    line(computeVwap(bars), IND_COLORS.vwap, 1.4, [6, 3]);
  }
}

/**
 * Always-visible execution boundaries: horizontal guides + price-axis badges
 * at the underlying level where TP / SL would fire at the earliest active
 * time. Directly driven by the TP%/SL% fields (and contour drags). Off-scale
 * levels clamp to the axis edge with a direction arrow instead of vanishing.
 * Breakevens (model-free, at the active premium basis) get white badges.
 */
function drawExitLevels(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  domain: [number, number],
  surface: HeatmapResult,
  overlay: StrategyOverlay,
) {
  const drawLevel = (level: number | null, color: string, tag: string) => {
    if (level === null) return;
    const y = priceToY(level, domain, layout);
    const onScreen = y >= 0 && y <= layout.volTop;
    if (onScreen) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([1, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(layout.plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    const text = onScreen ? `${tag} ${fmtPrice(level)}` : `${tag} ${y < 0 ? "↑" : "↓"}`;
    ctx.fillStyle = color;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, layout.plotW + 4, by);
  };
  // Breakevens first so TP/SL badges draw over them on overlap.
  for (const be of overlay.breakevens.slice(0, 3)) {
    const y = priceToY(be, domain, layout);
    if (y >= 0 && y <= layout.volTop) {
      ctx.strokeStyle = COLORS.breakeven;
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 5]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(layout.plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const by = Math.max(8, Math.min(layout.volTop - 8, y));
    ctx.fillStyle = COLORS.breakeven;
    ctx.fillRect(layout.plotW, by - 8, layout.axisW, 16);
    ctx.fillStyle = COLORS.bg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`BE ${fmtPrice(be)}`, layout.plotW + 4, by);
  }
  drawLevel(firstFinite(surface.tpLine), COLORS.tp, "TP");
  drawLevel(firstFinite(surface.slLine), COLORS.sl, "SL");
}

function niceStep(span: number, maxTicks: number): number {
  const raw = span / maxTicks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (raw <= mult * mag) return mult * mag;
  }
  return 10 * mag;
}

function drawPriceGrid(ctx: CanvasRenderingContext2D, layout: Layout, domain: [number, number]) {
  const [lo, hi] = domain;
  const step = niceStep(hi - lo, Math.max(3, Math.floor(layout.volTop / 60)));
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (let p = Math.ceil(lo / step) * step; p <= hi; p += step) {
    const y = priceToY(p, domain, layout);
    ctx.strokeStyle = COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.fillStyle = COLORS.axisText;
    ctx.fillText(fmtPrice(p), layout.plotW + 6, y);
  }
}

function drawTimeAxis(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  overlay: StrategyOverlay | null,
  tfMinutes: number,
  anchorIdx: number,
) {
  const targetPx = 90;
  const step = Math.max(1, Math.round((view.barsVisible * targetPx) / layout.plotW));
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = Math.ceil(first / step) * step; i <= last; i += step) {
    const x = indexToX(i, view, layout);
    if (x < 0 || x > layout.plotW) continue;
    ctx.strokeStyle = COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    const isNewDay = i > 0 && fmtDayET(bars.t[i]) !== fmtDayET(bars.t[i - 1]);
    ctx.fillStyle = isNewDay ? COLORS.last : COLORS.axisText;
    ctx.fillText(isNewDay ? fmtDayET(bars.t[i]) : fmtTimeET(bars.t[i]), x, layout.plotH + 6);
  }
  // Future-region ticks: +1h, +2h, ... in trading hours.
  if (overlay?.legs) {
    ctx.fillStyle = COLORS.expiry;
    const hteCeil = Math.ceil(overlay.hoursToExpiry);
    const hourStep = overlay.hoursToExpiry > 14 ? 6.5 : 1;
    for (let h = hourStep; h < hteCeil; h += hourStep) {
      const x = indexToX(futureIndex(h, anchorIdx, tfMinutes), view, layout);
      if (x < 0 || x > layout.plotW) continue;
      const label = hourStep === 6.5 ? `+${Math.round(h / 6.5)}d` : `+${h}h`;
      ctx.fillText(label, x, layout.plotH + 6);
    }
  }
}

function drawVolume(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
) {
  let maxV = 0;
  for (let i = first; i <= last; i++) if (bars.v[i] > maxV) maxV = bars.v[i];
  if (!maxV) return;
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));
  const volH = layout.plotH - layout.volTop - 1;
  for (let i = first; i <= last; i++) {
    const x = indexToX(i, view, layout);
    if (x < -barW || x > layout.plotW + barW) continue;
    const h = (bars.v[i] / maxV) * volH;
    ctx.fillStyle = bars.c[i] >= bars.o[i] ? COLORS.volUp : COLORS.volDown;
    ctx.fillRect(x - bodyW / 2, layout.plotH - h, bodyW, h);
  }
}

/** ET wall-clock offset (minutes to add to UTC epoch-minutes), one Intl call.
 * Cached per render; constant across a chart window except across a DST
 * transition mid-window, where being an hour off on shading is cosmetic. */
function etOffsetMinutes(ms: number): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    })
      .formatToParts(new Date(ms))
      .map((p) => [p.type, p.value]),
  );
  const wall = Date.UTC(
    Number(parts.year), Number(parts.month) - 1, Number(parts.day),
    Number(parts.hour === "24" ? 0 : parts.hour), Number(parts.minute), Number(parts.second),
  );
  return Math.round((wall - ms) / 60_000);
}

const RTH_START_MIN = 9 * 60 + 30; // 09:30 ET
const RTH_END_MIN = 16 * 60; //       16:00 ET

/** Extended-hours shading: bars outside 09:30–16:00 ET get a subtle tint so
 * pre/after-market price action reads as a different regime at a glance. */
function drawSessionZones(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  etOff: number,
) {
  const halfBar = layout.plotW / view.barsVisible / 2;
  ctx.fillStyle = "rgba(104,138,196,0.16)";
  let runStart: number | null = null;
  const flush = (endIdx: number) => {
    if (runStart === null) return;
    const x0 = Math.max(indexToX(runStart, view, layout) - halfBar, 0);
    const x1 = Math.min(indexToX(endIdx, view, layout) + halfBar, layout.plotW);
    if (x1 > x0) ctx.fillRect(x0, 0, x1 - x0, layout.plotH);
    runStart = null;
  };
  for (let i = first; i <= last; i++) {
    const minuteOfDay = (((bars.t[i] / 60_000 + etOff) % 1440) + 1440) % 1440;
    const extended = minuteOfDay < RTH_START_MIN || minuteOfDay >= RTH_END_MIN;
    if (extended && runStart === null) runStart = i;
    if (!extended) flush(i - 1);
  }
  flush(last);
}

/** Session-day boundaries: a full-height line + date chip at the first bar of
 * each new ET trading day, across ALL visible history. Day detection is pure
 * arithmetic on the cached ET offset — no per-bar Intl calls. */
function drawSessionBoundaries(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  first: number,
  last: number,
  etOff: number,
) {
  const dayIndex = (i: number) => Math.floor((bars.t[i] / 60_000 + etOff) / 1440);
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  for (let i = Math.max(first, 1); i <= last; i++) {
    if (dayIndex(i) === dayIndex(i - 1)) continue;
    const x = indexToX(i, view, layout) - layout.plotW / view.barsVisible / 2;
    if (x < 0 || x > layout.plotW) continue;
    ctx.strokeStyle = "#3a3a3a";
    ctx.setLineDash([2, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    ctx.setLineDash([]);
    const label = fmtDayET(bars.t[i]).toUpperCase();
    ctx.fillStyle = "rgba(0,0,0,0.8)";
    const w = ctx.measureText(label).width + 8;
    ctx.fillRect(x + 2, 2, w, 13);
    ctx.fillStyle = COLORS.axisText;
    ctx.fillText(label, x + 6, 4);
  }
}

function drawLastPrice(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  domain: [number, number],
  liveClose?: number,
) {
  const lastClose = liveClose ?? bars.c[bars.n - 1];
  const y = priceToY(lastClose, domain, layout);
  if (y < 0 || y > layout.volTop) return;
  ctx.strokeStyle = COLORS.last;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(layout.plotW, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = COLORS.last;
  ctx.fillRect(layout.plotW, y - 8, layout.axisW, 16);
  ctx.fillStyle = COLORS.bg;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(fmtPrice(lastClose), layout.plotW + 6, y);
}

function drawCrosshair(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  mouse: { x: number; y: number },
) {
  ctx.strokeStyle = COLORS.crosshair;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(mouse.x, 0);
  ctx.lineTo(mouse.x, layout.plotH);
  ctx.moveTo(0, mouse.y);
  ctx.lineTo(layout.plotW, mouse.y);
  ctx.stroke();
  ctx.setLineDash([]);

  const price = yToPrice(mouse.y, domain, layout);
  ctx.fillStyle = "#222222";
  ctx.fillRect(layout.plotW, mouse.y - 8, layout.axisW, 16);
  ctx.fillStyle = "#ffffff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(fmtPrice(price), layout.plotW + 6, mouse.y);

  const index = Math.round(xToIndex(mouse.x, view, layout));
  if (index >= 0 && index < bars.n) {
    const parts = [
      fmtDayET(bars.t[index]) + " " + fmtTimeET(bars.t[index]),
      "O " + fmtPrice(bars.o[index]),
      "H " + fmtPrice(bars.h[index]),
      "L " + fmtPrice(bars.l[index]),
      "C " + fmtPrice(bars.c[index]),
      "V " + Intl.NumberFormat("en-US").format(bars.v[index]),
    ];
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(6, 6, 430, 18);
    ctx.fillStyle = bars.c[index] >= bars.o[index] ? COLORS.up : COLORS.down;
    ctx.textAlign = "left";
    ctx.fillText(parts.join("   "), 12, 15);
  }
}
