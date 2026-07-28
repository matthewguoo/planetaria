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
  | { channel: "plans" }
  | { channel: "oquotes"; symbols: string[] };

const WS_URL = (import.meta.env.VITE_WS_URL ?? "ws://localhost:8000") + "/ws/stream";

function subKey(spec: SubSpec): string {
  if (spec.channel === "bars") return `bars:${spec.symbol}:${spec.tf}`;
  if (spec.channel === "quote") return `quote:${spec.symbol}`;
  if (spec.channel === "plans") return "plans";
  return `oquotes:${spec.symbols.slice().sort().join(",")}`;
}

class TerminalSocket {
  private ws: WebSocket | null = null;
  private subs = new Map<string, SubSpec>();
  private handlers = new Set<Handler>();
  private retry = 0;
  private closed = false;
  private connState: "connecting" | "open" | "down" = "connecting";
  private stateListeners = new Set<(s: string) => void>();

  connect(): void {
    if (this.closed) return;
    this.setState("connecting");
    this.ws = new WebSocket(WS_URL);
    this.ws.onopen = () => {
      this.retry = 0;
      this.setState("open");
      for (const spec of this.subs.values()) this.send({ op: "subscribe", ...spec });
    };
    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data) as WsMessage;
      for (const handler of this.handlers) handler(msg);
    };
    this.ws.onclose = () => {
      this.setState("down");
      if (this.closed) return;
      const delay = Math.min(15_000, 500 * 2 ** this.retry++) + Math.random() * 500;
      setTimeout(() => this.connect(), delay);
    };
    this.ws.onerror = () => this.ws?.close();
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
