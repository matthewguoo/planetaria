/** Monte Carlo exit simulation off the main thread. */

import { simulateExits, type McParams, type McResult } from "./mcSim";

export type McRequest = McParams & { id: number };
export type McResponse = McResult & { id: number };

self.onmessage = (event: MessageEvent<McRequest>) => {
  const { id, ...params } = event.data;
  const result = simulateExits(params);
  (self as unknown as Worker).postMessage({ id, ...result } satisfies McResponse);
};
