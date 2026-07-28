import { useCallback, useEffect, useMemo, useRef } from "react";
import { barsTable } from "../../lib/perspective";
import { focusFeed, onSnapshot } from "../../lib/barFeed";
import type { HeatmapResult } from "../../lib/heatmap.worker";
import {
  normPdf,
  positionEntryCost,
  positionIv,
  positionPlSmile,
  positionValueSmile,
  TRADING_HOURS_PER_YEAR,
  type Leg,
  type Smiles,
} from "../../lib/optionsMath";
import { useHeatmap } from "../../lib/useHeatmap";
import { TF_MS, useTradingStore } from "../../store/tradingStore";
import {
  availableStrikes,
  buildLegs,
  hoursToExpiry as calcHoursToExpiry,
  smileFromChain,
  strategyDef,
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
const CHIP_X = 6;
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
};

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

export function CandlePane() {
  const symbol = useTradingStore((s) => s.symbol);
  const tf = useTradingStore((s) => s.tf);
  const quote = useTradingStore((s) => s.quote);

  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const kind = useStrategyStore((s) => s.kind);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const tpPct = useStrategyStore((s) => s.tpPct);
  const slPct = useStrategyStore((s) => s.slPct);
  const timeStopEt = useStrategyStore((s) => s.timeStopEt);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);
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
  const surfaceRef = useRef<HeatmapResult | null>(null);
  const overlayRef = useRef<StrategyOverlay | null>(null);
  const rafRef = useRef(0);

  // ------------------------------------------------- strategy derivations

  const overlay: StrategyOverlay | null = useMemo(() => {
    const legs = buildLegs({ chain, expiry, kind, strikes, ratios });
    if (!chain || !expiry) return null;
    const spot = quote?.mid || chain.spot;
    const hte = calcHoursToExpiry(expiry);
    const entry = legs ? positionEntryCost(legs) : 0;
    const sides = strategyDef(kind).legs.map((l) => l.side);
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
    const template = strategyDef(kind).legs;
    return {
      legs,
      strikes,
      strikeSides: sides,
      strikeRights: template.map((l) => l.right),
      ratios: template.map((l, i) => ratios[i] ?? l.ratio),
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
    };
  }, [chain, expiry, kind, strikes, ratios, tpPct, slPct, timeStopEt, quote]);

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
      render(
        ctx,
        computeLayout(cssW, cssH),
        barsRef.current,
        viewRef.current,
        mouseRef.current,
        overlayRef.current,
        surfaceRef.current,
        TF_MS[tf] / 60000,
        dragTargetRef.current,
      );
    });
  }, [tf]);

  // Surface recompute (worker) — remaps on pan/zoom without recompute.
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
    };
  }, [overlay]);

  useHeatmap(surfaceInputs, (result) => {
    surfaceRef.current = result;
    draw();
  });

  useEffect(() => {
    if (!overlay?.legs) surfaceRef.current = null;
    draw();
  }, [overlay, draw]);

  // ------------------------------------------------------- data plumbing

  useEffect(() => {
    let disposed = false;
    let view: {
      delete(): Promise<void>;
      on_update(cb: () => void): void;
      to_columns(): Promise<Record<string, unknown[]>>;
    } | null = null;

    focusFeed(symbol, tf);

    async function pull() {
      if (!view || disposed) return;
      const cols = await view.to_columns();
      if (disposed) return;
      const t = cols.t as number[] | undefined;
      const n = t?.length ?? 0;
      barsRef.current = n
        ? {
            t: Float64Array.from(cols.t as number[]),
            o: Float64Array.from(cols.o as number[]),
            h: Float64Array.from(cols.h as number[]),
            l: Float64Array.from(cols.l as number[]),
            c: Float64Array.from(cols.c as number[]),
            v: Float64Array.from(cols.v as number[]),
            n,
          }
        : EMPTY;
      const v = viewRef.current;
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
  }, [symbol, tf, draw]);

  useEffect(() => {
    const observer = new ResizeObserver(draw);
    if (wrapRef.current) observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [draw]);

  // ---------------------------------------------------------- interactions

  const hitTestStrike = useCallback((y: number): number | null => {
    const overlayNow = overlayRef.current;
    const wrap = wrapRef.current;
    if (!overlayNow || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
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
    if (!overlayNow || !overlayNow.strikes.length || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    if (Math.abs(x - (layout.plotW - RAIL_INSET)) > RAIL_HIT_PX) return null;
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
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
    if (!overlayNow?.legs || !surface || !wrap) return null;
    const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
    const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
    const view = viewRef.current;
    const n = barsRef.current.n;
    const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
    if (y > layout.volTop) return null;
    const xTs = indexToX(futureIndex(overlayNow.timeStopHours, n, tfMinutes), view, layout);
    if (Math.abs(x - xTs) <= 6) return "timestop";
    const x0 = indexToX(n - 1, view, layout);
    const xExp = indexToX(futureIndex(surface.hoursToExpiry, n, tfMinutes), view, layout);
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
      if (!overlayNow || !overlayNow.strikes.length || !wrap || !canvas) return null;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
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
        const domain = currentDomain(barsRef.current, view, overlayRef.current);
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
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      if (x > layout.plotW && y <= layout.plotH) {
        // Grab the price axis: vertical scale drag.
        axisDragRef.current = {
          startY: y,
          domain: currentDomain(barsRef.current, viewRef.current, overlayRef.current),
        };
        return;
      }
      const chip = hitTestChip(x, y);
      if (chip) {
        const ratios = overlayRef.current?.ratios ?? [];
        if (chip.zone === "minus") {
          setRatio(chip.i, (ratios[chip.i] ?? 1) - 1);
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
          startDomain: currentDomain(barsRef.current, viewRef.current, overlayRef.current),
          vActive: viewRef.current.yDomain !== null,
        };
      }
    },
    [hitTestChip, hitTestRail, hitTestExit, hitTestStrike, setRatio],
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
          const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
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
            const n = barsRef.current.n;
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = ((idx - (n - 1)) * tfMinutes) / 60;
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
            const n = barsRef.current.n;
            const tfMinutes = TF_MS[useTradingStore.getState().tf] / 60000;
            const hte = surface?.hoursToExpiry ?? overlayNow.hoursToExpiry;
            const idx = xToIndex(x, viewRef.current, layout);
            const hours = Math.max(0, Math.min(((idx - (n - 1)) * tfMinutes) / 60, hte));
            const tau = Math.max(hte - hours, 0) / TRADING_HOURS_PER_YEAR;
            const price = yToPrice(y, domain, layout);
            if (price > 0) {
              const premium = positionValueSmile(
                overlayNow.legs, price, tau, overlayNow.spot, overlayNow.smiles,
              );
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
      <canvas
        ref={canvasRef}
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={endDrag}
        onMouseLeave={onMouseLeave}
        onDoubleClick={onDoubleClick}
      />
    </div>
  );
}

// ------------------------------------------------------------- rendering

function currentDomain(bars: Bars, view: ViewState, overlay: StrategyOverlay | null): [number, number] {
  // Manual vertical scale (axis wheel/drag or chart vertical pan) wins;
  // double-click restores auto-fit.
  if (view.yDomain) return view.yDomain;
  const base = priceDomain(bars, view);
  if (!overlay) return base;
  const levels = [...overlay.strikes];
  if (overlay.spot) levels.push(overlay.spot);
  return extendDomain(base, levels);
}

function futureIndex(hoursFromNow: number, n: number, tfMinutes: number): number {
  return n - 1 + (hoursFromNow * 60) / tfMinutes;
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

  const domain = currentDomain(bars, view, overlay);
  const [first, last] = visibleRange(bars, view);
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));

  drawPriceGrid(ctx, layout, domain);
  drawTimeAxis(ctx, layout, bars, view, first, last, overlay, tfMinutes);

  // Heatmap first: background layer in the future region.
  if (overlay?.legs && surface) {
    drawHeatmap(ctx, layout, bars, view, domain, overlay, surface, tfMinutes, dragTarget);
  }

  drawVolume(ctx, layout, bars, view, first, last);

  for (let i = first; i <= last; i++) {
    const x = indexToX(i, view, layout);
    if (x < -barW || x > layout.plotW + barW) continue;
    const up = bars.c[i] >= bars.o[i];
    const color = up ? COLORS.up : COLORS.down;
    const yH = priceToY(bars.h[i], domain, layout);
    const yL = priceToY(bars.l[i], domain, layout);
    const yO = priceToY(bars.o[i], domain, layout);
    const yC = priceToY(bars.c[i], domain, layout);
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

  if (overlay) {
    drawStrikes(ctx, layout, domain, overlay, draggingStrike);
    drawRail(ctx, layout, domain, overlay, draggingStrike);
  }
  drawLastPrice(ctx, layout, bars, domain);
  if (mouse && mouse.x <= layout.plotW && mouse.y <= layout.plotH) {
    drawCrosshair(ctx, layout, bars, view, domain, mouse);
    if (overlay?.legs && surface) {
      drawPlTooltip(ctx, layout, bars, view, domain, overlay, surface, mouse, tfMinutes);
    }
  }
}

/** Hover readout over the P/L surface: price, trading-time offset, and the
 * model P/L at that exact point (computed live, not sampled from the grid). */
function drawPlTooltip(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  domain: [number, number],
  overlay: StrategyOverlay,
  surface: HeatmapResult,
  mouse: { x: number; y: number },
  tfMinutes: number,
) {
  if (mouse.y > layout.volTop) return;
  const n = bars.n;
  const idx = xToIndex(mouse.x, view, layout);
  const hours = ((idx - (n - 1)) * tfMinutes) / 60;
  if (hours < 0 || hours > surface.hoursToExpiry) return;
  const price = yToPrice(mouse.y, domain, layout);
  if (price <= 0) return;
  const tau = Math.max(surface.hoursToExpiry - hours, 0) / TRADING_HOURS_PER_YEAR;
  const pl = positionPlSmile(overlay.legs!, price, tau, overlay.spot, overlay.smiles) * 100;
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
) {
  const n = bars.n;
  const risk = Math.max(
    overlay.slPremium !== null ? (overlay.entry - overlay.slPremium) * 100 : 100,
    1,
  );
  const x0 = indexToX(n - 1, view, layout);
  const xExpiry = indexToX(futureIndex(surface.hoursToExpiry, n, tfMinutes), view, layout);
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
    futureIndex(Math.min(overlay.timeStopHours, surface.hoursToExpiry), n, tfMinutes),
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
  vline(xTs, COLORS.timeStop, "⇔ TIME STOP", dragTarget?.kind === "timestop" ? 2.4 : 1.2);

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
  // middle (or the line/rail handle) drags the strike.
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
    ctx.fillStyle = COLORS.axisText;
    ctx.fillText("−", rect.x + CHIP_ZONE / 2 + 1, rect.y);
    ctx.fillText("+", rect.x + rect.w - CHIP_ZONE / 2 - 1, rect.y);
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
      const x = indexToX(futureIndex(h, bars.n, tfMinutes), view, layout);
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

function drawLastPrice(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  domain: [number, number],
) {
  const lastClose = bars.c[bars.n - 1];
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
