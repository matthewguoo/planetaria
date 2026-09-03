/**
 * ACCOUNT OVERVIEW — the live server's root page, and the top of the
 * phone's ACCOUNT tab. The equity big, today's move under it, then the
 * holdings the way a modern brokerage lists them: sortable by SIZE,
 * EXPOSURE, MOVERS, P/L, name; each row is the position's price, today's
 * change, its share of the account, its P/L — and, loudest, whether a
 * stop protects it. Tap a row → that symbol's trading interface.
 *
 * One component for both shells: columns fold on narrow screens; the
 * targets are finger-sized everywhere (the desktop just has more of them).
 */

import { useState } from "react";
import { apiError, getAccountRisk, getHoldings, type AccountRisk, type Holding } from "../../lib/api";
import { fmtUsd, pnlCls } from "../../lib/format";
import { exposureOf, SORT_LABEL, sortHoldings, summarize, type HoldingsSort } from "../../lib/holdings";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore, useTradingMode } from "../../store/accountStore";

const SORTS: HoldingsSort[] = ["size", "exposure", "movers", "pnl", "name"];

function pct(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(digits)}%`;
}

function ProtectionPill({ h }: { h: Holding }) {
  if (h.protected) {
    return (
      <span className="border border-bb-profit/60 px-1.5 text-[10px] tracking-wider text-bb-profit" title={`Stop ${h.sl?.toFixed(2)} enforced server-side`}>
        STOP {h.sl != null ? Math.abs(h.sl).toFixed(2) : ""}
      </span>
    );
  }
  if (h.plan_id) {
    return (
      <span className="border border-bb-orange/60 px-1.5 text-[10px] tracking-wider text-bb-orange" title="Managed plan without a stop (time stop only)">
        NO STOP · MANAGED
      </span>
    );
  }
  return (
    <span className="border border-bb-loss/60 px-1.5 text-[10px] tracking-wider text-bb-loss" title="Untracked at the broker — nothing will exit this position. Adopt it from POSITIONS.">
      NO STOP
    </span>
  );
}

export function OverviewPage({
  onOpen,
  onProtect,
}: {
  /** Open the trading interface for a holding. */
  onOpen: (h: Holding) => void;
  /** Jump to the positions surface (adopt / manage). */
  onProtect?: () => void;
}) {
  const account = useAccountStore((s) => s.account);
  const { live } = useTradingMode();
  const [rows, setRows] = useState<Holding[] | null>(null);
  const [risk, setRisk] = useState<AccountRisk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<HoldingsSort>(() => {
    try {
      const saved = localStorage.getItem("planetaria.holdingsSort") as HoldingsSort | null;
      return saved && SORTS.includes(saved) ? saved : "size";
    } catch {
      return "size";
    }
  });

  usePoll(async (alive) => {
    const [h, r] = await Promise.allSettled([getHoldings(), getAccountRisk()]);
    if (!alive()) return;
    if (h.status === "fulfilled") {
      setRows(h.value);
      setError(null);
    } else {
      setError(apiError(h.reason));
      setRows((prev) => prev ?? []);
    }
    if (r.status === "fulfilled") setRisk(r.value);
  }, 10_000);

  const equity = account?.equity ?? 0;
  const list = rows ?? [];
  const sum = summarize(list);
  const today = (account?.day_realized_pnl ?? 0) + sum.todayPl;
  const todayPct = equity > 0 ? today / (equity - today || equity) : 0;
  const sorted = sortHoldings(list, sort, equity);
  const unprotected = sum.total - sum.protectedCount;

  const pickSort = (s: HoldingsSort) => {
    setSort(s);
    try {
      localStorage.setItem("planetaria.holdingsSort", s);
    } catch {
      /* session only */
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      {/* The number. */}
      <div className="flex flex-col gap-1 px-4 pb-3 pt-4">
        <span className="text-[10px] tracking-widest text-bb-muted">
          {live ? "LIVE ACCOUNT" : "PAPER ACCOUNT"} · EQUITY
        </span>
        <span data-numeric className="text-[38px] font-semibold leading-none text-white sm:text-[44px]">
          {account ? `$${account.equity.toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}` : "—"}
        </span>
        <span data-numeric className={"text-[15px] " + pnlCls(today)}>
          {account ? `${fmtUsd(today, true)} (${pct(todayPct, 2)}) today` : ""}
          {account && (
            <span className="ml-2 text-[11px] text-bb-muted">
              realized {fmtUsd(account.day_realized_pnl, true)} · open {fmtUsd(sum.todayPl, true)}
            </span>
          )}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-px border-y border-bb-border bg-bb-border sm:grid-cols-5">
        {(
          [
            ["INVESTED", fmtUsd(sum.invested), equity > 0 ? `${((sum.invested / equity) * 100).toFixed(0)}% of equity` : "", "text-white"],
            ["CASH", fmtUsd(account?.cash), "settled buying power below", "text-white"],
            ["BUYING POWER", fmtUsd(account?.buying_power), "", "text-white"],
            ["OPEN P/L", fmtUsd(sum.unrealized, true), sum.invested > 0 ? pct(sum.unrealized / (sum.invested - sum.unrealized || 1)) + " on cost" : "", pnlCls(sum.unrealized)],
            [
              "AT RISK @ STOPS",
              risk?.total.risk_pct != null ? `${risk.total.risk_pct.toFixed(1)}%` : risk ? fmtUsd(risk.total.risk_dollars) : "—",
              unprotected ? `${unprotected} of ${sum.total} unprotected` : sum.total ? "every position has a stop" : "",
              unprotected ? "text-bb-loss" : "text-bb-orange",
            ],
          ] as const
        ).map(([label, value, sub, cls]) => (
          <div key={label} className="flex flex-col gap-0.5 bg-bb-panel px-3 py-2">
            <span className="text-[10px] tracking-widest text-bb-muted">{label}</span>
            <span data-numeric className={"text-[17px] " + cls}>{value}</span>
            {sub && <span className={"text-[10px] " + (label === "AT RISK @ STOPS" && unprotected ? "text-bb-loss" : "text-bb-muted")}>{sub}</span>}
          </div>
        ))}
      </div>

      {unprotected > 0 && onProtect && (
        <button
          className="mx-3 mt-2 flex h-11 items-center justify-between border border-bb-loss bg-bb-loss/10 px-3 text-[12px] text-bb-loss"
          onClick={onProtect}
        >
          <span>⚠ {unprotected} position{unprotected > 1 ? "s" : ""} without a stop — nothing will exit {unprotected > 1 ? "them" : "it"}</span>
          <span className="tracking-widest">ADOPT ›</span>
        </button>
      )}

      {/* Holdings. */}
      <div className="flex h-11 shrink-0 items-center gap-2 px-3 pt-2">
        <span className="text-[10px] tracking-widest text-bb-muted">HOLDINGS {sum.total ? `(${sum.total})` : ""}</span>
        <div className="chip-rail ml-auto">
          {SORTS.map((s) => (
            <button
              key={s}
              onClick={() => pickSort(s)}
              className={
                "h-9 shrink-0 px-2.5 text-[11px] tracking-wider " +
                (sort === s ? "bg-bb-amber font-semibold text-black" : "border border-bb-border text-bb-muted active:text-bb-amber hover:text-bb-amber")
              }
            >
              {SORT_LABEL[s]}
            </button>
          ))}
        </div>
      </div>
      <div className="hidden grid-cols-[1.4fr_0.8fr_1fr_1fr_1fr_1fr] gap-2 px-3 py-1 text-[10px] tracking-wider text-bb-muted sm:grid">
        <span>SYMBOL</span>
        <span className="text-right">QTY</span>
        <span className="text-right">PRICE · TODAY</span>
        <span className="text-right">VALUE · % ACCT</span>
        <span className="text-right">P/L</span>
        <span className="text-right">PROTECTION</span>
      </div>
      <div className="flex flex-col">
        {sorted.map((h) => {
          const exp = exposureOf(h, equity);
          const label = h.occ
            ? `${h.underlying} ${h.occ.expiry.slice(5)} ${h.occ.strike}${h.occ.right}`
            : h.underlying;
          return (
            <button
              key={h.symbol}
              onClick={() => onOpen(h)}
              className="grid grid-cols-[1.3fr_1fr_1fr] items-center gap-x-2 gap-y-0.5 border-t border-bb-border/60 px-3 py-2.5 text-left hover:bg-bb-hover active:bg-bb-hover sm:grid-cols-[1.4fr_0.8fr_1fr_1fr_1fr_1fr]"
              title="Open this symbol's trading interface"
            >
              <span className="min-w-0">
                <span className="block truncate text-[15px] font-semibold text-white">
                  {label}
                  {h.side < 0 && <span className="ml-1 text-[10px] text-bb-orange">SHORT</span>}
                </span>
                <span className="block truncate text-[11px] text-bb-muted">
                  {h.name ?? (h.occ ? "option" : "")}
                  <span className="sm:hidden"> · ×{Math.abs(h.qty)}</span>
                </span>
              </span>
              <span data-numeric className="hidden text-right text-[13px] text-white sm:block">
                {Math.abs(h.qty) % 1 === 0 ? Math.abs(h.qty) : Math.abs(h.qty).toFixed(3)}
              </span>
              <span className="text-right">
                <span data-numeric className="block text-[14px] text-white">{h.current_price != null ? h.current_price.toFixed(2) : "—"}</span>
                <span data-numeric className={"block text-[11px] " + pnlCls(h.change_today)}>{pct(h.change_today, 2)}</span>
              </span>
              <span className="text-right">
                <span data-numeric className="block text-[14px] text-white">{fmtUsd(holdingsValue(h))}</span>
                <span data-numeric className="block text-[11px] text-bb-muted">{(exp * 100).toFixed(1)}%</span>
              </span>
              <span className={"text-right " + pnlCls(h.unrealized_pl)}>
                <span data-numeric className="block text-[14px]">{fmtUsd(h.unrealized_pl, true)}</span>
                <span data-numeric className="block text-[11px]">{pct(h.unrealized_plpc)}</span>
              </span>
              <span className="col-span-3 flex justify-between sm:col-span-1 sm:justify-end">
                <span className="text-[10px] text-bb-muted sm:hidden">
                  {h.time_stop_utc ? `exit ${h.time_stop_utc.slice(5, 10)}` : ""}
                </span>
                <ProtectionPill h={h} />
              </span>
            </button>
          );
        })}
        {rows === null && <div className="px-3 py-6 text-center text-[12px] text-bb-muted">loading holdings…</div>}
        {rows !== null && !rows.length && <div className="px-3 py-6 text-center text-[12px] text-bb-muted">no positions at the broker</div>}
        {error && <div className="px-3 py-2 text-[11px] text-bb-loss">✗ {error}</div>}
      </div>
    </div>
  );
}

function holdingsValue(h: Holding): number {
  return Math.abs(h.market_value ?? 0);
}
