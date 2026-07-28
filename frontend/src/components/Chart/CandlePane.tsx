import { useCallback, useEffect, useRef } from "react";
import { barsTable } from "../../lib/perspective";
import { focusFeed, onSnapshot } from "../../lib/barFeed";
import { useTradingStore } from "../../store/tradingStore";
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

/** Future pad (fraction of visible width) kept clear right of the last bar. */
const RIGHT_PAD_FRAC = 0.08;

export function CandlePane() {
  const symbol = useTradingStore((s) => s.symbol);
  const tf = useTradingStore((s) => s.tf);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const barsRef = useRef<Bars>(EMPTY);
  const viewRef = useRef<ViewState>({ rightIndex: 0, barsVisible: 120, follow: true });
  const mouseRef = useRef<{ x: number; y: number } | null>(null);
  const dragRef = useRef<{ startX: number; startRight: number } | null>(null);
  const rafRef = useRef(0);

  const draw = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      const canvas = canvasRef.current;
      const wrap = wrapRef.current;
      if (!canvas || !wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const cssW = wrap.clientWidth;
      const cssH = wrap.clientHeight;
      if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
        canvas.width = cssW * dpr;
        canvas.height = cssH * dpr;
        canvas.style.width = `${cssW}px`;
        canvas.style.height = `${cssH}px`;
      }
      const ctx = canvas.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      render(ctx, computeLayout(cssW, cssH), barsRef.current, viewRef.current, mouseRef.current);
    });
  }, []);

  // Data plumbing: perspective view of the focused (symbol, tf) table.
  useEffect(() => {
    let disposed = false;
    let view: { delete(): Promise<void>; on_update(cb: () => void): void; to_columns(): Promise<Record<string, unknown[]>> } | null = null;

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
      const view_ = viewRef.current;
      if (view_.follow) {
        view_.rightIndex = Math.max(0, n - 1) + view_.barsVisible * RIGHT_PAD_FRAC;
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

  // Resize.
  useEffect(() => {
    const observer = new ResizeObserver(draw);
    if (wrapRef.current) observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [draw]);

  // Interactions.
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      const view = viewRef.current;
      const layout = computeLayout(wrapRef.current!.clientWidth, wrapRef.current!.clientHeight);
      const rect = canvasRef.current!.getBoundingClientRect();
      const anchor = xToIndex(e.clientX - rect.left, view, layout);
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      const next = Math.max(20, Math.min(3000, view.barsVisible * factor));
      // Keep the bar under the cursor stationary while zooming.
      const frac = (view.rightIndex - anchor) / view.barsVisible;
      view.barsVisible = next;
      view.rightIndex = anchor + frac * next;
      view.follow = false;
      draw();
    },
    [draw],
  );

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    dragRef.current = { startX: e.clientX, startRight: viewRef.current.rightIndex };
  }, []);

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      mouseRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      const drag = dragRef.current;
      if (drag) {
        const layout = computeLayout(wrapRef.current!.clientWidth, wrapRef.current!.clientHeight);
        const barW = layout.plotW / viewRef.current.barsVisible;
        viewRef.current.rightIndex = drag.startRight - (e.clientX - drag.startX) / barW;
        viewRef.current.follow = false;
      }
      draw();
    },
    [draw],
  );

  const onMouseUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  const onMouseLeave = useCallback(() => {
    mouseRef.current = null;
    dragRef.current = null;
    draw();
  }, [draw]);

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
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseLeave}
        onDoubleClick={onDoubleClick}
      />
    </div>
  );
}

// ---------------------------------------------------------------- rendering

function render(
  ctx: CanvasRenderingContext2D,
  layout: Layout,
  bars: Bars,
  view: ViewState,
  mouse: { x: number; y: number } | null,
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

  const domain = extendDomain(priceDomain(bars, view), []);
  const [first, last] = visibleRange(bars, view);
  const barW = layout.plotW / view.barsVisible;
  const bodyW = Math.max(1, Math.min(barW * 0.7, 14));

  drawPriceGrid(ctx, layout, domain);
  drawTimeAxis(ctx, layout, bars, view, first, last);
  drawVolume(ctx, layout, bars, view, first, last);

  // Candles.
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
    // Wick
    ctx.beginPath();
    ctx.moveTo(x, yH);
    ctx.lineTo(x, yL);
    ctx.stroke();
    // Body
    const top = Math.min(yO, yC);
    const height = Math.max(1, Math.abs(yC - yO));
    ctx.fillRect(x - bodyW / 2, top, bodyW, height);
  }

  drawLastPrice(ctx, layout, bars, domain);
  if (mouse && mouse.x <= layout.plotW && mouse.y <= layout.plotH) {
    drawCrosshair(ctx, layout, bars, view, domain, mouse);
  }
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

  // Price tag.
  const price = yToPrice(mouse.y, domain, layout);
  ctx.fillStyle = "#222222";
  ctx.fillRect(layout.plotW, mouse.y - 8, layout.axisW, 16);
  ctx.fillStyle = "#ffffff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(fmtPrice(price), layout.plotW + 6, mouse.y);

  // OHLCV legend for the hovered bar.
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
    ctx.fillRect(6, 6, 420, 18);
    ctx.fillStyle = bars.c[index] >= bars.o[index] ? COLORS.up : COLORS.down;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(parts.join("   "), 12, 15);
  }
}
