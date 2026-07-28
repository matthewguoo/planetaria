/**
 * Owns the heatmap web worker. Recomputes the P/L surface when the strategy
 * changes (legs/exits/expiry) or spot drifts — NOT on pan/zoom; the surface
 * covers a fixed ±4σ√τ window around spot and the renderer just remaps it.
 */

import { useEffect, useRef } from "react";
import type { HeatmapRequest, HeatmapResult } from "./heatmap.worker";
import { positionIv, TRADING_HOURS_PER_YEAR, type Leg, type Smiles } from "./optionsMath";

export type SurfaceInputs = {
  legs: Leg[] | null;
  hoursToExpiry: number;
  spot: number;
  tpPremium: number | null;
  slPremium: number | null;
  riskDollars: number;
  smiles: Smiles | null;
  volShift: number;
  skewBeta: boolean;
  entryOverride?: number | null;
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
    // A fresh worker has no pending request. Reset the input-dedupe key so
    // the next inputs effect re-posts — otherwise StrictMode's effect replay
    // (terminate worker A, create worker B) leaves B idle forever because
    // the key ref says the request was "already sent" (to the dead worker).
    lastKeyRef.current = "";
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!inputs || !inputs.legs || !inputs.legs.length || inputs.spot <= 0) return;
    const { legs, hoursToExpiry, spot, tpPremium, slPremium, riskDollars, smiles, volShift, skewBeta } = inputs;

    // Quantize spot to 0.1% so quote jitter doesn't thrash the worker; round
    // smile vols to 3dp so 10s chain refreshes only recompute on real moves.
    const qSpot = Math.round(spot * 1000) / 1000;
    const smileKey = smiles
      ? [smiles.C, smiles.P].map((pts) => pts.map(([k, v]) => `${k}:${v.toFixed(3)}`).join(","))
      : null;
    const key = JSON.stringify([
      legs.map((l) => [l.right, l.strike, l.side, l.qty, l.entry, l.iv]),
      Math.round(hoursToExpiry * 100),
      Math.round(qSpot / (spot * 0.001)),
      tpPremium,
      slPremium,
      smileKey,
      volShift,
      skewBeta,
      inputs.entryOverride ?? null,
    ]);
    if (key === lastKeyRef.current) return;
    lastKeyRef.current = key;

    const sigma = positionIv(legs) || 0.2;
    const tau = Math.max(hoursToExpiry, 0.5) / TRADING_HOURS_PER_YEAR;
    // Cover ±4σ√τ AND every strike (wide condor wings must not fall off the
    // surface), with a little slack past the farthest strike.
    const strikeReach = Math.max(...legs.map((l) => Math.abs(l.strike - qSpot)), 0);
    const span = Math.max(
      spot * 4 * sigma * Math.sqrt(tau),
      strikeReach * 1.25,
      spot * 0.004,
    );
    const request: HeatmapRequest = {
      id: ++seqRef.current,
      legs,
      hoursToExpiry,
      priceLo: Math.max(qSpot - span, 0.01),
      priceHi: qSpot + span,
      priceSteps: 128,
      timeSteps: 64,
      tpPremium,
      slPremium,
      riskDollars,
      spot0: qSpot,
      smiles,
      volShift,
      skewBeta,
      entryOverride: inputs.entryOverride ?? null,
    };
    workerRef.current?.postMessage(request);
  }, [inputs]);
}
