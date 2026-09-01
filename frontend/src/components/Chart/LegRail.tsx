/**
 * Leg rail: a permanent vertical strip on the chart's LEFT edge with one
 * square pill per leg. Pills sit at their strike's y position on the live
 * chart scale (published by CandlePane), drag vertically with snap-to-listed-
 * strike, and hover to a tastytrade-style stats tooltip for that contract.
 * HTML (not canvas) so tooltips, cursors, and touch come free — and nothing
 * here can occlude the canvas interactions: the strip is only 44px wide.
 */

import { useCallback, useRef, useState, useSyncExternalStore } from "react";
import {
  getScale,
  getScaleVersion,
  priceToRailY,
  railYToPrice,
  subscribeScale,
} from "../../lib/chartShared";
import type { Designer } from "../../lib/useDesigner";
import {
  availableStrikes,
  findContract,
  nearestStrike,
  useStrategyStore,
} from "../../store/strategyStore";
import { useUiStore } from "../../store/uiStore";

const PILL_H = 18;

function fmtPct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

function Tooltip({ designer, i }: { designer: Designer; i: number }) {
  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const strikes = useStrategyStore((s) => s.strikes);
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const ratios = useStrategyStore((s) => s.ratios);

  const strike = strikes[i];
  const right = rights[i];
  const contract =
    chain && expiry && strike !== undefined ? findContract(chain, expiry, right, strike) : null;
  const spot = designer.spot;
  const rows: [string, string, string?][] = [];
  if (contract) {
    rows.push(["BID × ASK", `${contract.bid.toFixed(2)} × ${contract.ask.toFixed(2)}`]);
    rows.push(["MID", contract.mid.toFixed(2)]);
    rows.push([
      "IV",
      `${fmtPct(contract.iv)}${contract.iv_source && contract.iv_source !== "feed" ? ` (${contract.iv_source})` : ""}`,
    ]);
    if (contract.delta !== null) rows.push(["DELTA", contract.delta.toFixed(3)]);
    const spread = contract.ask - contract.bid;
    if (contract.mid > 0 && spread > 0) {
      const sprPct = spread / contract.mid;
      rows.push(["SPREAD", `${spread.toFixed(2)} (${fmtPct(sprPct)})`, sprPct > 0.1 ? "text-bb-orange" : undefined]);
    }
    if (spot > 0 && strike !== undefined) {
      const moneyness = ((strike - spot) / spot) * 100;
      const itm = right === "C" ? spot > strike : spot < strike;
      rows.push([
        "VS SPOT",
        `${moneyness >= 0 ? "+" : ""}${moneyness.toFixed(2)}% ${itm ? "ITM" : "OTM"}`,
      ]);
    }
  }

  return (
    <div
      className="pointer-events-none absolute left-11 z-40 w-44 border border-bb-border bg-black/95 px-2 py-1.5 text-[10px]"
      style={{ top: -8 }}
    >
      <div className="mb-1 flex justify-between font-semibold">
        <span className={sides[i] > 0 ? "text-bb-amber" : "text-bb-orange"}>
          {sides[i] > 0 ? "LONG" : "SHORT"} {ratios[i] ?? 1}× {strike} {right}
        </span>
      </div>
      {contract ? (
        rows.map(([label, value, cls]) => (
          <div key={label} className="flex justify-between gap-2">
            <span className="text-bb-muted">{label}</span>
            <span data-numeric className={cls ?? "text-white"}>
              {value}
            </span>
          </div>
        ))
      ) : (
        <div className="text-bb-muted">no quote for this contract</div>
      )}
    </div>
  );
}

export function LegRail({ designer }: { designer: Designer }) {
  useSyncExternalStore(subscribeScale, getScaleVersion);
  const scale = getScale();

  const chain = useStrategyStore((s) => s.chain);
  const expiry = useStrategyStore((s) => s.expiry);
  const strikes = useStrategyStore((s) => s.strikes);
  const rights = useStrategyStore((s) => s.rights);
  const sides = useStrategyStore((s) => s.sides);
  const ratios = useStrategyStore((s) => s.ratios);
  const setStrike = useStrategyStore((s) => s.setStrike);
  const viewingPlanId = useUiStore((s) => s.viewingPlanId);

  const railRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);

  const onPointerMove = useCallback(
    (e: React.PointerEvent, i: number) => {
      if (dragging !== i) return;
      const rail = railRef.current;
      const s = getScale();
      if (!rail || !s) return;
      const y = e.clientY - rail.getBoundingClientRect().top;
      const price = railYToPrice(Math.max(0, Math.min(y, s.volTopPx)), s);
      const snaps = availableStrikes(chain, expiry);
      if (!snaps.length) return;
      const snapped = nearestStrike(snaps, price);
      if (snapped !== strikes[i]) setStrike(i, snapped);
    },
    [dragging, chain, expiry, strikes, setStrike],
  );

  // Hidden in read-only position view, or with nothing to show.
  if (viewingPlanId || !scale || !strikes.length || !chain || !expiry) return null;

  return (
    <div
      ref={railRef}
      className="absolute inset-y-0 left-0 z-20 w-11"
      style={{ height: scale.volTopPx }}
    >
      {/* track */}
      <div className="absolute bottom-0 left-[13px] top-0 w-px bg-bb-border/60" />
      {strikes.map((strike, i) => {
        if (strike === undefined) return null;
        const y = priceToRailY(strike, scale);
        if (y < -PILL_H || y > scale.volTopPx + PILL_H) return null;
        const short = sides[i] < 0;
        return (
          <div
            key={i}
            className="absolute left-0.5"
            style={{ top: y - PILL_H / 2 }}
            onMouseEnter={() => setHovered(i)}
            onMouseLeave={() => setHovered((h) => (h === i ? null : h))}
          >
            <button
              className={
                "touch-none select-none border px-1 text-[9px] font-semibold leading-[16px] " +
                (short
                  ? "border-bb-orange bg-black text-bb-orange"
                  : "border-bb-amber bg-black text-bb-amber") +
                (dragging === i ? " bg-bb-hover" : "")
              }
              style={{ cursor: "ns-resize", height: PILL_H }}
              title={`${short ? "SHORT" : "LONG"} ${ratios[i] ?? 1}× ${strike} ${rights[i]} — drag to move strike`}
              onPointerDown={(e) => {
                setDragging(i);
                try {
                  (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                } catch {
                  /* synthetic events */
                }
              }}
              onPointerMove={(e) => onPointerMove(e, i)}
              onPointerUp={() => setDragging(null)}
              onPointerCancel={() => setDragging(null)}
              onLostPointerCapture={() => setDragging(null)}
              aria-label={`Leg ${i + 1}: drag to change strike`}
            >
              {sides[i] > 0 ? "+" : "−"}
              {ratios[i] ?? 1}
              {rights[i]}
            </button>
            {hovered === i && dragging === null && <Tooltip designer={designer} i={i} />}
          </div>
        );
      })}
    </div>
  );
}
