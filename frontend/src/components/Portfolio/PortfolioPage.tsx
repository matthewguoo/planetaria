/**
 * PORTFOLIO — every registered paper account in one view, not just the one
 * the engine trades. Read-only aggregation: broker equity / day P/L /
 * positions / curve per account, plus the engine's own closed-plan record
 * for each. Accounts register by adding ALPACA_ACCOUNT_<NAME>_API_KEY /
 * _SECRET_KEY pairs to .env (paper keys only, enforced); the ACTIVE badge
 * marks the one the engine currently trades (switch in SYSTEM).
 */

import { useState } from "react";
import {
  apiError,
  getPortfolio,
  type PortfolioAccountRow,
  type PortfolioSnapshot,
} from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { usePoll } from "../../lib/usePoll";

function Spark({ history }: { history?: { equity: (number | null)[] } }) {
  const values = (history?.equity ?? []).filter((v): v is number => v != null);
  if (values.length < 2) return <span className="text-[10px] text-bb-muted">—</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const w = 120;
  const h = 28;
  const points = values
    .map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - ((v - lo) / span) * h).toFixed(1)}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} className="block">
      <polyline
        points={points}
        fill="none"
        strokeWidth={1.5}
        className={up ? "stroke-bb-profit" : "stroke-bb-loss"}
      />
    </svg>
  );
}

function AccountCard({ row }: { row: PortfolioAccountRow }) {
  if (row.error) {
    return (
      <div className="panel flex flex-col gap-1 p-3">
        <div className="flex items-center gap-2">
          <span className="text-[12px] tracking-widest text-white">{row.name.toUpperCase()}</span>
          {row.active && <span className="border border-bb-amber px-1 text-[9px] text-bb-amber">ACTIVE</span>}
        </div>
        <span className="text-[10px] text-bb-loss" title={row.error}>fetch failed: {row.error.slice(0, 80)}</span>
      </div>
    );
  }
  return (
    <div className="panel flex flex-col gap-1.5 p-3">
      <div className="flex items-center gap-2">
        <span className="text-[12px] tracking-widest text-white">{row.name.toUpperCase()}</span>
        {row.active && (
          <span className="border border-bb-amber px-1 text-[9px] text-bb-amber"
            title="The account the engine currently trades (switch in SYSTEM; restart applies)">
            ACTIVE
          </span>
        )}
        <span className="ml-auto text-[10px] text-bb-muted">{row.status}</span>
      </div>
      <div className="flex items-end justify-between gap-2">
        <div className="flex flex-col">
          <span data-numeric className="text-lg text-white">{fmtUsd(row.equity)}</span>
          <span data-numeric className={"text-[11px] " + pnlCls(row.day_pl)}>
            {fmtUsd(row.day_pl, true)} today
          </span>
        </div>
        <Spark history={row.history} />
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-bb-border pt-1 text-[10px]">
        <span className="text-bb-muted">CASH</span>
        <span data-numeric className="text-right text-white">{fmtUsd(row.cash)}</span>
        <span className="text-bb-muted">POSITIONS</span>
        <span data-numeric className="text-right text-white">{row.positions ?? "—"}</span>
        <span className="text-bb-muted">OPEN UNRLZD</span>
        <span data-numeric className={"text-right " + pnlCls(row.unrealized_pl)}>
          {fmtUsd(row.unrealized_pl, true)}
        </span>
        <span className="text-bb-muted" title="Engine-managed plans closed on this account (all time)">
          ENGINE TRADES
        </span>
        <span data-numeric className="text-right text-white">{row.plans_closed}</span>
        <span className="text-bb-muted" title="Realized P/L of engine-managed plans on this account">
          ENGINE REALIZED
        </span>
        <span data-numeric className={"text-right " + pnlCls(row.realized_pnl)}>
          {fmtUsd(row.realized_pnl, true)}
        </span>
      </div>
    </div>
  );
}

export default function PortfolioPage() {
  const [snap, setSnap] = useState<PortfolioSnapshot | null>(null);
  const [period, setPeriod] = useState("1M");
  const [error, setError] = useState<string | null>(null);

  usePoll(
    async (alive) => {
      try {
        const next = await getPortfolio(period);
        if (alive()) {
          setSnap(next);
          setError(null);
        }
      } catch (err) {
        if (alive()) setError(apiError(err));
      }
    },
    30_000,
    [period],
  );

  const totals = snap?.totals;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <div className="grid shrink-0 grid-cols-2 gap-px md:grid-cols-4">
        <div className="panel flex flex-col gap-1 p-3">
          <span className="text-[10px] tracking-widest text-bb-muted">TOTAL EQUITY (ALL ACCOUNTS)</span>
          <span data-numeric className="text-lg text-white">{fmtUsd(totals?.equity)}</span>
        </div>
        <div className="panel flex flex-col gap-1 p-3">
          <span className="text-[10px] tracking-widest text-bb-muted">DAY P/L</span>
          <span data-numeric className={"text-lg " + pnlCls(totals?.day_pl)}>
            {fmtUsd(totals?.day_pl, true)}
          </span>
        </div>
        <div className="panel flex flex-col gap-1 p-3">
          <span className="text-[10px] tracking-widest text-bb-muted">OPEN UNRLZD</span>
          <span data-numeric className={"text-lg " + pnlCls(totals?.unrealized_pl)}>
            {fmtUsd(totals?.unrealized_pl, true)}
          </span>
        </div>
        <div className="panel flex flex-col gap-1 p-3">
          <span className="text-[10px] tracking-widest text-bb-muted">ACCOUNTS · POSITIONS</span>
          <span data-numeric className="text-lg text-white">
            {totals ? `${totals.accounts}${totals.errors ? ` (${totals.errors} err)` : ""} · ${totals.positions}` : "—"}
          </span>
        </div>
      </div>

      <div className="panel flex shrink-0 items-center gap-2 px-3 py-1.5">
        <span className="text-[10px] tracking-widest text-bb-muted">CURVES</span>
        {["1W", "1M", "3M", "1A"].map((p) => (
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
        <span className="ml-auto text-[9px] text-bb-muted">
          add accounts: ALPACA_ACCOUNT_&lt;NAME&gt;_API_KEY/_SECRET_KEY in .env (paper only) · 30s refresh
        </span>
      </div>

      {error && <div className="panel p-2 text-[11px] text-bb-loss">{error}</div>}

      <div className="grid grid-cols-1 gap-px md:grid-cols-2 xl:grid-cols-3">
        {(snap?.accounts ?? []).map((row) => (
          <AccountCard key={row.name} row={row} />
        ))}
        {!snap && !error && (
          <div className="panel p-3 text-[11px] text-bb-muted">loading accounts…</div>
        )}
      </div>
    </div>
  );
}
