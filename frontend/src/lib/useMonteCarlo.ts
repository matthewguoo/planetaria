/**
 * Owns the MC worker. Debounced + input-keyed so strike/exit drags don't
 * queue a simulation per mousemove; only the latest request's result lands.
 */

import { useEffect, useRef } from "react";
import type { McRequest, McResponse } from "./mc.worker";
import type { McParams } from "./mcSim";
import type { McResult } from "./mcSim";

export type McInputs = Omit<McParams, "paths" | "stepMinutes" | "seed">;

const PATHS = 2000;
const STEP_MINUTES = 5;
const SEED = 1337; // fixed: results stable across identical inputs

export function useMonteCarlo(
  inputs: McInputs | null,
  onResult: (r: McResult | null) => void,
) {
  const workerRef = useRef<Worker | null>(null);
  const seqRef = useRef(0);
  const lastKeyRef = useRef("");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;

  useEffect(() => {
    const worker = new Worker(new URL("./mc.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (event: MessageEvent<McResponse>) => {
      if (event.data.id === seqRef.current) {
        const { id: _id, ...result } = event.data;
        onResultRef.current(result);
      }
    };
    workerRef.current = worker;
    // Same StrictMode guard as useHeatmap: a new worker needs a re-post.
    lastKeyRef.current = "";
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!inputs || !inputs.legs.length || inputs.spot <= 0) {
      onResultRef.current(null);
      lastKeyRef.current = "";
      return;
    }
    const key = JSON.stringify([
      inputs.legs.map((l) => [l.right, l.strike, l.side, l.qty, l.entry.toFixed(3), l.iv.toFixed(3)]),
      inputs.entry.toFixed(3),
      inputs.tpPremium?.toFixed(3) ?? null,
      inputs.slPremium?.toFixed(3) ?? null,
      Math.round(inputs.hoursToExpiry * 60),
      Math.round(inputs.timeStopHours * 12) / 12,
      Math.round(Math.log(inputs.spot) * 500), // ~0.2% spot buckets
      inputs.volShift,
      inputs.skewBeta,
      Math.round(inputs.frictionPerSet),
    ]);
    // Only reschedule when the QUANTIZED inputs changed. inputs' object
    // identity changes on every quote tick; clearing the pending timer in an
    // effect cleanup would keep resetting the debounce and the request would
    // never fire.
    if (key === lastKeyRef.current) return;
    lastKeyRef.current = key;

    const id = ++seqRef.current;
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      if (id !== seqRef.current) return;
      const request: McRequest = {
        id,
        ...inputs,
        paths: PATHS,
        stepMinutes: STEP_MINUTES,
        seed: SEED,
      };
      workerRef.current?.postMessage(request);
    }, 250);
  }, [inputs]);
}
