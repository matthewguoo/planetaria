/**
 * Full account dashboard for the connected (paper) account — the Alpaca
 * dashboard equivalent: summary stats, equity curve, live positions
 * (managed + untracked), open orders, and closed-trade history. Any
 * position row opens on the chart as a position view.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  adoptPositions,
  apiError,
  cancelOpenOrder,
  closePosition,
  getAccountHistory,
  getAccountRisk,
  getHistory,
  getOpenOrders,
  planStopRisk,
  type AccountRisk,
  type OpenOrder,
  type Plan,
  type PortfolioHistory,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { useAccountStore } from "../../store/accountStore";
import { useTradingStore } from "../../store/tradingStore";
import { useUiStore } from "../../store/uiStore";

function StatCard({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="panel flex flex-col gap-1 p-3">
      <span className="text-[10px] tracking-widest text-bb-muted">{label}</span>
      <span data-numeric className={"text-lg " + (cls ?? "text-white")}>
        {value}
      </span>
    </div>
  );
}

const fmtSigned = (v: number) =>
  `${v >= 0 ? "+" : "-"}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

/** Portfolio factor exposures + correlation structure of open underlyings. */
function RiskPanel({ risk }: { risk: AccountRisk | null }) {
  if (!risk || risk.total.open_plans === 0) return null;
  const g = risk.greeks;
  const metrics: [string, string, string, string][] = [
    // [label, value, cls, tooltip]
    ["AT RISK", `${fmtUsd(risk.total.risk_dollars)}${risk.total.risk_pct != null ? ` · ${risk.total.risk_pct.toFixed(1)}%` : ""}`,
     "text-bb-orange", "Sum of every open position's loss at its stop"],
    ["CORR-ADJ", `${fmtUsd(risk.total.corr_risk_dollars)}${risk.total.corr_risk_pct != null ? ` · ${risk.total.corr_risk_pct.toFixed(1)}%` : ""}`,
     "text-bb-orange", "sqrt(r'ρr) over underlying return correlations — what the stops are worth as ONE portfolio bet. Equals the simple sum when everything is perfectly correlated."],
    ["CONCENTRATION", `${risk.total.concentration_pct.toFixed(0)}%`, "text-bb-amber",
     "Largest single underlying's share of total stop risk"],
    ["NET Δ$", fmtSigned(g.delta_dollars), pnlCls(g.delta_dollars),
     "Net delta dollars: P/L per +100% underlying move; sign = direction"],
    ["β-WTD Δ$ (SPY)", fmtSigned(g.beta_weighted_delta_dollars), pnlCls(g.beta_weighted_delta_dollars),
     "Delta dollars scaled by each underlying's beta to SPY — the portfolio's effective equity-market exposure"],
    ["VEGA /pt", fmtSigned(g.vega_per_pt), pnlCls(g.vega_per_pt),
     "P/L per +1 vol point across all legs"],
    ["THETA /day", fmtSigned(g.theta_per_day), pnlCls(g.theta_per_day),
     "P/L per trading day of pure time decay"],
    ["RHO /1%", fmtSigned(g.rho_per_pct), pnlCls(g.rho_per_pct),
     "Interest-rate sensitivity: P/L per +1% risk-free rate (options rho). See ρ(RATES) below for the empirical rate correlation of each underlying."],
  ];
  return (
    <div className="panel flex shrink-0 flex-col">
      <div className="panel-title">
        PORTFOLIO RISK
        {!risk.history_ok && (
          <span className="ml-2 text-[9px] text-bb-orange" title="Daily return history unreachable — pairwise correlations default to 1 (no diversification credit)">
            · CORRELATIONS UNAVAILABLE (ρ=1 ASSUMED)
          </span>
        )}
      </div>
      <div className="grid grid-cols-4 gap-px border-b border-bb-border/50 xl:grid-cols-8">
        {metrics.map(([label, value, cls, tip]) => (
          <div key={label} className="flex flex-col gap-0.5 p-2" title={tip}>
            <span className="text-[9px] tracking-wider text-bb-muted">{label}</span>
            <span data-numeric className={"text-[12px] " + cls}>{value}</span>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-8 gap-y-1 p-2">
        <table className="border-collapse text-[11px]">
          <thead className="text-[9px] text-bb-muted">
            <tr>
              <th className="px-2 py-0.5 text-left">UNDERLYING</th>
              <th className="px-2 py-0.5 text-right">RISK</th>
              <th className="px-2 py-0.5 text-right">RISK%</th>
              <th className="px-2 py-0.5 text-right" title="Beta vs SPY (60d daily returns)">β SPY</th>
              <th className="px-2 py-0.5 text-right" title="Correlation vs SPY daily returns">ρ SPY</th>
              <th className="px-2 py-0.5 text-right" title="Correlation vs daily 10y-treasury-yield changes (^TNX)">ρ RATES</th>
              <th className="px-2 py-0.5 text-right">Δ$</th>
            </tr>
          </thead>
          <tbody>
            {risk.underlyings.map((u) => (
              <tr key={u.symbol} className="border-t border-bb-border/40">
                <td className="px-2 py-0.5 text-white">{u.symbol}</td>
                <td data-numeric className="px-2 py-0.5 text-right text-bb-orange">{fmtUsd(u.risk_dollars)}</td>
                <td data-numeric className="px-2 py-0.5 text-right text-bb-orange">
                  {u.risk_pct != null ? `${u.risk_pct.toFixed(1)}%` : "—"}
                </td>
                <td data-numeric className="px-2 py-0.5 text-right">{u.beta_spy?.toFixed(2) ?? "—"}</td>
                <td data-numeric className="px-2 py-0.5 text-right">{u.corr_spy?.toFixed(2) ?? "—"}</td>
                <td data-numeric className="px-2 py-0.5 text-right">{u.corr_rate?.toFixed(2) ?? "—"}</td>
                <td data-numeric className={"px-2 py-0.5 text-right " + pnlCls(u.delta_dollars)}>
                  {fmtSigned(u.delta_dollars)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {risk.matrix.symbols.length > 1 && (
          <table className="border-collapse text-[10px]" title="Pairwise correlation of daily returns (60d)">
            <thead className="text-[9px] text-bb-muted">
              <tr>
                <th className="px-1.5 py-0.5 text-left">ρ</th>
                {risk.matrix.symbols.map((s) => (
                  <th key={s} className="px-1.5 py-0.5 text-right">{s}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {risk.matrix.symbols.map((row, i) => (
                <tr key={row} className="border-t border-bb-border/40">
                  <td className="px-1.5 py-0.5 text-bb-muted">{row}</td>
                  {risk.matrix.rho[i].map((v, j) => (
                    <td
                      key={j}
                      data-numeric
                      className={
                        "px-1.5 py-0.5 text-right " +
                        (v == null ? "text-bb-muted" : Math.abs(v) > 0.7 && i !== j ? "text-bb-orange" : "text-white")
                      }
                    >
                      {v == null ? "—" : v.toFixed(2)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/** Realized-vs-specified exit quality: does your 50% stop actually lose
 * ~50%? Slippage per SL exit = specified stop premium − realized exit
 * premium (positive = filled worse than the line); TP improvement = fill
 * better than the line (resting limits fill at-or-better by construction).
 * Pure client-side math over closed trades. */
function ExitQualityPanel({ closed }: { closed: Plan[] | null }) {
  if (!closed) return null;
  const basis = (t: Plan) => Math.abs(t.fill_premium ?? t.entry_limit) || 1;
  const qty = (t: Plan) => t.filled_qty || t.qty;

  const slExits = closed.filter(
    (t) => t.exit_reason === "sl" && t.exit_premium != null,
  );
  const tpExits = closed.filter(
    (t) => t.exit_reason === "tp" && t.exit_premium != null,
  );
  if (!slExits.length && !tpExits.length) return null;

  const slRows = slExits.map((t) => {
    const slip = t.sl_premium - (t.exit_premium as number); // premium/share
    return { t, slip, slipPct: slip / basis(t), dollars: slip * 100 * qty(t) };
  });
  const tpRows = tpExits.map((t) => {
    const improve = (t.exit_premium as number) - t.tp_premium;
    return { t, improve, improvePct: improve / basis(t) };
  });
  const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
  const worstSl = slRows.length ? slRows.reduce((a, b) => (b.slip > a.slip ? b : a)) : null;

  return (
    <div className="panel flex shrink-0 flex-col">
      <div
        className="panel-title"
        title="Slippage = how much worse than your specified stop each SL exit actually filled (ladder + spread + gap). TP fills at-or-better by construction (broker-resting limit)."
      >
        EXIT QUALITY · realized vs specified
      </div>
      <div className="grid grid-cols-2 gap-px md:grid-cols-4">
        <div className="flex flex-col gap-0.5 p-2">
          <span className="text-[9px] tracking-wider text-bb-muted">SL EXITS</span>
          <span data-numeric className="text-[12px] text-white">{slRows.length}</span>
        </div>
        <div className="flex flex-col gap-0.5 p-2" title="Average slip past the stop line, as % of entry premium">
          <span className="text-[9px] tracking-wider text-bb-muted">AVG SLIP</span>
          <span data-numeric className={"text-[12px] " + (avg(slRows.map((r) => r.slipPct)) > 0.1 ? "text-bb-orange" : "text-white")}>
            {slRows.length ? `${(avg(slRows.map((r) => r.slipPct)) * 100).toFixed(1)}% · $${avg(slRows.map((r) => r.dollars)).toFixed(0)}` : "—"}
          </span>
        </div>
        <div className="flex flex-col gap-0.5 p-2" title="Worst single SL fill vs its line">
          <span className="text-[9px] tracking-wider text-bb-muted">WORST SLIP</span>
          <span data-numeric className="text-[12px] text-bb-loss">
            {worstSl ? `${(worstSl.slipPct * 100).toFixed(1)}% (${worstSl.t.underlying})` : "—"}
          </span>
        </div>
        <div className="flex flex-col gap-0.5 p-2" title="TP fills vs the line (resting limits fill at-or-better; software-triggered TPs can fill through)">
          <span className="text-[9px] tracking-wider text-bb-muted">TP EXITS · AVG BETTER</span>
          <span data-numeric className="text-[12px] text-bb-profit">
            {tpRows.length
              ? `${tpRows.length} · ${((v) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`)(avg(tpRows.map((r) => r.improvePct)))}`
              : "—"}
          </span>
        </div>
      </div>
    </div>
  );
}

function EquityCurve({ history }: { history: PortfolioHistory | null }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = "10px 'SF Mono', Consolas, monospace";

    const points = (history?.equity ?? [])
      .map((v, i) => ({ v, t: history!.timestamps[i] }))
      .filter((p): p is { v: number; t: number } => p.v !== null && p.v > 0);
    if (points.length < 2) {
      ctx.fillStyle = "#666666";
      ctx.textAlign = "center";
      ctx.fillText(
        history === null ? "loading…" : "no portfolio history (broker not configured?)",
        w / 2,
        h / 2,
      );
      return;
    }
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of points) {
      if (p.v < lo) lo = p.v;
      if (p.v > hi) hi = p.v;
    }
    const pad = (hi - lo || hi * 0.01 || 1) * 0.1;
    lo -= pad;
    hi += pad;
    const xOf = (i: number) => (i / (points.length - 1)) * (w - 60) + 4;
    const yOf = (v: number) => h - 16 - ((v - lo) / (hi - lo)) * (h - 24);

    const up = points[points.length - 1].v >= points[0].v;
    const color = up ? "#00C853" : "#FF1744";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((p, i) => (i === 0 ? ctx.moveTo(xOf(i), yOf(p.v)) : ctx.lineTo(xOf(i), yOf(p.v))));
    ctx.stroke();
    // Fill under.
    ctx.lineTo(xOf(points.length - 1), h - 16);
    ctx.lineTo(xOf(0), h - 16);
    ctx.closePath();
    ctx.fillStyle = up ? "rgba(0,200,83,0.08)" : "rgba(255,23,68,0.08)";
    ctx.fill();
    // End labels.
    ctx.fillStyle = "#999999";
    ctx.textAlign = "left";
    ctx.fillText(`$${Math.round(points[points.length - 1].v).toLocaleString()}`, w - 56, yOf(points[points.length - 1].v));
    ctx.fillStyle = "#666666";
    const first = new Date(points[0].t * 1000);
    const last = new Date(points[points.length - 1].t * 1000);
    ctx.fillText(first.toLocaleDateString("en-US", { month: "short", day: "numeric" }), 4, h - 4);
    ctx.textAlign = "right";
    ctx.fillText(last.toLocaleDateString("en-US", { month: "short", day: "numeric" }), w - 60, h - 4);
  }, [history]);

  return <canvas ref={ref} className="h-full w-full" />;
}

export function AccountPage() {
  const account = useAccountStore((s) => s.account);
  const positions = useAccountStore((s) => s.positions);
  const untracked = useAccountStore((s) => s.untracked);
  const refreshPositions = useAccountStore((s) => s.refreshPositions);
  const viewPosition = useUiStore((s) => s.viewPosition);
  const setSymbol = useTradingStore((s) => s.setSymbol);

  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [period, setPeriod] = useState("1M");
  const [orders, setOrders] = useState<OpenOrder[] | null>(null);
  const [closed, setClosed] = useState<Plan[] | null>(null);
  const [risk, setRisk] = useState<AccountRisk | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    const results = await Promise.allSettled([
      getAccountHistory(period, period === "1D" ? "15Min" : "1D"),
      getOpenOrders(),
      getHistory(),
      refreshPositions(),
      getAccountRisk(),
    ]);
    if (results[0].status === "fulfilled") setHistory(results[0].value);
    else setHistory({ timestamps: [], equity: [], profit_loss: [], base_value: null });
    if (results[1].status === "fulfilled") setOrders(results[1].value);
    else setOrders([]);
    if (results[2].status === "fulfilled") setClosed(results[2].value);
    if (results[4].status === "fulfilled") setRisk(results[4].value);
    const failed = results.find((r) => r.status === "rejected");
    if (failed) setError(apiError((failed as PromiseRejectedResult).reason));
  }, [period, refreshPositions]);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const unrealized = positions.reduce((acc, p) => acc + (p.unrealized_pnl ?? 0), 0);
  const openView = (plan: Plan) => {
    setSymbol(plan.underlying);
    viewPosition(plan.id);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <div className="grid shrink-0 grid-cols-2 gap-px md:grid-cols-4 xl:grid-cols-7">
        <StatCard label="EQUITY" value={fmtUsd(account?.equity)} />
        <StatCard label="CASH" value={fmtUsd(account?.cash)} />
        <StatCard label="BUYING POWER" value={fmtUsd(account?.buying_power)} />
        <StatCard
          label="DAY REALIZED"
          value={fmtUsd(account?.day_realized_pnl, true)}
          cls={pnlCls(account?.day_realized_pnl)}
        />
        <StatCard label="OPEN UNRLZD" value={fmtUsd(unrealized, true)} cls={pnlCls(unrealized)} />
        <div
          className="panel flex flex-col gap-1 p-3"
          title={
            "Account % lost if EVERY open position exits at its stop loss.\n" +
            "CORR = correlation-adjusted (positions in correlated underlyings " +
            "don't diversify — three index ETF longs are ~one bet)."
          }
        >
          <span className="text-[10px] tracking-widest text-bb-muted">AT RISK @ SL</span>
          <span data-numeric className="text-lg text-bb-orange">
            {risk?.total.risk_pct != null
              ? `${risk.total.risk_pct.toFixed(1)}%`
              : risk
                ? fmtUsd(risk.total.risk_dollars)
                : "—"}
            {risk?.total.corr_risk_pct != null && (
              <span className="ml-1 text-[10px] text-bb-muted">
                CORR {risk.total.corr_risk_pct.toFixed(1)}%
              </span>
            )}
          </span>
        </div>
        <StatCard
          label="STATUS"
          value={`${account?.status ?? "—"} · DT ${account?.daytrade_count ?? "—"}`}
          cls="text-bb-amber"
        />
      </div>

      <div className="panel flex h-48 shrink-0 flex-col">
        <div className="panel-title flex items-center gap-2">
          EQUITY CURVE
          <span className="ml-auto flex gap-px">
            {["1D", "1W", "1M", "3M", "1A"].map((p) => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                className={
                  "px-1.5 text-[10px] " +
                  (period === p ? "bg-bb-amber font-semibold text-black" : "text-bb-muted hover:text-bb-amber")
                }
              >
                {p}
              </button>
            ))}
          </span>
        </div>
        <div className="min-h-0 flex-1 p-1">
          <EquityCurve history={history} />
        </div>
      </div>

      <RiskPanel risk={risk} />
      <ExitQualityPanel closed={closed} />

      <div className="panel flex min-h-[10rem] shrink-0 flex-col">
        <div className="panel-title">
          POSITIONS ({positions.length}
          {untracked.length ? ` + ${untracked.length} untracked` : ""})
        </div>
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-[10px] text-bb-muted">
            <tr>
              <th className="px-2 py-1 text-left">POSITION</th>
              <th className="px-2 py-1 text-right">QTY</th>
              <th className="px-2 py-1 text-center">STATUS</th>
              <th className="px-2 py-1 text-right">ENTRY</th>
              <th className="px-2 py-1 text-right">MARK</th>
              <th className="px-2 py-1 text-right">UNRLZD</th>
              <th className="px-2 py-1 text-right">TP / SL</th>
              <th className="px-2 py-1 text-right" title="Account % lost if this position exits at its stop loss">
                RISK%
              </th>
              <th className="px-2 py-1 text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr
                key={p.id}
                className="cursor-pointer border-b border-bb-border/50 hover:bg-bb-hover"
                onClick={() => openView(p)}
                title="View this position on the chart (entry-anchored P/L surface)"
              >
                <td className="px-2 py-1 text-white">
                  {p.underlying}
                  <span className="ml-1 text-[10px] text-bb-muted">
                    {p.legs.map((l) => `${l.side > 0 ? "+" : "−"}${l.ratio || 1}${l.right}${l.strike}`).join(" ")}
                  </span>
                </td>
                <td data-numeric className="px-2 py-1 text-right">{p.filled_qty || p.qty}</td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-muted">{p.status.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right">{(p.fill_premium ?? p.entry_limit).toFixed(2)}</td>
                <td data-numeric className="px-2 py-1 text-right">{p.mark != null ? p.mark.toFixed(2) : "—"}</td>
                <td data-numeric className={"px-2 py-1 text-right " + pnlCls(p.unrealized_pnl)}>
                  {fmtUsd(p.unrealized_pnl, true)}
                </td>
                <td data-numeric className="px-2 py-1 text-right">
                  <span className="text-bb-profit">{p.tp_premium.toFixed(2)}</span> /{" "}
                  <span className="text-bb-loss">{p.sl_premium.toFixed(2)}</span>
                </td>
                <td
                  data-numeric
                  className="px-2 py-1 text-right text-bb-orange"
                  title={`-$${planStopRisk(p).toFixed(0)} at stop`}
                >
                  {account?.equity ? `${((planStopRisk(p) / account.equity) * 100).toFixed(1)}%` : "—"}
                </td>
                <td className="px-2 py-1 text-right">
                  <button
                    className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black"
                    onClick={(e) => {
                      e.stopPropagation();
                      openView(p);
                    }}
                  >
                    VIEW
                  </button>
                  <button
                    className="ml-1 border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await closePosition(p.id);
                        await refreshPositions();
                      } catch (err) {
                        setError(apiError(err));
                      }
                    }}
                  >
                    CLOSE
                  </button>
                </td>
              </tr>
            ))}
            {untracked.map((p) => (
              <tr key={p.symbol} className="border-b border-bb-border/50 bg-bb-orange/5">
                <td className="px-2 py-1 text-white">
                  {p.symbol}
                  <span className="ml-1 text-[9px] text-bb-orange">UNTRACKED</span>
                </td>
                <td data-numeric className="px-2 py-1 text-right">{p.qty}</td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-orange">LIVE @ BROKER</td>
                <td data-numeric className="px-2 py-1 text-right">{p.avg_entry_price.toFixed(2)}</td>
                <td data-numeric className="px-2 py-1 text-right">
                  {p.current_price != null ? p.current_price.toFixed(2) : "—"}
                </td>
                <td data-numeric className={"px-2 py-1 text-right " + pnlCls(p.unrealized_pl)}>
                  {fmtUsd(p.unrealized_pl, true)}
                </td>
                <td className="px-2 py-1 text-right text-bb-muted">—</td>
                <td className="px-2 py-1 text-right text-bb-muted" title="No stop loss — adopt to define one">
                  —
                </td>
                <td className="px-2 py-1 text-right">
                  {p.occ && (
                    <button
                      className="border border-bb-amber px-1.5 text-[10px] text-bb-amber hover:bg-bb-amber hover:text-black"
                      onClick={async () => {
                        try {
                          await adoptPositions([p.symbol]);
                          await refreshPositions();
                        } catch (err) {
                          setError(apiError(err));
                        }
                      }}
                    >
                      ADOPT
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!positions.length && !untracked.length && (
              <tr>
                <td colSpan={9} className="px-2 py-4 text-center text-bb-muted">
                  no open positions
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel flex min-h-[8rem] shrink-0 flex-col">
        <div className="panel-title">OPEN ORDERS ({orders?.length ?? "…"})</div>
        <table className="w-full border-collapse text-[11px]">
          <thead className="text-[10px] text-bb-muted">
            <tr>
              <th className="px-2 py-1 text-left">ORDER</th>
              <th className="px-2 py-1 text-left">TYPE</th>
              <th className="px-2 py-1 text-right">QTY</th>
              <th className="px-2 py-1 text-right">LIMIT</th>
              <th className="px-2 py-1 text-center">STATUS</th>
              <th className="px-2 py-1 text-right">SUBMITTED</th>
              <th className="px-2 py-1 text-right">ACTIONS</th>
            </tr>
          </thead>
          <tbody>
            {(orders ?? []).map((o) => (
              <tr key={o.id} className="border-b border-bb-border/50">
                <td className="px-2 py-1 text-white">
                  {o.legs.length
                    ? o.legs.map((l) => `${l.side === "buy" ? "+" : "−"}${l.ratio}× ${l.symbol}`).join(" ")
                    : `${o.side.toUpperCase()} ${o.symbol}`}
                </td>
                <td className="px-2 py-1 text-bb-muted">{o.type.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right">
                  {o.filled_qty > 0 ? `${o.filled_qty}/` : ""}
                  {o.qty ?? "—"}
                </td>
                <td data-numeric className="px-2 py-1 text-right">
                  {o.limit_price != null ? o.limit_price.toFixed(2) : "MKT"}
                </td>
                <td className="px-2 py-1 text-center text-[10px] text-bb-muted">{o.status.toUpperCase()}</td>
                <td data-numeric className="px-2 py-1 text-right text-bb-muted">
                  {o.submitted_at
                    ? new Date(o.submitted_at).toLocaleTimeString("en-US", {
                        timeZone: "America/New_York",
                        hour: "2-digit",
                        minute: "2-digit",
                        hour12: false,
                      }) + " ET"
                    : "—"}
                </td>
                <td className="px-2 py-1 text-right">
                  <button
                    className="border border-bb-loss px-1.5 text-[10px] text-bb-loss hover:bg-bb-loss hover:text-black"
                    onClick={async () => {
                      try {
                        await cancelOpenOrder(o.id);
                        setOrders(await getOpenOrders());
                      } catch (err) {
                        setError(apiError(err));
                      }
                    }}
                  >
                    CANCEL
                  </button>
                </td>
              </tr>
            ))}
            {orders && !orders.length && (
              <tr>
                <td colSpan={7} className="px-2 py-4 text-center text-bb-muted">
                  no open orders
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel flex min-h-[8rem] flex-1 flex-col">
        <div className="panel-title">
          CLOSED TRADES ({closed?.length ?? "…"}) ·{" "}
          <span data-numeric className={pnlCls((closed ?? []).reduce((a, t) => a + (t.realized_pnl ?? 0), 0))}>
            TOTAL {fmtUsd((closed ?? []).reduce((a, t) => a + (t.realized_pnl ?? 0), 0), true)}
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <table className="w-full border-collapse text-[11px]">
            <tbody>
              {(closed ?? []).map((t) => (
                <tr key={t.id} className="border-b border-bb-border/50">
                  <td className="px-2 py-1 text-bb-muted">{t.created_at.slice(0, 10)}</td>
                  <td className="px-2 py-1 text-white">
                    {t.underlying}
                    <span className="ml-1 text-[10px] text-bb-muted">
                      {t.legs.map((l) => `${l.side > 0 ? "+" : "−"}${l.strike}${l.right}`).join(" ")}
                    </span>
                  </td>
                  <td data-numeric className="px-2 py-1 text-right">×{t.qty}</td>
                  <td className="px-2 py-1 text-center text-[10px] text-bb-muted">
                    {(t.exit_reason ?? t.status).toUpperCase()}
                  </td>
                  <td data-numeric className={"px-2 py-1 text-right " + pnlCls(t.realized_pnl)}>
                    {fmtUsd(t.realized_pnl, true)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {error && <div className="px-2 py-1 text-[10px] text-bb-loss">{error}</div>}
    </div>
  );
}
