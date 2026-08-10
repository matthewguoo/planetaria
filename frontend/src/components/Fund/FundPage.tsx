/**
 * FUND — the root management page. One glance answers: whose money is
 * where (the allocation bar), what is each strategy actually holding
 * (click a segment), and what does the whole account add up to
 * (directional net, gross both ways, beta-weighted net, options margin
 * at risk, pairwise correlation of the held names).
 *
 * Options positions are shown as MARGIN AT RISK — the same collateral
 * math the engine's allocation gate charges — never as a fake delta.
 * The console has no greeks and says so instead of pretending.
 */

import { useCallback, useEffect, useState } from "react";
import { getFund, type FundInstance, type FundState } from "../../lib/api";
import { allocationSegments, fmtUsd } from "../../lib/fund";

const POLL_MS = 15_000;

function Tile({ label, value, tone, title }: {
  label: string;
  value: string;
  tone?: "profit" | "loss" | "muted";
  title?: string;
}) {
  const cls =
    tone === "profit" ? "text-bb-profit"
    : tone === "loss" ? "text-bb-loss"
    : tone === "muted" ? "text-bb-muted"
    : "text-bb-amber";
  return (
    <div className="panel px-3 py-2" title={title}>
      <div className="text-[10px] tracking-widest text-bb-muted">{label}</div>
      <div className={`text-[15px] font-semibold ${cls}`} data-numeric>{value}</div>
    </div>
  );
}

function AllocationBar({ fund, selected, onSelect }: {
  fund: FundState;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const segments = allocationSegments(fund.instances, fund.account.equity);
  return (
    <div className="panel px-3 py-2">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[10px] tracking-widest text-bb-muted">
          EQUITY ALLOCATION
        </span>
        <span className="text-[10px] text-bb-muted">
          {fmtUsd(fund.allocated_total)} allocated of {fmtUsd(fund.account.equity)} —
          click a strategy for its positions
        </span>
      </div>
      <div className="flex h-8 w-full overflow-hidden border border-bb-border">
        {segments.map((seg) => (
          <button
            key={seg.id}
            onClick={() =>
              onSelect(seg.id === "__unallocated__" ? null
                : selected === seg.id ? null : seg.id)}
            title={`${seg.label}: ${fmtUsd(seg.value)} (${seg.pct.toFixed(1)}%)`}
            style={{ width: `${seg.pct}%`, backgroundColor: seg.color }}
            className={
              "h-full min-w-[2px] transition-opacity " +
              (selected && selected !== seg.id ? "opacity-40" : "opacity-90 hover:opacity-100")
            }
          />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5">
        {segments.map((seg) => (
          <button
            key={seg.id}
            onClick={() =>
              onSelect(seg.id === "__unallocated__" ? null
                : selected === seg.id ? null : seg.id)}
            className={
              "flex items-center gap-1.5 text-[11px] " +
              (selected === seg.id ? "text-bb-amber" : "text-bb-muted hover:text-bb-amber")
            }
          >
            <span className="inline-block h-2 w-2" style={{ backgroundColor: seg.color }} />
            {seg.label}
            <span data-numeric>{fmtUsd(seg.value)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function PositionsPanel({ inst }: { inst: FundInstance }) {
  return (
    <div className="panel px-3 py-2">
      <div className="mb-1 flex items-baseline gap-3">
        <span className="text-[12px] font-semibold tracking-widest text-bb-amber">
          {inst.name.toUpperCase()}
        </span>
        <span className="text-[11px] text-bb-muted">{inst.kind}</span>
        <span className={"text-[11px] " +
          (inst.state === "enabled" ? "text-bb-profit" : "text-bb-orange")}>
          {inst.state}
        </span>
        <span className={"border border-bb-border px-1.5 text-[10px] " +
          (inst.live ? "text-bb-loss" : "text-bb-muted")}>
          {inst.live ? "LIVE" : "NOTE-MODE"}
        </span>
        <span className="ml-auto text-[11px] text-bb-muted" data-numeric>
          {fmtUsd(inst.deployed)} deployed / {fmtUsd(inst.allocated)} allocated
          {inst.breaker.limit != null &&
            ` · breaker ${fmtUsd(inst.breaker.drawdown)} of ${fmtUsd(inst.breaker.limit)}`}
          {inst.breaker.tripped ? " · TRIPPED" : ""}
        </span>
      </div>
      {inst.positions.length === 0 ? (
        <div className="py-2 text-[11px] text-bb-muted">
          no open positions{inst.live ? "" :
            " — note-mode journals decisions and places nothing; the twin on" +
            " the STRATEGIES page is this instance's would-be equity curve"}
        </div>
      ) : (
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-left text-bb-muted">
              <th className="px-2 py-1">underlying</th>
              <th className="px-2 py-1">class</th>
              <th className="px-2 py-1 text-right">qty</th>
              <th className="px-2 py-1 text-right">entry</th>
              <th className="px-2 py-1 text-right">legs</th>
              <th className="px-2 py-1 text-right"
                  title="signed share notional; options show margin at risk">
                net / margin
              </th>
              <th className="px-2 py-1">status</th>
            </tr>
          </thead>
          <tbody>
            {inst.positions.map((p) => (
              <tr key={p.plan_id} className="border-t border-bb-border">
                <td className="px-2 py-1 text-bb-amber">{p.underlying}</td>
                <td className="px-2 py-1">{p.asset_class}</td>
                <td className="px-2 py-1 text-right" data-numeric>{p.qty}</td>
                <td className="px-2 py-1 text-right" data-numeric>
                  {p.entry_limit.toFixed(2)}
                </td>
                <td className="px-2 py-1 text-right" data-numeric>{p.legs}</td>
                <td className="px-2 py-1 text-right" data-numeric>
                  {p.net_usd != null ? fmtUsd(p.net_usd) : `${fmtUsd(p.margin_usd)} margin`}
                </td>
                <td className="px-2 py-1 text-bb-muted">{p.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ExposurePanel({ fund }: { fund: FundState }) {
  const x = fund.exposure;
  const { symbols, matrix } = x.corr;
  return (
    <div className="panel px-3 py-2">
      <div className="mb-1 text-[10px] tracking-widest text-bb-muted">
        ACCOUNT EXPOSURE
      </div>
      {x.by_underlying.length === 0 && x.options_margin_usd === 0 ? (
        <div className="py-1 text-[11px] text-bb-muted">
          flat — nothing held anywhere in the book
        </div>
      ) : (
        <div className="flex flex-wrap gap-6">
          <table className="text-[11px]">
            <thead>
              <tr className="text-left text-bb-muted">
                <th className="px-2 py-1">underlying</th>
                <th className="px-2 py-1 text-right">net</th>
                <th className="px-2 py-1 text-right">gross</th>
                <th className="px-2 py-1 text-right" title="90-session beta vs SPY">β</th>
              </tr>
            </thead>
            <tbody>
              {x.by_underlying.map((u) => (
                <tr key={u.symbol} className="border-t border-bb-border">
                  <td className="px-2 py-1 text-bb-amber">{u.symbol}</td>
                  <td className={"px-2 py-1 text-right " +
                    (u.net_usd >= 0 ? "text-bb-profit" : "text-bb-loss")} data-numeric>
                    {fmtUsd(u.net_usd)}
                  </td>
                  <td className="px-2 py-1 text-right" data-numeric>{fmtUsd(u.gross_usd)}</td>
                  <td className="px-2 py-1 text-right" data-numeric>
                    {u.beta_spy == null ? "—" : u.beta_spy.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {symbols.length > 1 && (
            <table className="text-[11px]"
                   title="pairwise correlation of daily returns, trailing ~90 sessions">
              <thead>
                <tr className="text-bb-muted">
                  <th className="px-2 py-1 text-left">ρ</th>
                  {symbols.map((s) => (
                    <th key={s} className="px-2 py-1 text-right">{s}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {symbols.map((a, i) => (
                  <tr key={a} className="border-t border-bb-border">
                    <td className="px-2 py-1 text-bb-amber">{a}</td>
                    {symbols.map((b, j) => {
                      const v = matrix[i]?.[j];
                      const hot = v != null && i !== j && Math.abs(v) >= 0.7;
                      return (
                        <td key={b}
                            className={"px-2 py-1 text-right " +
                              (hot ? "text-bb-orange" : "text-bb-muted")}
                            data-numeric>
                          {v == null ? "—" : v.toFixed(2)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <div className="mt-1 text-[10px] text-bb-muted">
        options are margin at risk, not delta — the console carries no greeks.
        β-weighted net covers the share book only.
      </div>
    </div>
  );
}

export default function FundPage() {
  const [fund, setFund] = useState<FundState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getFund()
      .then((f) => { setFund(f); setError(null); })
      .catch((e) => setError(String(e?.message ?? e)));
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  if (error) {
    return <div className="panel m-1 p-3 text-[12px] text-bb-loss">fund: {error}</div>;
  }
  if (!fund) {
    return <div className="panel m-1 p-3 text-[12px] text-bb-muted">reading the book…</div>;
  }

  const x = fund.exposure;
  const selectedInst = fund.instances.find((i) => i.id === selected) ?? null;
  const shown = selectedInst ? [selectedInst] : fund.instances;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto">
      <div className="grid grid-cols-4 gap-px lg:grid-cols-8">
        <Tile label="EQUITY" value={fmtUsd(fund.account.equity)} />
        <Tile label="ALLOCATED" value={fmtUsd(fund.allocated_total)} />
        <Tile label="UNALLOCATED" value={fmtUsd(fund.unallocated)} tone="muted" />
        <Tile label="NET (SHARES)" value={fmtUsd(x.net_usd)}
              tone={x.net_usd >= 0 ? "profit" : "loss"}
              title="signed share notional across every strategy" />
        <Tile label="GROSS LONG" value={fmtUsd(x.gross_long_usd)} tone="muted" />
        <Tile label="GROSS SHORT" value={fmtUsd(x.gross_short_usd)} tone="muted" />
        <Tile label="β-WTD NET" value={fmtUsd(x.beta_weighted_net_usd)}
              tone={x.beta_weighted_net_usd >= 0 ? "profit" : "loss"}
              title="share notional times each name's 90-session SPY beta — the correlated-exposure headline" />
        <Tile label="OPT MARGIN" value={fmtUsd(x.options_margin_usd)}
              title="collateral the broker holds for options structures (the engine's own margin math)" />
      </div>
      <AllocationBar fund={fund} selected={selected} onSelect={setSelected} />
      {shown.map((inst) => <PositionsPanel key={inst.id} inst={inst} />)}
      <ExposurePanel fund={fund} />
    </div>
  );
}
