import { api } from "../lib/api";

/** One journaled LLM call (signals row, type="analysis"). */
export type Analysis = {
  id: number;
  ts: string;
  source: string;
  symbols: string[];
  payload: {
    task?: string;
    model?: string;
    latency_ms?: number | null;
    result?: Record<string, unknown> | null;
    error?: string | null;
  };
};

export type Verdict = {
  eps_vs_consensus?: string;
  revenue_vs_consensus?: string;
  guidance?: string;
  quality_flags?: string[];
  direction?: "bullish" | "bearish" | "neutral";
  confidence?: "low" | "medium" | "high";
  summary?: string;
};

/** The strategy's own record of one release. `would_trade` in shadow mode,
 * `decision` in live mode — identical shape on purpose. */
export type DecisionBody = {
  symbol: string;
  side?: 1 | -1;
  verdict?: Verdict;
  source?: string;
  px0?: number;
  px?: number;
  move_pct?: number;
  day_move_pct?: number | null;
  run5d_pct?: number | null;
  spread_pct?: number | null;
  bracket?: { sl_pct: number; tp_pct: number; avg_abs_ret_pct?: number | null };
  sizing?: Record<string, number>;
  model?: string;
  latency_ms?: number;
  intent?: Record<string, unknown>;
};

export type Decision = {
  id: number;
  ts: string;
  strategy_id: string;
  action: string;
  plan_id: string | null;
  signal_ids: number[] | null;
  detail: Record<string, unknown> | null;
};

export type Instance = {
  id: string;
  kind: string;
  name: string;
  state: string;
  params: Record<string, unknown>;
  runtime?: { task: string; queue_size?: number; events_seen?: number; errors?: number };
};

export type LabPlan = {
  id: string;
  underlying: string;
  status: string;
  qty: number;
  entry_limit: number;
  fill_premium: number | null;
  exit_premium: number | null;
  realized_pnl: number | null;
  exit_reason: string | null;
  created_at: string;
  exec_quality?: Record<string, { fair?: number; cost?: number; spread_capture?: number }> | null;
};

export type Stream = {
  instances: Instance[];
  decisions: Decision[];
  analyses: Analysis[];
  plans: LabPlan[];
  marks: Record<string, number>;
  llm: { backend: string; model: string; configured: boolean };
  feed: { stock: string };
  cursor: { decision: number; signal: number };
};

export type SimTrade = {
  symbol: string;
  side: number;
  opened_at: string;
  entry: number;
  qty: number;
  notional: number;
  sl_pct: number;
  tp_pct: number;
  exit: number | null;
  exit_reason: string | null;
  exited_at: string | null;
  pnl: number | null;
  mark: number | null;
  unrealized: number | null;
  verdict?: Verdict;
  move_pct?: number | null;
  run5d_pct?: number | null;
};

export type SimPolicy = {
  policy: "vol" | "flat";
  start_equity: number;
  equity: number;
  realized: number;
  unrealized: number;
  return_pct: number;
  max_drawdown: number;
  trades: number;
  open: number;
  closed: number;
  win_rate: number | null;
  exposure: number;
  curve: { ts: string | null; equity: number }[];
  positions: SimTrade[];
};

export type Sim = {
  instances: string[];
  signals: number;
  params: { equity: number; risk_pct: number; flat_pct: number };
  policies: SimPolicy[];
};

export async function fetchStream(cursor: { decision: number; signal: number }): Promise<Stream> {
  const { data } = await api.get<Stream>("/api/lab/stream", {
    params: { since_decision: cursor.decision, since_signal: cursor.signal },
  });
  return data;
}

export async function fetchSim(equity: number, flatPct: number): Promise<Sim> {
  const { data } = await api.get<Sim>("/api/lab/sim", {
    params: { equity, flat_pct: flatPct },
  });
  return data;
}

/** The body a decision row carries, whatever mode produced it. `queued` is
 * included so a parked release still shows its ticker on the tape. */
export function bodyOf(d: Decision): DecisionBody | null {
  const detail = (d.detail ?? {}) as Record<string, unknown>;
  for (const key of ["would_trade", "decision", "veto", "no_trade", "queued"]) {
    const body = detail[key];
    if (body && typeof body === "object" && "symbol" in body) return body as DecisionBody;
  }
  return null;
}

/** Trailing one-liner for a tape row: the refusal reason, the model's own
 * summary, or the raw payload for the shapes that are just a string. */
export function summaryOf(d: Decision): string {
  const detail = (d.detail ?? {}) as Record<string, unknown>;
  if (typeof detail.why === "string") return detail.why;
  const body = bodyOf(d);
  if (body?.verdict?.summary) return body.verdict.summary;
  for (const key of ["skip", "error"]) {
    if (typeof detail[key] === "string") return detail[key] as string;
  }
  if (detail.watchlist && typeof detail.watchlist === "object") {
    const names = Object.keys(detail.watchlist as object);
    return `${names.length} names watched${names.length ? `: ${names.join(" ")}` : ""}`;
  }
  if (detail.reanchor && typeof detail.reanchor === "object") {
    const moved = Object.keys(detail.reanchor as object);
    return moved.length ? `re-anchored ${moved.join(" ")}` : "nothing to re-anchor";
  }
  if (detail.queued) {
    const q = detail.queued as { source?: string; confirm_min?: number };
    return `parked ${q.confirm_min}m for confirmation (${q.source})`;
  }
  return "";
}

/** Coarse row kind, for colouring the tape. */
export function kindOf(d: Decision): string {
  const detail = (d.detail ?? {}) as Record<string, unknown>;
  if (d.action !== "note") return d.action;
  for (const key of ["would_trade", "decision", "veto", "no_trade", "queued", "watchlist", "reanchor", "skip"]) {
    if (key in detail) return key;
  }
  return "note";
}
