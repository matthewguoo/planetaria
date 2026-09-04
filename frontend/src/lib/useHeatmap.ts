/* eslint-disable react-hooks/refs -- legacy chart pattern: props are mirrored
   into refs during render so rAF/canvas draw callbacks read fresh values
   without re-subscribing; predates the rule, behavior verified in prod. */
/**
 * Owns the heatmap web worker. The GRID is computed over the VISIBLE price
 * window (padded + quantized) so vertical resolution follows the zoom — a
 * butterfly's 4-point tent gets dense sampling instead of dissolving into
 * blobs inside a fixed ±4σ window. Contour solves still use the wide
 * structural window so off-screen TP/SL/BE boundaries keep resolving.
 * Quantization means pans only recompute when the view actually shifts a
 * meaningful fraction; the worker round-trip is a few ms.
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
  smiles: Smiles | null;
  volShift: number;
  skewBeta: boolean;
  entryOverride?: number | null;
  /** Visible y-domain (quantized by the chart); null before first draw. */
  viewLo: number | null;
  viewHi: number | null;
  /** Spot move (fraction) that triggers a recompute; default 0.1%. A
   * sub-minute chart passes something finer so the surface follows the
   * tape instead of stepping every $0.65 of SPY. */
  spotQuant?: number;
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
    const { legs, hoursToExpiry, spot, tpPremium, slPremium, smiles, volShift, skewBeta } = inputs;

    // Quantize spot (0.1% by default, finer on fast charts) so quote jitter
    // doesn't thrash the worker; round smile vols to 3dp so chain refreshes
    // only recompute on real moves.
    const quant = inputs.spotQuant ?? 0.001;
    const qSpot = Math.round(spot * 1000) / 1000;
    const smileKey = smiles
      ? [smiles.C, smiles.P].map((pts) => pts.map(([k, v]) => `${k}:${v.toFixed(3)}`).join(","))
      : null;
    const key = JSON.stringify([
      legs.map((l) => [l.right, l.strike, l.side, l.qty, l.entry, l.iv]),
      Math.round(hoursToExpiry * 100),
      Math.round(qSpot / (spot * quant)),
      tpPremium,
      slPremium,
      smileKey,
      volShift,
      skewBeta,
      inputs.entryOverride ?? null,
      inputs.viewLo,
      inputs.viewHi,
    ]);
    if (key === lastKeyRef.current) return;
    lastKeyRef.current = key;

    const sigma = positionIv(legs) || 0.2;
    const tau = Math.max(hoursToExpiry, 0.5) / TRADING_HOURS_PER_YEAR;
    // Wide structural window: ±4σ√τ AND every strike (condor wings must not
    // fall outside the contour-solve bracket).
    const strikeReach = Math.max(...legs.map((l) => Math.abs(l.strike - qSpot)), 0);
    const span = Math.max(
      spot * 4 * sigma * Math.sqrt(tau),
      strikeReach * 1.25,
      spot * 0.004,
    );
    const solveLo = Math.max(qSpot - span, 0.01);
    const solveHi = qSpot + span;
    // Grid window: the visible view padded 30% each side, so vertical
    // resolution tracks the zoom. Falls back to the wide window pre-draw.
    let gridLo = solveLo;
    let gridHi = solveHi;
    const { viewLo, viewHi } = inputs;
    if (viewLo !== null && viewHi !== null && viewHi > viewLo) {
      const pad = (viewHi - viewLo) * 0.3;
      gridLo = Math.max(viewLo - pad, 0.01);
      gridHi = viewHi + pad;
    }
    const request: HeatmapRequest = {
      id: ++seqRef.current,
      legs,
      hoursToExpiry,
      priceLo: gridLo,
      priceHi: gridHi,
      solveLo,
      solveHi,
      priceSteps: 128,
      timeSteps: 64,
      tpPremium,
      slPremium,
      spot0: qSpot,
      smiles,
      volShift,
      skewBeta,
      entryOverride: inputs.entryOverride ?? null,
    };
    workerRef.current?.postMessage(request);
  }, [inputs]);
}
