/**
 * Owns the heatmap web worker. Recomputes the P/L surface when the strategy
 * changes (legs/exits/expiry) or spot drifts — NOT on pan/zoom; the surface
 * covers a fixed ±4σ√τ window around spot and the renderer just remaps it.
 */

import { useEffect, useRef } from "react";
import type { HeatmapRequest, HeatmapResult } from "./heatmap.worker";
import { positionIv, TRADING_HOURS_PER_YEAR, type Leg } from "./optionsMath";

export type SurfaceInputs = {
  legs: Leg[] | null;
  hoursToExpiry: number;
  spot: number;
  tpPremium: number | null;
  slPremium: number | null;
  riskDollars: number;
};

export function useHeatmap(inputs: SurfaceInputs | null, onResult: (r: HeatmapResult) => void) {
  const workerRef = useRef<Worker | null>(null);
  const seqRef = useRef(0);
  const lastKeyRef = useRef("");
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;

  useEffect(() => {
    const worker = new Worker(new URL("./heatmap.worker.ts", import.meta.url), {
      type: "module",
    });
    worker.onmessage = (event: MessageEvent<HeatmapResult>) => {
      if (event.data.id === seqRef.current) onResultRef.current(event.data);
    };
    workerRef.current = worker;
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!inputs || !inputs.legs || !inputs.legs.length || inputs.spot <= 0) return;
    const { legs, hoursToExpiry, spot, tpPremium, slPremium, riskDollars } = inputs;

    // Quantize spot to 0.1% so quote jitter doesn't thrash the worker.
    const qSpot = Math.round(spot * 1000) / 1000;
    const key = JSON.stringify([
      legs.map((l) => [l.right, l.strike, l.side, l.entry, l.iv]),
      Math.round(hoursToExpiry * 100),
      Math.round(qSpot / (spot * 0.001)),
      tpPremium,
      slPremium,
    ]);
    if (key === lastKeyRef.current) return;
    lastKeyRef.current = key;

    const sigma = positionIv(legs) || 0.2;
    const tau = Math.max(hoursToExpiry, 0.5) / TRADING_HOURS_PER_YEAR;
    const span = Math.max(spot * 4 * sigma * Math.sqrt(tau), spot * 0.004);
    const request: HeatmapRequest = {
      id: ++seqRef.current,
      legs,
      hoursToExpiry,
      priceLo: qSpot - span,
      priceHi: qSpot + span,
      priceSteps: 96,
      timeSteps: 48,
      tpPremium,
      slPremium,
      riskDollars,
    };
    workerRef.current?.postMessage(request);
  }, [inputs]);
}
