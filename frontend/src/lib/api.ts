import axios from "axios";

// Same-origin by default (vite dev/preview proxy /api to the backend), so
// the app works from any host — laptop, LAN phone, or a tunnel — without
// baking in localhost. VITE_API_URL still overrides for split deployments.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? "",
  timeout: 10_000,
});

export type SymbolHit = { symbol: string; name: string };

export async function searchSymbols(q: string): Promise<SymbolHit[]> {
  const { data } = await api.get<{ results: SymbolHit[] }>("/api/symbols/search", {
    params: { q },
  });
  return data.results;
}

export type RiskSettings = {
  max_loss_pct: number;
  daily_loss_pct: number;
  max_positions: number;
  bp_cap_pct: number;
  default_tp_pct: number;
  default_sl_pct: number;
  time_stop_et: string;
  expiry_time_stop_et: string;
  max_spread_pct: number;
  entry_ttl_min: number;
  max_trades_per_day: number;
  /** SL breach must persist this long (on the Kalman fair value) before
   * firing; deep breaches fire immediately. 0 = instant. */
  sl_confirm_s: number;
  equity_max_notional_per_name_pct: number;
  equity_gross_exposure_pct: number;
  equity_long_only: boolean;
  equity_short_overnight: boolean;
  manual_book: {
    enabled: boolean;
    equity_usd: number;
    max_loss_pct: number;
    daily_loss_usd: number;
    max_open_plans: number;
    require_stop_equity: boolean;
  };
};

/** The discretionary book's envelope, as GET /api/account reports it. */
export type ManualBook = {
  enabled: boolean;
  equity_usd: number;
  max_loss_pct: number;
  per_trade_max_loss_usd: number;
  daily_loss_usd: number;
  max_open_plans: number;
  require_stop_equity: boolean;
  open_plans: number;
  used_usd: number;
  remaining_usd: number;
  realized_today: number;
};

export type Account = {
  equity: number;
  cash: number;
  buying_power: number;
  daytrade_count: number;
  status: string;
  paper: boolean;
  risk: RiskSettings;
  day_realized_pnl: number;
  manual_book?: ManualBook;
};

export type PlanLeg = {
  symbol: string;
  /** null on equity legs — a share leg has no right/strike/expiry. */
  right: "C" | "P" | null;
  strike: number | null;
  expiry: string | null;
  side: number;
  ratio: number;
  entry: number;
  iv: number | null;
};

export type Plan = {
  id: string;
  created_at: string;
  underlying: string;
  strategy: string;
  strategy_id?: string | null;
  asset_class?: "option" | "equity";
  extended_hours?: boolean;
  legs: PlanLeg[];
  qty: number;
  entry_limit: number;
  /** null = bracketless (time-stop-only, or SL-only swing plans). */
  tp_premium: number | null;
  sl_premium: number | null;
  time_stop_utc: string | null;
  status: string;
  filled_qty?: number | null;
  exit_reason: string | null;
  fill_premium: number | null;
  exit_premium: number | null;
  realized_pnl: number | null;
  mark?: number | null;
  tp_order_id?: string | null;
  unrealized_pnl?: number | null;
  quote_stale?: boolean;
  /** History enrichment: actual entry/exit event times from the journal. */
  entered_at?: string | null;
  exited_at?: string | null;
  /** Chunked closing waves (external liquidations): one chart marker each. */
  exit_fills?: { ts: string; premium: number; qty: number }[] | null;
  /** Execution-quality ledger: realized fill vs submit-time fair value. */
  exec_quality?: Partial<
    Record<
      "entry" | "exit",
      {
        fair: number;
        half_spread: number;
        ts: string;
        fill?: number;
        cost?: number;
        spread_capture?: number;
      }
    >
  > | null;
};

export type UntrackedPosition = {
  symbol: string;
  qty: number;
  side: number;
  asset_class: "option" | "stock";
  avg_entry_price: number;
  current_price: number | null;
  market_value: number | null;
  unrealized_pl: number | null;
  occ: { underlying: string; expiry: string; right: "C" | "P"; strike: number } | null;
};

export type PositionsPayload = {
  positions: Plan[];
  untracked: UntrackedPosition[];
  untracked_error?: string;
};

export const getAccount = () => api.get<Account>("/api/account").then((r) => r.data);
export const putRisk = (patch: Partial<RiskSettings>) =>
  api.put<RiskSettings>("/api/settings/risk", patch).then((r) => r.data);
export const getPositions = () =>
  api.get<PositionsPayload>("/api/positions").then((r) => r.data);
export const adoptPositions = (symbols: string[]) =>
  api.post<{ adopted: Plan[] }>("/api/positions/adopt", { symbols }).then((r) => r.data.adopted);
export const getHistory = () =>
  api.get<{ trades: Plan[] }>("/api/history").then((r) => r.data.trades);
export const postOrder = (payload: object) =>
  api.post<Plan>("/api/orders", payload).then((r) => r.data);
export const closePosition = (planId: string) =>
  api.post(`/api/positions/${planId}/close`).then((r) => r.data);
export const flattenAll = () => api.post("/api/positions/flatten").then((r) => r.data);
export const tightenExits = (
  planId: string,
  patch: { tp_premium?: number; sl_premium?: number; time_stop_utc?: string },
) => api.patch<Plan>(`/api/positions/${planId}/exits`, patch).then((r) => r.data);

export type PortfolioHistory = {
  timestamps: number[];
  equity: (number | null)[];
  profit_loss: (number | null)[];
  base_value: number | null;
};

export type OpenOrder = {
  id: string;
  symbol: string;
  side: string;
  qty: number | null;
  filled_qty: number;
  type: string;
  limit_price: number | null;
  status: string;
  submitted_at: string | null;
  legs: { symbol: string; side: string; ratio: number }[];
};

export type AccountRisk = {
  asof: number;
  equity: number;
  history_ok: boolean;
  total: {
    risk_dollars: number;
    risk_pct: number | null;
    corr_risk_dollars: number;
    corr_risk_pct: number | null;
    concentration_pct: number;
    open_plans: number;
  };
  greeks: {
    delta_dollars: number;
    beta_weighted_delta_dollars: number;
    vega_per_pt: number;
    theta_per_day: number;
    rho_per_pct: number;
  };
  per_plan: {
    id: string;
    underlying: string;
    status: string;
    risk_dollars: number;
    risk_pct: number | null;
  }[];
  underlyings: {
    symbol: string;
    risk_dollars: number;
    risk_pct: number | null;
    beta_spy: number | null;
    corr_spy: number | null;
    corr_rate: number | null;
    delta_dollars: number;
  }[];
  matrix: { symbols: string[]; rho: (number | null)[][] };
};

export const getAccountRisk = () =>
  api.get<AccountRisk>("/api/account/risk").then((r) => r.data);

export type SystemState = {
  asof: number;
  feed: {
    configured: boolean;
    demo: boolean;
    sources: Record<string, string>;
    stream_age_s: number | null;
    stock_symbols: string[];
    option_symbols: number;
  };
  broker: {
    configured: boolean;
    paper: boolean;
    account_status: string;
    /** Broker calendar as the enforcer caches it. `known: false` until the
     * first successful fetch — absent is not the same as closed. */
    market_clock: {
      known: boolean;
      is_open?: boolean | null;
      next_open?: string | null;
      next_close?: string | null;
    };
  };
  db: { ok: boolean; latency_ms: number | null; engine: string };
  redis: { ok: boolean };
  enforcer: {
    monitors: number;
    monitored_plan_ids: string[];
    ghost_keys: number;
    last_reconcile_age_s: number | null;
    monitors_without_mid: Record<string, string>;
    parked_exits?: string[];
  };
  /** The strategy data plane. A dead feed shows here as a task state plus a
   * climbing last_event_age — never as quiet non-trading. */
  signals?: {
    feeds: Record<string, { task: string } & Record<string, unknown>>;
    event_bus: Record<string, unknown>;
  };
  strategies?: Record<string, unknown>;
  tasks: Record<string, string>;
  power?: { keep_awake_supported: boolean; keep_awake_active: boolean };
};

export type MarketClock = {
  known: boolean;
  is_open?: boolean | null;
  next_open?: string | null;
  next_close?: string | null;
  error?: string;
};

/** Forces the broker calendar fetch. `/api/system/state` only reports the
 * enforcer's cached clock, which stays unknown until something asks. */
export const getMarketClock = () =>
  api.get<MarketClock>("/api/market/clock").then((r) => r.data);

export type Quote = {
  symbol?: string;
  bid?: number | null;
  ask?: number | null;
  mid?: number | null;
  ts?: number | null;
};

/** Single REST quote. 503 when keys are missing or the feed is down — the
 * market page renders that as "no data" rather than as a zero. */
export const getQuote = (symbol: string) =>
  api.get<Quote>(`/api/quote/${encodeURIComponent(symbol)}`).then((r) => r.data);

export type FeedSettings = {
  chain_refresh_s: number;
  positions_poll_s: number;
  account_poll_s: number;
  public_poll_s: number;
  stock_feed: "iex" | "sip";
  option_feed: "indicative" | "opra";
  restart_required_keys: string[];
};

export type PlanEvent = {
  ts: string | null;
  event: string;
  source: string;
  target: string | null;
  applied: boolean;
  detail: string | null;
};

export const getSystemState = () =>
  api.get<SystemState>("/api/system/state").then((r) => r.data);
export const getFeedSettings = () =>
  api.get<FeedSettings>("/api/settings/feed").then((r) => r.data);
export const putFeedSettings = (patch: Partial<FeedSettings>) =>
  api.put<FeedSettings>("/api/settings/feed", patch).then((r) => r.data);
export const getPlanEvents = (planId: string) =>
  api.get<{ events: PlanEvent[] }>(`/api/positions/${planId}/events`).then((r) => r.data.events);

/** Client-side mirror of the server's plan_stop_risk: dollars lost if this
 * plan exits exactly at its stop. Bracketless plans (sl null) carry no
 * stop-defined risk — they are bounded by their book/allocation instead. */
export function planStopRisk(plan: {
  fill_premium?: number | null;
  entry_limit: number;
  sl_premium: number | null;
  filled_qty?: number | null;
  qty: number;
  asset_class?: string;
}): number {
  if (plan.sl_premium == null) return 0;
  const basis = plan.fill_premium ?? plan.entry_limit;
  const qty = plan.filled_qty ?? plan.qty;
  const mult = plan.asset_class === "equity" ? 1 : 100;
  return Math.max(basis - plan.sl_premium, 0) * mult * Math.max(qty, 0);
}

export const getAccountHistory = (period = "1M", timeframe = "1D") =>
  api
    .get<PortfolioHistory>("/api/account/history", { params: { period, timeframe } })
    .then((r) => r.data);
export const getOpenOrders = () =>
  api.get<{ orders: OpenOrder[] }>("/api/orders/open").then((r) => r.data.orders);
export const cancelOpenOrder = (orderId: string) =>
  api.delete(`/api/orders/${orderId}`).then((r) => r.data);

/** Extract a readable message from an axios error (FastAPI detail). */
export function apiError(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr.response?.data?.detail ?? anyErr.message ?? String(err);
}

// ---------------------------------------------------------- strategy runtime
// Control plane for the backend StrategyRunner.
// See backend/app/api/routes/strategies.py.

export type StrategyRuntime = {
  task: string;
  queue_size?: number;
  events_seen?: number;
  errors?: number;
  last_event_age_s?: number | null;
};

/** How much of the account a strategy may deploy. `pct` moves with equity;
 * `usd` is a fixed ceiling that does not. */
export type Allocation = { mode: "pct" | "usd"; value: number };

/** The drawdown that flattens a strategy's book and pauses it. `pct` is of
 * the strategy's ALLOCATION, not the account — a breaker must not loosen
 * because the account grew. This is what replaced the per-position stop. */
export type CircuitBreaker = { enabled: boolean; mode: "pct" | "usd"; value: number };

export type StrategyCapital = {
  allocation: Allocation;
  allocated: number;
  deployed: number;
  available: number;
  account_equity: number;
  pct_of_account: number | null;
  breaker?: BreakerState;
};

export type BreakerState = {
  breaker: CircuitBreaker;
  realized: number;
  unrealized: number;
  pnl: number;
  peak: number;
  drawdown: number;
  max_drawdown: number;
  limit: number;
  tripped: boolean;
  headroom: number;
};

export type StrategyInstance = {
  id: string;
  kind: string;
  name: string;
  params: Record<string, unknown>;
  allocation: Allocation;
  circuit_breaker: CircuitBreaker;
  /** Platform capabilities this KIND declares it needs (sip/shorts/…). */
  requires: string[];
  capital: StrategyCapital;
  state: "enabled" | "paused" | "disabled";
  created_at: string;
  updated_at: string;
  open_plans: number;
  runtime: StrategyRuntime;
};

export type StrategyKind = {
  kind: string;
  subscriptions: string[];
  default_params: Record<string, unknown>;
  requires: string[];
  doc: string;
  registered: RegisteredBlock | null;
};

/** The pre-registration as data — FROZEN at the pre-reg commit; live
 * results never edit it (docs/briefs/fund-capital-scheduling.md §5). */
export type RegisteredBlock = {
  doc: string;
  registered_commit: string;
  source_note: string;
  window: string;
  metric: string;
  metric_label: string;
  band: [number, number];
  sharpe_band?: [number, number];
  trades_per_day_band?: [number, number];
  backtest: { value: number; t?: number; sharpe?: number; basis: string };
  costs_assumed: string;
  ladder: { stage: string; sessions: number | null }[];
};

/** The instance's own record measured on the registered metric. */
export type LadderState = {
  stage: string;
  target_sessions: number | null;
  sessions_observed: number;
  trades_closed: number;
  samples: number;
  metric: string;
  metric_computed: boolean;
  metric_label: string | null;
  running_metric: number | null;
  band: [number, number] | null;
  band_status: "in" | "above" | "below" | null;
};

/** What the platform currently provides, against what strategies declare. */
export type Capabilities = Record<string, { met: boolean; detail: string }>;

export const setStrategyAllocation = (id: string, allocation: Allocation) =>
  api.put<StrategyInstance>(`/api/strategies/${id}/allocation`, allocation)
    .then((r) => r.data);
export const setStrategyBreaker = (id: string, breaker: CircuitBreaker) =>
  api.put<StrategyInstance>(`/api/strategies/${id}/breaker`, breaker).then((r) => r.data);
export const deleteStrategy = (id: string) =>
  api.delete<{ name: string; decisions_deleted: number }>(`/api/strategies/${id}`)
    .then((r) => r.data);

export type StrategyDecision = {
  id: number;
  ts: string;
  strategy_id: string;
  signal_ids: number[] | null;
  action: string; // note | intent | placed | rejected | skip | error
  dedupe_key: string | null;
  plan_id: string | null;
  detail: Record<string, unknown> | null;
};

/** One allocator's replay of a strategy's journalled would-be trades. */
export type TwinPolicy = {
  policy: string;
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
  curve: { ts: string | null; equity: number }[];
};

export type StrategyTwin = {
  signals: number;
  params: { equity: number; risk_pct: number; flat_pct: number };
  policies: TwinPolicy[];
};

/** The paper twin. Note-mode strategies place nothing, so `performance` is
 * empty for them by construction — this is the outcome they DO have. */
export const getStrategyTwin = (id: string) =>
  api.get<StrategyTwin>(`/api/strategies/${id}/twin`).then((r) => r.data);

export type SignalRecord = {
  id: number;
  ts: string;
  source: string;
  type: string;
  key: string | null;
  symbols: string[] | null;
  payload: Record<string, unknown> | null;
  parent_id: number | null;
};

export const getStrategies = () =>
  api.get<{ instances: StrategyInstance[] }>("/api/strategies").then((r) => r.data.instances);
export const getStrategyCatalog = () =>
  api
    .get<{ kinds: StrategyKind[]; capabilities: Capabilities }>("/api/strategies/catalog")
    .then((r) => r.data);
export const createStrategy = (kind: string, name: string, params: Record<string, unknown>) =>
  api.post<StrategyInstance>("/api/strategies", { kind, name, params }).then((r) => r.data);
export const patchStrategyParams = (id: string, params: Record<string, unknown>) =>
  api.patch<StrategyInstance>(`/api/strategies/${id}`, { params }).then((r) => r.data);
export const enableStrategy = (id: string) =>
  api.post<StrategyInstance>(`/api/strategies/${id}/enable`).then((r) => r.data);
export const pauseStrategy = (id: string) =>
  api.post<StrategyInstance>(`/api/strategies/${id}/pause`).then((r) => r.data);
export const flattenStrategy = (id: string) =>
  api.post(`/api/strategies/${id}/flatten`).then((r) => r.data);
export const triggerStrategy = (id: string, payload: Record<string, unknown> = {}) =>
  api.post(`/api/strategies/${id}/trigger`, { payload }).then((r) => r.data);
export const killAllStrategies = (flatten: boolean) =>
  api.post("/api/strategies/kill-all", { flatten }).then((r) => r.data);
export const getStrategyDecisions = (id: string, limit = 100) =>
  api
    .get<{ decisions: StrategyDecision[] }>(`/api/strategies/${id}/decisions`, {
      params: { limit },
    })
    .then((r) => r.data.decisions);
export const getSignals = (params: { limit?: number; type?: string; symbol?: string } = {}) =>
  api.get<{ signals: SignalRecord[] }>("/api/signals", { params }).then((r) => r.data.signals);

export type StrategySource = {
  kind: string;
  module: string;
  file: string;
  source: string;
};

export type StrategyPlanRow = {
  id: string;
  underlying: string;
  status: string;
  qty: number;
  entry_limit: number;
  exit_premium: number | null;
  realized_pnl: number | null;
  created_at?: string | null;
  asset_class?: string | null;
};

export type StrategyPerformance = {
  name: string;
  /** Absent (undefined) from an engine older than the registered plane —
   * the console renders nothing then; null means genuinely unregistered. */
  registered?: RegisteredBlock | null;
  ladder?: LadderState | null;
  open: number;
  closed: number;
  win_rate: number | null;
  realized_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  plans: StrategyPlanRow[];
};

export const getStrategySource = (kind: string) =>
  api
    .get<StrategySource>(`/api/strategies/catalog/${encodeURIComponent(kind)}/source`)
    .then((r) => r.data);
export const getStrategyPerformance = (id: string) =>
  api.get<StrategyPerformance>(`/api/strategies/${id}/performance`).then((r) => r.data);

// Live call-flow monitor (backend/app/services/call_log.py).

export type MonitorCall = {
  ts: number;
  category: "data" | "broker" | "engine";
  name: string;
  detail: string;
  ms: number | null;
  ok: boolean;
};

export type MonitorSnapshot = {
  status: {
    counts: Record<string, number>;
    errors: Record<string, number>;
    buffered: number;
  };
  calls: MonitorCall[];
};

export const getMonitorCalls = (category?: string, limit = 80) =>
  api
    .get<MonitorSnapshot>("/api/monitor/calls", { params: { category, limit } })
    .then((r) => r.data);

// Named paper-account selection (backend AccountService; restart applies).

export type AccountEntry = {
  name: string;
  key_masked: string;
  active: boolean;
  selected: boolean;
};

export type AccountList = {
  accounts: AccountEntry[];
  selected: string;
  applied: string | null;
  restart_required: boolean;
  paper_only: boolean;
};

export const getAccounts = () =>
  api.get<AccountList>("/api/system/accounts").then((r) => r.data);
export const selectAccount = (name: string) =>
  api.post<AccountList>("/api/system/accounts/select", { name }).then((r) => r.data);

/** ------------------------------------------------------------- FUND page
 * One payload for the fund root: allocation envelopes, per-strategy open
 * positions, and the account-level exposure rollup. Options carry margin
 * at risk (the collateral math), not fake deltas — the console has no
 * greeks and pretending otherwise would be worse than saying so. */
export type FundPosition = {
  plan_id: string;
  underlying: string;
  asset_class: "equity" | "option";
  qty: number;
  entry_limit: number;
  legs: number;
  margin_usd: number;
  net_usd: number | null;
  status: string;
  created_at: string | null;
};

export type FundInstance = {
  id: string;
  name: string;
  kind: string;
  state: string;
  live: boolean;
  allocated: number;
  deployed: number;
  available: number;
  breaker: { limit: number | null; drawdown: number | null; tripped: boolean | null };
  positions: FundPosition[];
};

export type FundState = {
  account: { equity: number; cash: number | null; buying_power: number | null };
  instances: FundInstance[];
  allocated_total: number;
  unallocated: number;
  exposure: {
    gross_long_usd: number;
    gross_short_usd: number;
    net_usd: number;
    beta_weighted_net_usd: number;
    options_margin_usd: number;
    options_premium_usd: number;
    by_underlying: { symbol: string; net_usd: number; gross_usd: number; beta_spy: number | null }[];
    corr: { symbols: string[]; matrix: (number | null)[][] };
  };
};

export const getFund = () => api.get<FundState>("/api/fund").then((r) => r.data);
