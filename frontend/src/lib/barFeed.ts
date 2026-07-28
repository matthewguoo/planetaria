/**
 * Bridges the terminal WebSocket into Perspective tables + the Zustand store.
 * One active (symbol, tf) bar subscription + one quote subscription at a time.
 */

import { replaceBars, upsertBar, type BarRow } from "./perspective";
import { socket, type WsMessage } from "./ws";
import { useTradingStore, type Quote, type Timeframe } from "../store/tradingStore";

let unsubBars: (() => void) | null = null;
let unsubQuote: (() => void) | null = null;
let current = { symbol: "", tf: "" };

/** Version counter lets the chart know when a full snapshot replaced the table. */
let snapshotVersion = 0;
const snapshotListeners = new Set<(v: number) => void>();

export function onSnapshot(fn: (v: number) => void): () => void {
  snapshotListeners.add(fn);
  return () => snapshotListeners.delete(fn);
}

// Keep position rows live: plan lifecycle pushes from the server.
socket.subscribe({ channel: "plans" });

socket.onMessage((msg: WsMessage) => {
  const store = useTradingStore.getState();
  switch (msg.t) {
    case "plan": {
      void import("../store/accountStore").then(({ useAccountStore }) => {
        useAccountStore.getState().applyPlanUpdate(msg.plan as never);
        void useAccountStore.getState().refreshPositions();
      });
      break;
    }
    case "plans_snapshot": {
      void import("../store/accountStore").then(({ useAccountStore }) => {
        void useAccountStore.getState().refreshPositions();
      });
      break;
    }
    case "bars_snapshot": {
      if (msg.symbol !== current.symbol || msg.tf !== current.tf) return;
      void replaceBars(msg.symbol as string, msg.tf as string, msg.bars as BarRow[]).then(() => {
        snapshotVersion += 1;
        for (const fn of snapshotListeners) fn(snapshotVersion);
      });
      break;
    }
    case "bar": {
      void upsertBar(msg.symbol as string, msg.tf as string, msg.bar as BarRow);
      break;
    }
    case "quote": {
      if (msg.symbol === store.symbol) store.setQuote(msg as unknown as Quote);
      break;
    }
    case "status": {
      store.patchStatus({
        configured: msg.configured as boolean,
        demo: (msg.demo as boolean) ?? false,
        redis: msg.redis as boolean,
        stream_age_s: msg.stream_age_s as number | null,
      });
      break;
    }
  }
});

socket.onStateChange((s) => {
  useTradingStore.getState().patchStatus({ connection: s as "connecting" | "open" | "down" });
});

/** Point the feed at a (symbol, tf). Idempotent; swaps subscriptions. */
export function focusFeed(symbol: string, tf: Timeframe): void {
  if (current.symbol === symbol && current.tf === tf) return;
  unsubBars?.();
  if (current.symbol !== symbol) {
    unsubQuote?.();
    unsubQuote = socket.subscribe({ channel: "quote", symbol });
  }
  current = { symbol, tf };
  unsubBars = socket.subscribe({ channel: "bars", symbol, tf });
}
