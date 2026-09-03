/**
 * Terminal WebSocket: single multiplexed connection with auto-reconnect.
 *
 * The server speaks snapshot-then-stream, so reconnect is trivially safe:
 * we simply replay our subscription set and the server pushes fresh
 * snapshots before deltas.
 */

export type WsMessage = {
  t: string;
  [key: string]: unknown;
};

type Handler = (msg: WsMessage) => void;
type SubSpec =
  | { channel: "bars"; symbol: string; tf: string }
  | { channel: "quote"; symbol: string }
  | { channel: "plans" };

// Same-origin by default (dev/preview server proxies /ws), matching api.ts —
// works from any host including phones and tunnels.
const WS_URL =
  (import.meta.env.VITE_WS_URL ??
    `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`) +
  "/ws/stream";

function subKey(spec: SubSpec): string {
  if (spec.channel === "bars") return `bars:${spec.symbol}:${spec.tf}`;
  if (spec.channel === "quote") return `quote:${spec.symbol}`;
  return "plans";
}

class TerminalSocket {
  private ws: WebSocket | null = null;
  private subs = new Map<string, SubSpec>();
  private handlers = new Set<Handler>();
  private retry = 0;
  private closed = false;
  private connState: "connecting" | "open" | "down" = "connecting";
  private stateListeners = new Set<(s: string) => void>();

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  connect(): void {
    if (this.closed) return;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.setState("connecting");
    this.ws = new WebSocket(WS_URL);
    this.ws.onopen = () => {
      this.retry = 0;
      this.setState("open");
      for (const spec of this.subs.values()) this.send({ op: "subscribe", ...spec });
    };
    this.ws.onmessage = (event) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(event.data) as WsMessage;
      } catch {
        return; // one bad frame must not kill the handler chain
      }
      for (const handler of this.handlers) {
        try {
          handler(msg);
        } catch (err) {
          console.error("ws handler failed", err);
        }
      }
    };
    this.ws.onclose = () => {
      this.setState("down");
      if (this.closed) return;
      // Fast, capped backoff: the backend serves snapshots on subscribe so
      // aggressive reconnects are always safe.
      const delay = Math.min(5_000, 400 * 2 ** this.retry++) + Math.random() * 300;
      this.reconnectTimer = setTimeout(() => this.connect(), delay);
    };
    this.ws.onerror = () => this.ws?.close();
  }

  /** Skip any pending backoff and reconnect right now (if down). */
  reconnectNow(): void {
    if (this.closed || this.connState === "open") return;
    this.retry = 0;
    this.connect();
  }

  get state(): string {
    return this.connState;
  }

  private setState(s: "connecting" | "open" | "down"): void {
    this.connState = s;
    for (const listener of this.stateListeners) listener(s);
  }

  onStateChange(fn: (s: string) => void): () => void {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  }

  onMessage(fn: Handler): () => void {
    this.handlers.add(fn);
    return () => this.handlers.delete(fn);
  }

  subscribe(spec: SubSpec): () => void {
    const key = subKey(spec);
    this.subs.set(key, spec);
    this.send({ op: "subscribe", ...spec });
    return () => {
      this.subs.delete(key);
      this.send({ op: "unsubscribe", ...spec });
    };
  }

  private send(payload: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }
}

export const socket = new TerminalSocket();
socket.connect();

// Recover instantly when the network returns or the tab wakes up, instead of
// waiting out the backoff timer.
window.addEventListener("online", () => socket.reconnectNow());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") socket.reconnectNow();
});
