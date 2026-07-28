import { useCallback, useEffect, useMemo, useRef } from "react";
import { barsTable } from "../../lib/perspective";
import { focusFeed, onSnapshot } from "../../lib/barFeed";
import type { HeatmapResult } from "../../lib/heatmap.worker";
import {
  normPdf,
  positionEntryCost,
  positionIv,
  positionPl,
  TRADING_HOURS_PER_YEAR,
  type Leg,
} from "../../lib/optionsMath";
import { useHeatmap } from "../../lib/useHeatmap";
import { TF_MS, useTradingStore } from "../../store/tradingStore";
import {
  availableStrikes,
  buildLegs,
  hoursToExpiry as calcHoursToExpiry,
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

type StrategyOverlay = {
  legs: (Leg & { symbol: string })[] | null;
  strikes: number[];
  strikeSides: number[]; // +1 long leg, -1 short leg (for coloring)
  snapStrikes: number[];
  hoursToExpiry: number;
  timeStopHours: number;
  tpPremium: number | null;
  slPremium: number | null;
  entry: number;
  sigma: number;
  spot: number;
};

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

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const barsRef = useRef<Bars>(EMPTY);
  const viewRef = useRef<ViewState>({ rightIndex: 0, barsVisible: 120, follow: true });
  const mouseRef = useRef<{ x: number; y: number } | null>(null);
  const dragRef = useRef<{ startX: number; startRight: number } | null>(null);
  const strikeDragRef = useRef<number | null>(null); // index into strikes
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
    return {
      legs,
      strikes,
      strikeSides: sides,
      snapStrikes: availableStrikes(chain, expiry),
      hoursToExpiry: hte,
      timeStopHours,
      // Signed premium axis: TP above entry, SL below — credit-safe.
      tpPremium: priced ? entry + Math.abs(entry) * tpPct : null,
      slPremium: priced ? entry - Math.abs(entry) * slPct : null,
      entry,
      sigma: legs ? positionIv(legs) : 0,
      spot,
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
        strikeDragRef.current,
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

  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      const view = viewRef.current;
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);
      const rect = canvasRef.current!.getBoundingClientRect();
      const anchor = xToIndex(e.clientX - rect.left, view, layout);
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
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
      const y = e.clientY - rect.top;
      const strikeIdx = hitTestStrike(y);
      if (strikeIdx !== null) {
        strikeDragRef.current = strikeIdx;
      } else {
        dragRef.current = { startX: e.clientX, startRight: viewRef.current.rightIndex };
      }
    },
    [hitTestStrike],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      mouseRef.current = { x, y };
      const wrap = wrapRef.current!;
      const layout = computeLayout(wrap.clientWidth, wrap.clientHeight);

      if (strikeDragRef.current !== null) {
        const overlayNow = overlayRef.current;
        if (overlayNow) {
          const domain = currentDomain(barsRef.current, viewRef.current, overlayNow);
          const price = yToPrice(y, domain, layout);
          const snaps = overlayNow.snapStrikes;
          if (snaps.length) {
            const snapped = snaps.reduce(
              (best, s) => (Math.abs(s - price) < Math.abs(best - price) ? s : best),
              snaps[0],
            );
            if (snapped !== overlayNow.strikes[strikeDragRef.current]) {
              setStrike(strikeDragRef.current, snapped);
            }
          }
        }
      } else if (dragRef.current) {
        const barW = layout.plotW / viewRef.current.barsVisible;
        viewRef.current.rightIndex =
          dragRef.current.startRight - (e.clientX - dragRef.current.startX) / barW;
        viewRef.current.follow = false;
      } else {
        const cursor = hitTestStrike(y) !== null ? "ns-resize" : "crosshair";
        if (canvasRef.current!.style.cursor !== cursor) {
          canvasRef.current!.style.cursor = cursor;
        }
      }
      draw();
    },
    [draw, hitTestStrike, setStrike],
  );

  const endDrag = useCallback(() => {
    dragRef.current = null;
    strikeDragRef.current = null;
  }, []);

  const onMouseLeave = useCallback(() => {
    mouseRef.current = null;
    endDrag();
    draw();
  }, [draw, endDrag]);

  const onDoubleClick = useCallback(() => {
    const view = viewRef.current;
    view.follow = true;
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
  draggingStrike: number | null,
) {
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
    drawHeatmap(ctx, layout, bars, view, domain, overlay, surface, tfMinutes);
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

  if (overlay) drawStrikes(ctx, layout, domain, overlay, draggingStrike);
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
  const pl = positionPl(overlay.legs!, price, tau) * 100;
  const hLabel = hours >= 6.5 ? `+${(hours / 6.5).toFixed(1)}d` : `+${hours.toFixed(1)}h`;
  const txt = `S ${fmtPrice(price)}  ${hLabel}  P/L ${pl >= 0 ? "+" : "-"}$${Math.abs(pl).toFixed(0)}/set`;
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

  // Contours.
  const lineAt = (line: Float64Array, color: string, dash: number[]) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
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
    }
    ctx.stroke();
    ctx.setLineDash([]);
  };
  lineAt(surface.breakevenLine, COLORS.breakeven, [2, 3]);
  lineAt(surface.tpLine, COLORS.tp, [6, 3]);
  lineAt(surface.slLine, COLORS.sl, [6, 3]);

  // Expiry + time-stop verticals.
  const vline = (x: number, color: string, label: string) => {
    if (x < 0 || x > layout.plotW) return;
    ctx.strokeStyle = color;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, layout.plotH);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(label, x, 4);
  };
  vline(xExpiry, COLORS.expiry, "EXPIRY");
  if (overlay.timeStopHours < surface.hoursToExpiry) {
    vline(
      indexToX(futureIndex(overlay.timeStopHours, n, tfMinutes), view, layout),
      COLORS.timeStop,
      "TIME STOP",
    );
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
  overlay.strikes.forEach((strike, i) => {
    const y = priceToY(strike, domain, layout);
    if (y < 0 || y > layout.volTop) return;
    const isShort = overlay.strikeSides[i] < 0;
    const color = isShort ? COLORS.strikeShort : COLORS.strike;
    ctx.strokeStyle = color;
    ctx.lineWidth = draggingStrike === i ? 2.5 : 1.5;
    ctx.setLineDash(isShort ? [8, 4] : []);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(layout.plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    // Label chip on the left.
    const label = `${isShort ? "SHORT " : ""}K ${fmtPrice(strike)} ⇕`;
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    const w = ctx.measureText(label).width + 12;
    ctx.fillRect(6, y - 9, w, 18);
    ctx.strokeStyle = color;
    ctx.strokeRect(6, y - 9, w, 18);
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, 12, y);
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
