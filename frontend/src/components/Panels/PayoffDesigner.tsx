/**
 * Interactive payoff diagram: x = underlying price, y = P/L per contract-set.
 * Each leg is a draggable VERTICAL RAIL at its strike — drag horizontally to
 * re-strike (snapped to the chain's strike grid), click the −/+ zones on a
 * rail's chip to change that leg's contract ratio. Every edit flows through
 * the strategy store, so the on-chart P/L heatmap updates live.
 */

import { useCallback, useEffect, useRef } from "react";
import {
  payoffAtExpiry,
  positionEntryCost,
  positionPl,
  TRADING_HOURS_PER_YEAR,
} from "../../lib/optionsMath";
import type { Designer } from "../../lib/useDesigner";
import {
  availableStrikes,
  strategyDef,
  useStrategyStore,
} from "../../store/strategyStore";

const RAIL_HIT_PX = 7;
const CHIP_H = 15;

const C = {
  long: "#FFB000",
  short: "#FF6D00",
  expiry: "#FFB000",
  now: "#2196F3",
  zero: "#333333",
  spot: "#ffffff",
  profit: "rgba(0,200,83,0.12)",
  loss: "rgba(255,23,68,0.12)",
  text: "#999999",
};

type ChipZones = { minus: [number, number, number, number]; plus: [number, number, number, number]; drag: [number, number, number, number] };

export function PayoffDesigner({ designer }: { designer: Designer }) {
  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const kind = useStrategyStore((s) => s.kind);
  const strikes = useStrategyStore((s) => s.strikes);
  const ratios = useStrategyStore((s) => s.ratios);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const setRatio = useStrategyStore((s) => s.setRatio);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<number | null>(null);
  const hoverRef = useRef<{ x: number; y: number } | null>(null);
  const chipZonesRef = useRef<Map<number, ChipZones>>(new Map());
  const domainRef = useRef<[number, number]>([0, 1]);

  const snaps = availableStrikes(chain, expiry);
  const template = strategyDef(kind).legs;
  const spot = designer.spot || chain?.spot || 0;

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
    }
    const ctx = canvas.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = "10px 'SF Mono', Consolas, monospace";
    chipZonesRef.current.clear();

    if (!strikes.length || spot <= 0) {
      ctx.fillStyle = C.text;
      ctx.textAlign = "center";
      ctx.fillText("no strategy", w / 2, h / 2);
      return;
    }

    // Price domain around strikes + spot.
    const anchors = [...strikes, spot];
    let lo = Math.min(...anchors);
    let hi = Math.max(...anchors);
    const pad = Math.max((hi - lo) * 0.45, spot * 0.012);
    lo -= pad;
    hi += pad;
    domainRef.current = [lo, hi];
    const xOf = (p: number) => ((p - lo) / (hi - lo)) * w;

    const legs = designer.legs;
    const n = 140;
    let yOf: (v: number) => number = () => h / 2;
    if (legs) {
      const tau = designer.hoursToExpiry / TRADING_HOURS_PER_YEAR;
      const expiryPl: number[] = [];
      const nowPl: number[] = [];
      for (let i = 0; i <= n; i++) {
        const s = lo + ((hi - lo) * i) / n;
        expiryPl.push(payoffAtExpiry(legs, s) * 100);
        nowPl.push(positionPl(legs, s, tau) * 100);
      }
      const all = [...expiryPl, ...nowPl];
      const minV = Math.min(...all);
      const maxV = Math.max(...all);
      const vpad = (maxV - minV || 1) * 0.12;
      yOf = (v: number) =>
        h - CHIP_H - ((v - minV + vpad) / (maxV - minV + 2 * vpad)) * (h - CHIP_H - 4) - 0;
      const xStep = w / n;

      // Profit/loss fill under expiry curve.
      const y0 = yOf(0);
      for (let i = 0; i < n; i++) {
        const v = expiryPl[i];
        const y1 = yOf(v);
        ctx.fillStyle = v >= 0 ? C.profit : C.loss;
        ctx.fillRect(i * xStep, Math.min(y0, y1), xStep + 0.5, Math.abs(y1 - y0));
      }
      // Zero line.
      ctx.strokeStyle = C.zero;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(0, y0);
      ctx.lineTo(w, y0);
      ctx.stroke();
      ctx.setLineDash([]);

      const strokeCurve = (values: number[], color: string, dash: number[]) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.setLineDash(dash);
        ctx.beginPath();
        values.forEach((v, i) => (i === 0 ? ctx.moveTo(i * xStep, yOf(v)) : ctx.lineTo(i * xStep, yOf(v))));
        ctx.stroke();
        ctx.setLineDash([]);
      };
      strokeCurve(nowPl, C.now, [4, 3]);
      strokeCurve(expiryPl, C.expiry, []);
    }

    // Spot marker.
    const spotX = xOf(spot);
    ctx.strokeStyle = C.spot;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(spotX, 0);
    ctx.lineTo(spotX, h - CHIP_H);
    ctx.stroke();
    ctx.setLineDash([]);

    // Vertical rails, one per leg. Chips live in a bottom band, staggered so
    // same-strike legs (straddles) stay individually grabbable.
    strikes.forEach((strike, i) => {
      const t = template[i];
      const x = xOf(strike);
      const color = t.side > 0 ? C.long : C.short;
      const active = dragRef.current === i;
      ctx.strokeStyle = color;
      ctx.lineWidth = active ? 2.5 : 1.5;
      ctx.setLineDash(t.side < 0 ? [5, 3] : []);
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h - CHIP_H);
      ctx.stroke();
      ctx.setLineDash([]);

      // Chip: [−] label [+]
      const ratio = ratios[i] ?? t.ratio;
      const label = `${t.side > 0 ? "+" : "−"}${ratio}${t.right} ${strike}`;
      const labelW = ctx.measureText(label).width;
      const zone = 13;
      const chipW = labelW + zone * 2 + 10;
      const rowY = h - CHIP_H;
      let cx = Math.min(Math.max(x - chipW / 2, 1), w - chipW - 1);
      // Stagger duplicate columns horizontally.
      for (let j = 0; j < i; j++) {
        const prev = chipZonesRef.current.get(j);
        if (prev && Math.abs(prev.drag[0] - cx) < chipW) cx = Math.min(prev.drag[0] + prev.drag[2] + zone * 2 + 4, w - chipW - 1);
      }
      ctx.fillStyle = "rgba(0,0,0,0.9)";
      ctx.fillRect(cx, rowY, chipW, CHIP_H - 1);
      ctx.strokeStyle = color;
      ctx.lineWidth = active ? 1.6 : 0.8;
      ctx.strokeRect(cx, rowY, chipW, CHIP_H - 1);
      ctx.fillStyle = C.text;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("−", cx + zone / 2 + 1, rowY + CHIP_H / 2);
      ctx.fillText("+", cx + chipW - zone / 2 - 1, rowY + CHIP_H / 2);
      ctx.fillStyle = color;
      ctx.fillText(label, cx + chipW / 2, rowY + CHIP_H / 2);
      chipZonesRef.current.set(i, {
        minus: [cx, rowY, zone, CHIP_H],
        plus: [cx + chipW - zone, rowY, zone, CHIP_H],
        drag: [cx + zone, rowY, chipW - zone * 2, CHIP_H],
      });
    });

    // Hover readout.
    const hover = hoverRef.current;
    if (hover && legs) {
      const price = lo + (hover.x / w) * (hi - lo);
      const tau = designer.hoursToExpiry / TRADING_HOURS_PER_YEAR;
      const atExp = payoffAtExpiry(legs, price) * 100;
      const atNow = positionPl(legs, price, tau) * 100;
      const txt = `S ${price.toFixed(2)}  exp ${atExp >= 0 ? "+" : ""}$${atExp.toFixed(0)}  now ${atNow >= 0 ? "+" : ""}$${atNow.toFixed(0)}`;
      ctx.fillStyle = "rgba(0,0,0,0.85)";
      const tw = ctx.measureText(txt).width + 10;
      ctx.fillRect(w - tw - 4, 2, tw, 14);
      ctx.fillStyle = "#ffffff";
      ctx.textAlign = "left";
      ctx.fillText(txt, w - tw + 1, 9);
    }

    // Entry cost badge (DEBIT/CREDIT).
    if (legs) {
      const entry = positionEntryCost(legs);
      const tag = entry >= 0 ? `DEBIT ${entry.toFixed(2)}` : `CREDIT ${(-entry).toFixed(2)}`;
      ctx.fillStyle = entry >= 0 ? C.long : "#00C853";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(tag, 4, 3);
    }
  }, [strikes, ratios, template, designer, spot]);

  useEffect(() => {
    draw();
  }, [draw]);

  useEffect(() => {
    const observer = new ResizeObserver(draw);
    if (wrapRef.current) observer.observe(wrapRef.current);
    return () => observer.disconnect();
  }, [draw]);

  const railAt = useCallback(
    (x: number, y: number): { index: number; zone: "minus" | "plus" | "drag" | "rail" } | null => {
      // Chip zones take priority (they're the only place same-strike legs differ).
      for (const [i, zones] of chipZonesRef.current) {
        for (const zone of ["minus", "plus", "drag"] as const) {
          const [zx, zy, zw, zh] = zones[zone];
          if (x >= zx && x <= zx + zw && y >= zy && y <= zy + zh) return { index: i, zone };
        }
      }
      const wrap = wrapRef.current;
      if (!wrap) return null;
      const [lo, hi] = domainRef.current;
      const w = wrap.clientWidth;
      let best: number | null = null;
      let bestDist = RAIL_HIT_PX + 1;
      strikes.forEach((strike, i) => {
        const dist = Math.abs(((strike - lo) / (hi - lo)) * w - x);
        if (dist < bestDist) {
          best = i;
          bestDist = dist;
        }
      });
      return best === null ? null : { index: best, zone: "rail" };
    },
    [strikes],
  );

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const hit = railAt(e.clientX - rect.left, e.clientY - rect.top);
      if (!hit) return;
      if (hit.zone === "minus") {
        setRatio(hit.index, (ratios[hit.index] ?? 1) - 1);
      } else if (hit.zone === "plus") {
        setRatio(hit.index, (ratios[hit.index] ?? 1) + 1);
      } else {
        dragRef.current = hit.index;
      }
    },
    [railAt, ratios, setRatio],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const rect = canvasRef.current!.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      hoverRef.current = { x, y };
      const wrap = wrapRef.current!;
      if (dragRef.current !== null && snaps.length) {
        const [lo, hi] = domainRef.current;
        const price = lo + (x / wrap.clientWidth) * (hi - lo);
        const snapped = snaps.reduce(
          (best, s) => (Math.abs(s - price) < Math.abs(best - price) ? s : best),
          snaps[0],
        );
        if (snapped !== strikes[dragRef.current]) setStrike(dragRef.current, snapped);
      } else {
        const hit = railAt(x, y);
        const cursor = !hit ? "crosshair" : hit.zone === "minus" || hit.zone === "plus" ? "pointer" : "col-resize";
        if (canvasRef.current!.style.cursor !== cursor) canvasRef.current!.style.cursor = cursor;
      }
      draw();
    },
    [draw, railAt, snaps, strikes, setStrike],
  );

  const endInteraction = useCallback(() => {
    dragRef.current = null;
    draw();
  }, [draw]);

  const onMouseLeave = useCallback(() => {
    hoverRef.current = null;
    endInteraction();
  }, [endInteraction]);

  return (
    <div ref={wrapRef} className="relative h-full w-full overflow-hidden">
      <canvas
        ref={canvasRef}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={endInteraction}
        onMouseLeave={onMouseLeave}
      />
    </div>
  );
}
